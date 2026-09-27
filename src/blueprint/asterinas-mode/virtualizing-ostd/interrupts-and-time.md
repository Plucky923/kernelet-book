# Interrupts and time

*Part of question 2. Virtualizes `irq` and `timer`. Discharges the interrupt-context half of invariant I7: no kernelet code ever runs in interrupt context, so a kernelet task can always be terminated where it stands.*

A kernelet has no interrupts. The host owns the interrupt descriptor table, the interrupt controller and every physical line; nothing a kernelet does can register a handler with them, mask them, or send one (absent `smp`, `IRQ_CHIP`, `disable_local` in its real sense). What a kernelet has instead is **virtual interrupt lines** raised by the endovisor, **jobs** delivered to its per-virtual-CPU **workers**, and a **tick** the host counts at the kernel's own frequency while the kernelet is busy and posts while it is idle. The kernel proper's drivers, bottom halves and timers keep their code and run in task context on the worker.

## Virtual interrupt lines

`IrqLine` keeps the tree's structure: a shared `Inner` per line holding the callback list under a reader-writer lock, an `InnerHandle` per `IrqLine` value so that clones share the line and the line is freed when the last clone drops, and a `CallbackHandle` per registered callback that unregisters itself on drop (checked on the tree: `ostd/src/irq/top_half.rs`). What is removed is what the host owns: the mapping to a hardware line, the acknowledgment, and `remapping_index`, which is absent. The lines are numbered 0 to 255: 0 is `TIMER_VIRQ`, reserved for the tick; 1 to 31 are reserved; 32 to 255 are device lines, which `alloc_specific(n)` takes when the virtio MMIO bus binds the line `BootArgs` lists for a device ([Devices](devices.md)), and `alloc` takes from what remains. `is_empty` and `num` read the handle's own list and number, as on the tree.

A line is raised only by the host, through `Kernelet::raise_irq(virq)` on the control half, which the endovisor's device models call from a hook or a device thread when a request completes. The host sets the line's pending bit and wakes the worker of the virtual CPU the line is **bound** to, the `vcpu` field of the device's description, fixed for the kernelet's life; a device the endovisor does not bind is on virtual CPU 0. If a task of the kernelet is running on that virtual CPU's host CPU with its preemption count at zero, the wakeup also sends a reschedule interrupt there, so that the worker preempts it at the next interrupt return rather than at the next tick.

## Workers and the job loop

Each virtual CPU has one worker, a host kernel thread the endovisor spawns for `create` through the `spawn_task` hook with `run_task(1, vcpu)`, pinned to that virtual CPU's host CPU and given the lowest `nice` of the kernelet's threads, so that the host serves a job before the kernelet's other threads on that CPU, which is what interrupt priority gives a real kernel. The worker delivers virtual interrupts, grants, and the ticks of an idle kernelet; a busy virtual CPU's ticks are consumed by its own tasks (below). The worker's body is in vOSTD:

```rust
// vOSTD, ostd/src/kernelet_side/worker.rs
extern "C" fn worker_main(vcpu: u64) -> ! {
    loop {
        let job = services().job_wait();                             // packed: kind in bits 0..8, argument above
        if job < 0 { break; }                                        // -STATE only: a bug; a kill never returns here
        rcu::note_quiescent(vcpu);                                   // the top of the loop is a quiescent state
        rcu::run_callbacks_if_period_complete(vcpu);                 // pending `RcuDrop`s of a completed period
        match job & 0xff {
            JOB_VIRQ  => deliver_virq((job >> 8) as u8),
            JOB_TICK  => tick::run(vcpu, (job >> 8) as u32, PrivilegeLevel::Kernel),   // an idle tick: nothing was interrupted
            JOB_GRANT => grant::add_new_runs(),
            _ => {}
        }
    }
    services().task_exit()
}

fn deliver_virq(virq: u8) {
    let frame = TrapFrame { trap_num: virq as usize, ..Default::default() };   // drivers ignore the frame (checked on the tree)
    let _guard = disable_preempt();                            // the interrupt-disabled state a top half expects
    level::enter_virtual(InterruptLevel::L1(PrivilegeLevel::Kernel), || {
        top_half::process(&frame, virq);                       // the tree's dispatch over the line's callbacks
        bottom_half::process(virq);                            // the L1 and L2 bottom-half hooks
    });
}
```

