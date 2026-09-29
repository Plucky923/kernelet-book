# Interrupts and time

*Part of question 2. Virtualizes `irq` and `timer`. Discharges the interrupt-context half of invariant I7: a kernelet runs interrupt-context code only inside the upcall stub, at a point the host chose, so a carrier can always be stopped where it stands.*

A kernelet has no interrupts. The host owns the interrupt descriptor table, the interrupt controller and every physical line; nothing a kernelet does can register a handler with them, mask them, or send one (absent `smp`, `IRQ_CHIP`, `disable_local` in its real sense). What a kernelet has instead is **virtual interrupt lines** raised by the endovisor as bits in its per-virtual-CPU record, delivered by the **upcall** that [Scheduling](scheduling.md#upcall) specifies, and a **tick** the host counts at the kernel's own frequency. The kernel proper's drivers, bottom halves and timers keep their code and run where a real kernel runs them: in interrupt context, on the carrier that was interrupted.

That is the change from this chapter's earlier version, which delivered every virtual interrupt as a **job** on a per-virtual-CPU **worker** task. The workers are gone, and with them `job_wait`, the three job kinds, and the claim that no kernelet code ever runs in interrupt context.

## Virtual interrupt lines

`IrqLine` keeps the tree's structure: a shared `Inner` per line holding the callback list under a reader-writer lock, an `InnerHandle` per `IrqLine` value so that clones share the line and the line is freed when the last clone drops, and a `CallbackHandle` per registered callback that unregisters itself on drop (*checked on the tree*: `ostd/src/irq/top_half.rs`). What is removed is what the host owns: the mapping to a hardware line, the acknowledgment, and `remapping_index`, which is absent. The lines are numbered 0 to 255: 0 is `TIMER_VIRQ`, reserved for the tick; 1 to 31 are reserved; 32 to 255 are device lines, which `alloc_specific(n)` takes when the virtio MMIO bus binds the line `BootArgs` lists for a device ([Devices](devices.md)), and `alloc` takes from what remains. `is_empty` and `num` read the handle's own list and number, as on the tree.

A line is raised only by the host, through `Kernelet::raise_irq(virq)` on the control half, which the endovisor's device models call from a hook or a device thread when a request completes. The host sets the line's bit in the **LINES** field of the record of the virtual CPU the line is **bound** to — the `vcpu` field of the device's description, fixed for the kernelet's life; a device the endovisor does not bind is on virtual CPU 0 — with a release store, and then kicks that carrier: if it is running kernelet code it is redirected at its next trap return, and if it is asleep in `vcpu_idle` it is woken.

## Delivery is an upcall, not a job

The record's three pending groups are a **TICK** count, a **KICK** bit and the **LINES** mask. The host sets them; the carrier's stub consumes them. The stub enters `InterruptLevel::L1` with the privilege the host sampled at the trap, runs the tree's own dispatch over each pending line's callbacks, and then the bottom halves:

```rust
// vOSTD, ostd/src/kernelet_side/virq.rs — the body of the stub, after its register save
fn deliver(rec: &VcpuRecord) {
    let priv_ = rec.sampled_privilege();                  // the host's sample; the stub cannot derive it
    level::enter_virtual(InterruptLevel::L1(priv_), || {
        let ticks = rec.tick_pending.swap(0, Acquire);
        if ticks != 0 { tick::run(ticks); }               // identical timer callbacks
        let mut lines = rec.lines.swap(0, Acquire);
        while lines != 0 {
            let virq = lines.trailing_zeros() as u8;
            lines &= lines - 1;
            let frame = TrapFrame { trap_num: virq as usize, ..Default::default() };
            top_half::process(&frame, virq);              // the tree's dispatch over the line's callbacks
        }
        bottom_half::process(TIMER_VIRQ);                 // L1 and L2 hooks, as after every interrupt on the tree
    });
}
```

Four things on the tree read that level and must see `L1`: `bottom_half::process`, which is `unreachable!` at level zero; the CPU-time statistics and the per-process CPU clocks, which charge time only at `L1` and split user from system by its privilege; and the softirq component's bottom-half guard, which runs pending softirqs on drop only in task context (*checked on the tree*: `ostd/src/irq/bottom_half.rs`, `time/cpu_time_stats.rs`, `process/process/timer_manager.rs`, `comps/softirq/src/lock.rs`). `InterruptLevel` is therefore virtualized, and it is the **stub** that enters it, because OSTD's own `level::enter` is the host's cell, is `pub(super)`, and is back to its pre-trap value by the time the `iretq` runs — the kernelet's replica would read task context and the first delivered interrupt would hit that `unreachable!`.

Delivery is edge-triggered: a line raised while its handler runs is a bit set again, and the next upcall takes it. Nothing is fetched and nothing sleeps; the stub returns to the instruction the carrier was executing.

## `disable_local`, and why the aliasing goes

The earlier version of this page aliased `irq::disable_local` and `DisabledLocalIrqGuard` to a preemption-disable guard, and justified it in one sentence: *a kernelet has no handlers; its handlers are jobs on the worker task.* **Upcalls give it handlers**, so the justification is gone and the aliasing with it (register D18, revised).

In its place, the four real `arch::irq` primitives sit over the record's **`irq_off`** field, which is separate from `guards`, the count of held guards. Two fields and not one, for a reason each host's prototype found independently: a single count would mean that a kernelet holding a spin lock received no ticks. And they cannot be one field for a second reason — the L1 bottom half calls `disable_preempt()` and then `arch::irq::enable_local()`, two distinct facilities, and under the aliasing the second would have to lower the count the first had just raised.

The consequence for the kernel proper's locks is the opposite of what the old page said, and the prototype found it by hanging: **a kernelet's spin locks must take an interrupt guard, not a preemption guard.** The scheduler held its run-queue lock, the tick arrived as an upcall on that very task, the handler re-entered the run queue, and the virtual CPU deadlocked against itself (*measured on the booted Asterinas prototype*, finding F7). `LocalIrqDisabled`, `WriteIrqDisabled` and `SpinLock::disable_irq` therefore mean what they mean on a machine: they clear `irq_off`'s counterpart, and the host will not redirect a carrier while it is set.

## The tick

**Whom a tick charges.** The kernel proper's tick callbacks charge CPU time to `Thread::current()`, the thread the tick interrupted, and split user from system by the privilege level at the tick (*checked on the tree*: `update_cpu_statistics`, `update_cpu_time`). On the tree that is right because the tick runs on the interrupted task's stack — and with upcalls it is right here for the same reason. The host tick that finds a carrier of this kernelet running adds one to that virtual CPU's `tick_pending` and samples the privilege it interrupted; the carrier is redirected at that same trap's return, so the callbacks run **on the task that was interrupted**, in interrupt context, at `TIMER_FREQ`.

This is simpler than what the page said before, and one mechanism retires with it: the **tick points** — the top of `execute`'s loop, the return from a service call, `might_preempt` — at which a busy virtual CPU used to consume a coalesced count. They existed because nothing could interrupt a running kernelet task; now something can. `tick_pending` remains a count rather than a bit, because a carrier inside a critical section is not redirected and several host ticks may pass before it is; the count is coalesced, so no jiffy of accounting is lost or duplicated, and what can be late is the timer wheel's advance, by the length of the critical section, bounded by the cooperation bound ([Scheduling](scheduling.md#cooperative)).

The accounting is a sample, as it was: under contention for a host CPU a kernelet's `times(2)` undercounts, as a guest's does under a hypervisor, since a tick that found a host task or another kernelet on the processor is charged to no one.

## Time

`TIMER_FREQ` is identical, 1000 Hz on the tree. `Jiffies::elapsed` is virtualized without a crossing: it reads the host-wide clock page's `jiffies`, so time inside a kernelet is the host's time, advancing whether or not the kernelet runs; the increment of `ELAPSED` inside `call_timer_callback_functions` on the boot CPU (*checked on the tree*: `ostd/src/timer/mod.rs`) is `cfg`'d out, since the host counts. `register_callback_on_cpu` stores its callback in the calling virtual CPU's replica and the callbacks run there when the tick is delivered, in the order registered, once per host tick the count carries.

## Idling {#idle}

A kernelet now has idle tasks again ([Tasks](tasks.md)), so the idle rule is the idle loop's, not a policy knob's. The loop calls **`vcpu_idle(deadline)`**, which sleeps the carrier until the earliest of: the deadline, a virtual interrupt, or a kick. The deadline is the kernel proper's own earliest pending timer expiry.

That service is an addition to OSTD and not an optional one: *checked on the tree*, `halt_cpu()` is the only idle primitive and takes no argument, and `timer::register_callback_on_cpu` is per **host** processor, which is exactly what this design declines to arm. So **register D122**, a hook by which the owner of a timer wheel reports its earliest expiry, is a prerequisite on both hosts rather than an extension of one.

With it, `policy.idle_tick_hz` and the 10 ms default wake latency it implied both go: an idle kernelet wakes when its next timer is due, not at a rate chosen in advance, and costs nothing in between. The resolution is one host tick, since the host's own timer is the tick, and the prototype's carrier **halts rather than parks**, which is what fixes it at millisecond granularity (*measured on the booted Asterinas prototype*, deviation D8).

A **grant** is the other thing that used to be a job. The host sets a pending bit in the record and kicks the carrier; vOSTD reads the grant table past the count it last added, exactly as the requesting task does when `grains_request` returns ([Memory](memory.md)).

## What a tenant sees

- Timer latency is 1 ms while the kernelet is busy, as on the host kernel, and the time to its own next timer when it is idle — not a fixed 10 ms.
- Per-CPU `user`, `system` and `idle` jiffies in `/proc/stat` accrue only on ticks the host charged to this sandbox, so a virtual CPU whose carrier was not running shows frozen counters and the per-CPU sums no longer add up to uptime times the CPU count.
- CPU-time charging is a sample at the host's tick instant, charged to the task that was interrupted: `times(2)`, `getrusage` and `ITIMER_PROF` undercount when another kernelet or a host task held the processor at that instant, as a guest's do under a hypervisor.
- `/proc/interrupts` shows the tick on line 0 and one virtual line per device from 32; every device interrupt is delivered on the carrier of the virtual CPU its line is bound to, so unbound devices serialize on virtual CPU 0.
- Interrupt latency for a virtual device is the time to the bound carrier's next trap return, or a wake if it is idle — not a task wakeup.
- Nothing else: drivers, bottom halves and timers behave as on the host kernel.

## Costs

- Per virtual interrupt: one release store and one kick by the host; one redirect and the stub's register save on the carrier; then the handler. **No task wakeup** — against about 3 µs for one before.
- Per tick on a busy virtual CPU: one atomic add and one privilege sample by the host, one swap and the callbacks in the stub.
- Per idle virtual CPU: nothing until its deadline or a kick.
- Per `disable_local`: one store, against a `cli` and `sti` pair.

## What this page decides

- **Virtual interrupts are pending bits in a per-virtual-CPU record, delivered by upcall on the carrier** (register D117, this host's binding; it retires D9 and D11, the jobs and the workers). The alternative is what this chapter said before: a worker task per virtual CPU, which costs a wakeup per interrupt, serializes a device's handler behind the host's scheduler, and makes "no kernelet code runs in interrupt context" true at the price of every interrupt's latency.
- **`InterruptLevel` is entered by the stub, with the privilege the host sampled** (register D18, revised). The host's own level is back to its pre-trap value by the `iretq`, and the stub cannot derive the privilege because the redirect only ever rewrites kernel-mode frames.
- **The four `arch::irq` primitives sit over the record's `irq_off`, and the kernelet's spin locks take an interrupt guard** (register D18, revised). The aliasing to the preemption count rested on a kernelet having no handlers, which upcalls falsify, and the prototype deadlocked a virtual CPU against itself to prove it.
- **The tick is delivered by the same upcall, on the interrupted task** (register D66, halved: the count survives, the tick points do not). `Thread::current()` is right by construction, as it is on the tree, because the callbacks now run where the tick landed.
- **An idle carrier sleeps to its kernel's own next deadline** (register D19, retired in favor of D122). The alternative, a policy tick rate, was a fixed 10 ms of timer latency chosen because nothing could report the real deadline.