`job_wait` returns one job at a time; delivery is edge-triggered on the host side, so a line raised while its handler runs is the next job. A kill never returns from `job_wait`: a parked worker is terminated in the service epilogue like any parked task ([Faults, termination, and reclamation](../faults-and-reclamation.md)). The callbacks run as they would in a top half on the host kernel, with preemption disabled in place of interrupts disabled and with `InterruptLevel::current()` reporting `L1`. Four things on the tree read that level and must see `L1` here: `bottom_half::process`, which is `unreachable!` at level zero; the CPU-time statistics and the per-process CPU clocks, which charge time only at `L1` and split user from system by its privilege; and the softirq component's bottom-half guard, which runs pending softirqs on drop only in task context (checked on the tree: `ostd/src/irq/bottom_half.rs`, `time/cpu_time_stats.rs`, `process/process/timer_manager.rs`, `comps/softirq/src/lock.rs`). That makes `InterruptLevel` a virtualized item: OSTD's `level::enter` records the level in per-CPU state around a real top half (`ostd/src/irq/level.rs`), and vOSTD records it in the replica around a delivered job or a consumed tick.

The bottom-half dispatch is virtualized too, in one place: the tree's `process_l1` re-enables interrupts around the handler and then forgets the guard it took (checked: `bottom_half.rs`), which under the aliased guard would leak one preemption count per job; vOSTD's `process_l1` drops its guard and enables nothing. `register_bottom_half_handler_l1` and `_l2` are the identical hooks, and `bottom_half::process` runs after every delivered line and after every tick, as it runs after every interrupt on the tree, the timer's included, which is where the kernel proper's timer softirq advances the timer wheel (checked: `kernel/core/src/time/softirq.rs`).

What differs from a real top half is that the worker is a task: it can be preempted by the host if its preemption count is zero, which a top half never is, and it can sleep, which a top half must not. The first cannot happen inside a delivery, since the count is held for the whole of it, top half and up to the softirq's five rounds (checked: `comps/softirq/src/lib.rs`), so `policy.preempt_off_ticks` must exceed the longest delivery; the second is a bug in a driver that would also be a bug on the host kernel. A kernelet's worker is never entered by the host; it fetches its own work, which is what keeps the entry rule and the single-bit depth of invariant I7 true ([service half](../kernelet-api-service.md)).

## The tick

**Whom a tick charges, and who runs it.** The kernel proper's tick callbacks charge CPU time to `Thread::current()`, the thread the tick interrupted, and split user from system by the privilege level at the tick (checked on the tree: `update_cpu_statistics`, `update_cpu_time`); on the tree that is right because the tick runs on the interrupted task's stack. A kernelet cannot take the interrupt, but it can run the callbacks on the interrupted task itself, a little later, in task context: the host tick that finds a task of the kernelet running on a CPU adds one to that virtual CPU's `tick_pending` in the shared per-virtual-CPU record and notes whether it found the task in user mode ([service half](../kernelet-api-service.md)); the kernelet consumes the count at the next **tick point** on that virtual CPU, which is the top of `execute`'s loop after `user_run` returns for an interrupt ([User mode](user-mode.md)), the return from any service call, and `might_preempt`. The consumer swaps the count to zero, raises its preemption count, enters `InterruptLevel::L1` with the sampled privilege, and runs the per-CPU callbacks that many times, followed by the bottom halves:

```rust
// vOSTD, ostd/src/kernelet_side/tick.rs; called at every tick point
pub(crate) fn poll() {
    let vcpu = cpu_slot().vcpu;                                      // under the count raised below
    let pending = vcpu_record(vcpu).tick_pending.swap(0, Acquire);
    if pending == 0 { return; }
    let user = pending >> 31 != 0;
    run(vcpu, pending & 0x7fff_ffff, if user { PrivilegeLevel::User } else { PrivilegeLevel::Kernel });
}
pub(crate) fn run(vcpu: u64, ticks: u32, level: PrivilegeLevel) {
    let _guard = disable_preempt();
    level::enter_virtual(InterruptLevel::L1(level), || {
        for _ in 0..ticks { timer::call_timer_callback_functions(); }   // identical callbacks; `Thread::current()` is the consuming task
        bottom_half::process(TIMER_VIRQ);                                 // the timer softirq, as after every tick on the tree
    });
}
```

`Thread::current()` inside the callbacks is the consuming task, which is the interrupted task whenever the task that was running at the tick reaches a tick point before another task of the kernelet runs on that virtual CPU; when the host's kernel-mode preemption switched tasks at the tick, the next task of the kernelet on that virtual CPU consumes it, and the charge lands on it. That is a sampling error of at most one tick per switch, and the accounting is a sample anyway: under contention for a host CPU a kernelet's `times(2)` undercounts, as a guest's does under a hypervisor, since a tick that found a host task or another kernelet on the CPU is charged to no one. What this buys against delivering ticks on the worker is two host context switches per millisecond per busy virtual CPU, 0.2 to 0.4 percent of the CPU (*estimated* from the tree's switch cost), and the two `cfg` lines the callbacks would otherwise need to take the interrupted thread as an argument; the tick callbacks are identical code.

A tick that has been taken is consumed at the next tick point, which a task in user mode reaches at once, since the host's interrupt returns `user_run`; a task in kernel mode reaches it at its next service call or `might_preempt`. A task that computes in kernel mode without either is preempted by the host at the tick if its count is zero ([Tasks](tasks.md)), and the next task consumes the tick; a task that holds preemption off delays the tick as `cli` delays it on the tree, bounded by `policy.preempt_off_ticks`. The count is coalesced, so no jiffy of accounting is lost or duplicated; what can be late is the timer wheel's advance, by the longest such delay.

## `disable_local` and the guardians

`irq::disable_local` and `DisabledLocalIrqGuard` are virtualized without a crossing to a preemption-disable guard: the same type name, the same `GuardTransfer` and `PinCurrentCpu` implementations, a `preempt_count` increment on the record of the task in the CPU slot instead of a `cli`. The guardians `LocalIrqDisabled` and `WriteIrqDisabled`, and `SpinLock::disable_irq`, follow: a lock the kernel proper takes with interrupts disabled is a lock it takes with preemption disabled in a kernelet. This is sound because the only thing interrupt disabling excludes on the host kernel is a handler on the same CPU, and a kernelet has no handlers: its "handlers" are jobs on the worker task, which cannot run on this CPU while this task holds preemption off. The kernel proper's `pre_user_run` hook, which takes a `&DisabledLocalIrqGuard` and loads the thread's FS base, GS base and FPU state into the CPU (checked on the tree), gets the aliased guard and is correct for the same reason: the host does not move a task whose count is nonzero ([Tasks](tasks.md)).

## Time

`TIMER_FREQ` is identical, 1000 Hz on the tree. `Jiffies::elapsed` is virtualized without a crossing: it reads the host-wide clock page's `jiffies`, so time inside a kernelet is the host's time, advancing whether or not the kernelet runs; the increment of `ELAPSED` inside `call_timer_callback_functions` on the boot CPU (checked on the tree: `ostd/src/timer/mod.rs`) is `cfg`'d out, since the host counts. `register_callback_on_cpu` stores its callback in the calling virtual CPU's replica, as on the host, and the callbacks run on that virtual CPU when a tick is consumed there, under a preemption guard, in the order registered, once per host tick the count carries.

**When ticks arrive.** A real tick fires on every CPU every millisecond whether the CPU is busy or idle. A *busy* virtual CPU, one on whose host CPU a task of the kernelet was running at the host's tick, gets that tick added to its `tick_pending` and consumes it as above, at `TIMER_FREQ`: no wakeup, no crossing, the callbacks run on the task that was there. An *idle* kernelet, one none of whose tasks the host's tick found running during the period, gets `JOB_TICK` on virtual CPU 0's worker at the reduced rate `policy.idle_tick_hz`, 100 Hz by default (chosen), coalesced with a count; the kernel proper's timer wheel is advanced by the tick softirq, so a sleeping process whose timer expires is woken within 10 ms rather than 1 ms while its kernelet is idle, and with `idle_tick_hz = 0` an idle kernelet costs nothing and its timers wait for the next event that wakes it.

`timer_arm(vcpu, deadline)` is a one-shot behind the idle rule's extension: a `cfg` line in the kernel proper's real-time timer manager, the one driven by jiffies (checked on the tree: the per-process CPU-clock managers advance only on ticks charged to that process and have no deadline to arm), can report its earliest pending deadline, and vOSTD would arm it and let the host stop idle ticks entirely. A second `timer_arm` on a virtual CPU replaces its deadline and `u64::MAX` cancels it; the resolution is one host tick, since the host's own timer is the tick. The design permits that line but does not require it; without it, `idle_tick_hz` is the tenant-visible timer latency of an idle sandbox.

## What a tenant sees

- Timer latency for a sleeping process in an idle kernelet is up to `1 / idle_tick_hz` (10 ms by default) instead of 1 ms; while the kernelet is busy it is 1 ms as on the host kernel.
- Per-CPU `user`, `system` and `idle` jiffies in `/proc/stat` accrue only on delivered ticks, so a virtual CPU that was not busy shows frozen counters and the per-CPU sums no longer add up to uptime times the CPU count.
- CPU-time charging is a sample at the host's tick instant, consumed by the task that next reaches a tick point on that virtual CPU: `times(2)`, `getrusage` and `ITIMER_PROF` undercount when another kernelet or a host task held the host CPU at that instant, as a guest's do under a hypervisor, and a tick taken across a kernel-mode preemption is charged to the next task.
- `/proc/interrupts` shows the tick on line 0 and one virtual line per device from 32; every device interrupt of a kernelet is delivered on the worker of the virtual CPU its line is bound to, so unbound devices serialize on virtual CPU 0.
- Interrupt latency for a virtual device is a worker wakeup after the endovisor raises the line plus the worker's scheduling; when the bound CPU is running a kernelet task at preemption count zero the reschedule interrupt bounds it by an interrupt return, otherwise by the task's next preemption point, and the Evaluation chapter measures it against a virtio interrupt in a microVM.
- Nothing else: drivers, bottom halves and timers behave as on the host kernel.

## Costs

- Per virtual interrupt: one `raise_irq` (an atomic or, a wakeup, and a reschedule interrupt when the bound CPU is running a kernelet task), one `job_wait` return on the worker, the handler, and, for a virtio device, the two register crossings the transport's interrupt handler makes to read and acknowledge the interrupt status (checked on the tree: `transport/mmio/multiplex.rs`); *estimated* at one context switch and a few hundred cycles of bookkeeping, to be measured.
- Per tick on a busy virtual CPU: one atomic add by the host, one swap and the callbacks on the consuming task; no wakeup and no crossing. Per tick point: one load of `tick_pending`.
- Per idle kernelet: `idle_tick_hz` wakeups per second on one worker.
- Per virtual CPU: the worker's task and stack.
- Per `disable_local`: one store, against a `cli` and `sti` pair.
- Reclamation of an `RcuDrop` waits for the next switch point or tick point on its virtual CPU.

## What this page decides

- **Virtual interrupts are jobs on per-virtual-CPU workers, fetched, never pushed** (registers D9 and D11): the host never enters kernelet code except at task start.
- **`InterruptLevel` is virtualized to report the delivered level** (register D18), because four places in OSTD and the kernel proper dispatch on it and would panic, skip, or misaccount at level zero.
- **Ticks are rate-limited when a kernelet is idle** (register D19), with the rate a policy and a one-shot timer as the extension toward a tickless kernelet.
- **A busy virtual CPU's ticks are consumed in task context by the interrupted task, not delivered on the worker** (register D66, which withdraws D46). The alternative, delivering every tick as a job on the worker, costs two host context switches per millisecond per busy virtual CPU and needs the tick callbacks to take the interrupted thread as an argument; consuming the tick on the task itself makes the callbacks identical code and `Thread::current()` right by construction, at the price of a tick that waits while its task holds preemption off, bounded as on the tree.
