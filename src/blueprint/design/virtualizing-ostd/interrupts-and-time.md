# Interrupts and time

*Part of question 2. Virtualizes `irq` and `timer`. Establishes the interrupt-context half of invariant I7: host interrupt handlers never call kernelet code.*

The host owns the interrupt descriptor table, the interrupt controller and every physical interrupt line.
Virtual device interrupts are raised by the endovisor and delivered by a **worker on each vCPU**. Virtual ticks instead run with the interrupted Task still current, as specified in [The tick](interrupts-and-time.md#tick).
A worker is an internal task with its own stack, using the vCPU Thread described in [Tasks](tasks.md#objects).
It calls the kernel's registered driver handlers and bottom halves; the host never calls those closures.

For example, a virtual disk finishes a request: the endovisor records the completion, raises its virtual line, and wakes the bound vCPU if necessary.
When that vCPU next permits virtual interrupt delivery, its worker runs the disk handler, which reads the completion and wakes the waiting internal Task.
Time notifications follow the same host-to-kernelet boundary, but elapsed wall time and a Task's charged CPU time have different consumers, described below.

The vCPU Thread executes the worker on that worker's internal Task stack. vOSTD presents interrupt-handler semantics while the worker calls handlers.
The host may physically interrupt and pause it, even during a virtual IRQ guard; resuming continues the same instruction.
The separate [host interruption path](interrupts-and-time.md#host-irq-entry) explains how OSTD preserves that execution.

## Virtual interrupt lines

`IrqLine` keeps the tree's structure: a shared `Inner` per line holding the callback list under a reader-writer lock, an `InnerHandle` per `IrqLine` value so that clones share the line and the line is freed when the last clone drops, and a `CallbackHandle` per registered callback that unregisters itself on drop (checked on the tree: `ostd/src/irq/top_half.rs`). What is removed is what the host owns: the mapping to a hardware line, the acknowledgment, and `remapping_index`, which is absent. The lines are numbered 0 to 255: 0 is `TIMER_VIRQ`, reserved for the tick; 1 to 31 are reserved; 32 to 255 are device lines, which `alloc_specific(n)` takes when the virtio MMIO bus binds the line `BootArgs` lists for a device ([Devices](devices.md)), and `alloc` takes from what remains. `is_empty` and `num` read the handle's own list and number, as on the tree.

`Kernelet::raise_irq` sets host pending state and notifies the bound vCPU Thread if idle.
It does not call a kernelet callback from the device completion path and does not schedule the virtual interrupt worker as a separately scheduled native thread.
Repeated notifications may coalesce while results remain in the device's bounded completion state.
The [lending-device](../zero-copy-io.md) completion path uses the same boundary; frame-return/DMA ownership must finish according to that device protocol.

## Workers and the event loop {#worker-delivery}

Each vCPU's first idle entry creates one ordinary internal Thread pinned to that vCPU, using the [kernel builder](tasks.md#objects). Its closure calls the public vOSTD `irq::worker_main` below. There is no separate worker entry selector or Host-reserved Task resource.
The worker is registered in the same run queue as other Tasks. Therefore native `WaitQueue::wait_until` can park it, and native Waker can enqueue it, without changing which Task the scheduler believes is current.

Host keeps pending notifications in its own `Vcpu.events`; `event_take` transfers claimed work into vOSTD's separate, image-owned `events_for(vcpu)` storage.
At an internal checkpoint, vOSTD collects that work and wakes the worker's internal wait queue:

```rust
// vOSTD. One preallocated event state and native WaitQueue per vCPU.
// events_for returns stable image-owned storage, not a CPU-local borrow.
pub(crate) fn poll_events_at_checkpoint() {
    if !virtual_guards_allow_delivery() { return; }
    collect_host_events(); // Nonblocking SERVICE event_take; publishes pending work.
    let events = events_for(current_vcpu());
    if events.has_pending_work() {
        events.wait_queue.wake_all(); // Native Waker -> unpark_target -> internal enqueue.
    }
}

// Public vOSTD function called by the kernel-created Thread's closure.
pub fn worker_main() -> ! {
    let events = events_for(current_vcpu()); // This Thread never changes vCPU.
    loop {
        events.wait_queue.wait_until(|| {
            events.has_pending_work().then_some(())
        }); // No virtual guard held: native park_current selects another Task.

        drain_pending_work(); // Bounded batch; device branch calls deliver_virq below.
        Task::yield_now();    // Normal internal scheduling, including after a full batch.
    }
}
```

`drain_pending_work` consumes this vCPU's device bits and wall-time/grant recheck flags; it does not apply CPU ticks for another Task. It releases pending-state locks before invoking callbacks or applying work. Device callbacks run under the virtual interrupt guard shown below; other ordinary work runs at L0. Failed/retryable work stays pending. A continuously busy worker is subject to the same internal scheduling rules as other Tasks; Host schedules the vCPU Thread that executes it.

The checkpoint is called before the native `might_preempt` check and in idle before its no-work check. It neither chooses a Task nor switches stacks itself. A service return may invoke it only at an explicit scheduling-safe boundary; `event_take`, `vcpu_notify`, pending-state helpers and the checkpoint itself must not recursively invoke it.
Virtual IRQ/preemption guards and transition exclusion defer the whole checkpoint. Bootstrap can retain pending events before worker creation; the newly created worker checks pending state before sleeping.

`collect_host_events` calls `(service_table().event_take)()` in a bounded batch and decodes the service ABI's `PairResult`.
Before allowing a switch, it records each claimed event in preallocated per-vCPU pending state; it neither allocates nor calls kernel callbacks during that handoff.
The status is decoded before releasing virtual exclusion:

```rust
// vOSTD: bounded event collection; pending storage is preallocated.
fn collect_host_events() {
    let _guard = irq::disable_local();
    for _ in 0..EVENT_BATCH_LIMIT {
        let PairResult { value, status } = (service_table().event_take)(); // SERVICE.
        match status {
            0 => break,
            1 if (32..=255).contains(&value) => pending_device_lines().set(value as u8),
            3 => set_time_recheck_pending(),
            4 => set_grant_recheck_pending(),
            _ => crate::panic::abort(), // Negative status or invalid category payload.
        }
    }
}
```

The setters update this vCPU's internal pending state, without callbacks or checkpoints. Status 2 is reserved; virtual ticks use their separate entry protocol. A full notification queue falls back to discoverable authoritative state under the [event contract](synchronization.md#vcpu-wait). Ending a bounded batch leaves remaining events discoverable.
`take_pending_device_irq` removes one bit under virtual exclusion and releases its pending-state borrow before calling a handler.
If the host raises that line again during delivery, the new notification remains pending for a later iteration.
Repeated notifications can coalesce because the driver drains authoritative device completion state, not because each notification represents one completion.

A wake before the worker registers is safe: `WaitQueue::wait_until` checks the predicate, registers its Waker, then checks again. A wake during park uses the native wake token and enqueue path. Neither case needs a Host pointer to the worker or its Waker. Only idle uses `vcpu_wait` after all internal work is unavailable.

Native L1 bottom-half code requires adaptation: its physical enable/forget-guard sequence cannot be used with a virtual guard, where forgetting the guard would leak exclusion.
The virtual implementation releases its own guard without changing physical IF; an enclosing worker-delivery exclusion still prevents same-vCPU callback reentry.
Host L1/L2 interrupt nesting is separate and keeps its own guards and stack slots.
Callbacks have the same restrictions as native interrupt handlers: acknowledge/drain the immediate event, wake waiters or queue work, then return.
Long work belongs in ordinary internal tasks; sleeping inside the callback is invalid even though the underlying host execution is a thread.
A handler that loops, or a worker that continually prioritizes device traffic, can starve this kernelet's ordinary tasks.
That is the kernelet's scheduling responsibility; Host still controls when its vCPU Thread runs.

### Calling the existing driver and bottom-half hooks

The callback list remains the native `Inner.callbacks` list from `ostd/src/irq/top_half.rs`.
The native dispatcher takes `&HwIrqLine` and acknowledges hardware, so its body needs the following small kernelet branch; passing a `u8` to the native function unchanged would be incorrect.

```rust
// Proposed cfg branch inside vOSTD's irq module.
fn deliver_virq(virq: u8) {
    assert!((32..=255).contains(&virq)); // Device lines; timer work is separate.
    let _irq_guard = irq::disable_local(); // Virtual exclusion, no CLI.
    assert_eq!(InterruptLevel::current(), InterruptLevel::L0);
    let frame = TrapFrame { trap_num: usize::from(virq), ..Default::default() };

    level::enter(|| {
        top_half::process_virtual(&frame, virq);
        bottom_half::process(virq);
    }, PrivilegeLevel::Kernel);
}

// In irq/top_half.rs; INNERS contains the 224 device-line entries, 32..=255.
pub(super) fn process_virtual(frame: &TrapFrame, virq: u8) {
    let inner = &INNERS[usize::from(virq - 32)];
    for callback in &*inner.callbacks.read() {
        callback(frame);
    }
    // No HwIrqLine::ack(): the host owns the physical interrupt controller.
}

// Kernelet branch of irq/bottom_half.rs::process_l1.
fn process_l1(irq_num: u8) {
    let Some(handler) = BOTTOM_HALF_HANDLER_L1.get() else { return; };
    let preempt_guard = disable_preempt();
    let irq_guard = handler(disable_local(), irq_num);
    drop(irq_guard); // Balance the virtual guard; do not forget it.
    drop(preempt_guard);
}
```

The outer delivery guard remains held even if the bottom-half hook releases its own guard.
`level::enter` is the native scoped level update, operating on the vCPU's replica in this build: the callbacks see `L1(Kernel)`, and normal return restores `L0`.
L1 means “delivering an interrupt callback”; it does not mean physical IF is clear or that the worker interrupted the current user thread.
`bottom_half::process` uses this level to choose L1/L2 dispatch, and the softirq guard uses it to distinguish callback execution from ordinary Task execution.
The synthetic frame identifies the virtual line; it is not a snapshot of an interrupted Task's registers.
Handlers that require real interrupted registers need a different explicit interface; supported device callbacks must not infer those registers from this frame.
An uncaught panic during delivery must stop the instance rather than resume ordinary code with a partially restored level/guard state.

## Host interruption and saved state {#host-saved-state}

A kernelet sees virtual interrupts, but it still executes on a physical CPU.
The physical timer can interrupt that execution and let Host schedule another native thread.
If Host switches the vCPU Thread out, it preserves two continuations: the interrupted execution and the later Host scheduling call, as shown in the [execution-flow guide](tasks.md#execution-flow).
Re-selection first resumes Host code; the return path then restores internal execution, with any eligible [virtual tick](interrupts-and-time.md#tick) handled before ordinary continuation.
Physical pause/resume itself neither selects another internal Task nor reruns an internal pre/post pair.
Virtual guards preserve internal exclusion across this pause, not physical IF.

**What actually saves the state on x86-64?**
The native `ostd/src/arch/x86/trap/trap.S` kernel-entry path pushes general registers, passes rsp to trap_handler as a TrapFrame pointer, then pops those registers before interrupt return.
`TrapFrame` in `ostd/src/arch/x86/trap/mod.rs` distinguishes the software-pushed registers from the frame pushed by the CPU.
That native mechanism is the starting point; the proposed kernelet pause path needs additional state and storage:

`TrapFrame` retains all GPRs and the CPU's `rip`, `rsp`, `rflags`, `cs` and `ss`. The Host-owned `CpuStateSnapshot` below retains FS, inactive user GS, FPU, CR3 and the TSS user-return fields. Copy the interrupt frame and retain its stack/root owners before releasing the IRQ entry slot; a raw CR3 value does not own mappings.

The entry prefix must capture supplementary state before any compiled code can clobber it; calling an ordinary Rust “save” function after an unrestricted handler is too late.
Return restores the selected root and supplementary state, restores the general registers, and finishes in an audited assembly interrupt-return tail with no later clobber.
The native TrapFrame/save primitives do not by themselves implement these added entry and lifetime guarantees.

The [handler interruption example](tasks.md#handler-physical-pause) explains why a partially saved pre-handler context cannot replace this snapshot of the actual CPU registers.
vOSTD's user-GS primitive therefore accesses the inactive user-base MSR directly; a virtual guard cannot make a temporary swapgs of the active host GS safe.

Ordinary service return restores its input snapshot; completed `user_run` instead retains and restores the newly produced user state.
Restoring the old service input there would erase user execution.
A physical pause preserves the interrupted state for continuation, subject to the tick-entry ordering below.
Voluntary internal switching selects the incoming resource's saved state.

### CpuStateSnapshot used by both return paths {#machine-state-code}

The return tail pops the general registers and uses `iretq` for the interrupt frame. Neither operation restores FS, user-GS, FPU or the page-table root.
The Task switch and Host pause paths therefore retain a Host-private `CpuStateSnapshot` beside their register contexts.
It holds the supplementary CPU registers absent from `TaskContext` and `TrapFrame`; those structures still hold their own general-register and return state:

```rust
// Initialized once from the Host CPU-local layout, before any vCPU is started.
static CURRENT_TASK_CPU_STATE_OFFSET: core::sync::atomic::AtomicUsize =
    core::sync::atomic::AtomicUsize::new(0);

#[repr(C)]
struct CpuStateSnapshot {
    cr3: u64,
    fs_base: u64,
    user_gs_base: u64, // IA32_KERNEL_GS_BASE while active GS remains Host GS.
    tss_sp0: u64,
    tss_sp1: u64,
    fpu_area: *mut u8, // Owned, preallocated storage; never a kernelet pointer.
    xsave_mask: u64,
    use_xsave: u64,   // 0: FXSAVE64, 1: XSAVE64; validated at creation.
}
```

The field layout is derived from this `repr(C)` definition, not the service ABI.
Each snapshot has its own FPU backing, sized/aligned using the native FPU feature discovery and initialization. `fpu_area`, `xsave_mask` and `use_xsave` are initialized before capture and never supplied by a kernelet.
The retained execution resource also owns its root/mappings; a raw CR3 value is not a lifetime claim. All entry code, snapshot storage and the vCPU Host Tasks' native stacks must be mapped in every permitted root.

Native `GsBase::save/load` in `ostd/src/arch/x86/cpu/context/mod.rs` uses `swapgs; rdgsbase/wrgsbase; swapgs` under a physical IRQ guard. In vOSTD, the guard is virtual, so keep active GS unchanged and access the inactive user base directly:

```rust
// Proposed vOSTD x86 GsBase backend. These methods execute only with Host GS
// active; user entry/exit remains Host assembly. Same public API as native.
impl GsBase {
    pub fn save(&mut self, _guard: &DisabledLocalIrqGuard) {
        let low: u32;
        let high: u32;
        unsafe {
            core::arch::asm!("rdmsr",
                in("ecx") 0xc0000102u32, lateout("eax") low, lateout("edx") high,
                options(nostack, preserves_flags));
        }
        self.0 = ((u64::from(high) << 32) | u64::from(low)) as usize;
    }

    pub fn load(&self, _guard: &DisabledLocalIrqGuard) {
        // Same accepted address domain as native; invalid restore faults stop
        // the instance through Host recovery, never resume a partial user entry.
        let value = self.0 as u64;
        unsafe {
            core::arch::asm!("wrmsr", in("ecx") 0xc0000102u32,
                in("eax") value as u32, in("edx") (value >> 32) as u32,
                options(nostack, preserves_flags));
        }
    }
}
```

Native `FsBase::save/load` uses `rdfsbase/wrfsbase` and never switches Host GS, so its register operation can remain. Both FS and user GS still need physical snapshot/restore if Host interrupts them. A pause between `rdmsr` and the Rust assignment restores both the captured GPR result and the actual supplementary registers; it does not alter the kernelet's `CpuSync` bookkeeping.

The following proposed x86-64 helpers preserve callee-saved GPRs. Entry assembly calls capture **after** saving all live GPRs but **before** any compiled Host handler can change supplementary state:

```rust
#[unsafe(naked)]
unsafe extern "C" fn capture_cpu_state(state: *mut CpuStateSnapshot) {
    core::arch::naked_asm!(
        "mov rax, cr3", "mov [rdi + 0], rax",
        "mov ecx, 0xc0000100", // IA32_FS_BASE.
        "rdmsr", "shl rdx, 32", "or rax, rdx", "mov [rdi + 8], rax",
        "mov ecx, 0xc0000102", // Inactive user GS; never swap active Host GS.
        "rdmsr", "shl rdx, 32", "or rax, rdx", "mov [rdi + 16], rax",
        "mov rax, gs:[4]", "mov [rdi + 24], rax",  // Native TSS.sp0.
        "mov rax, gs:[12]", "mov [rdi + 32], rax", // Native TSS.sp1.
        "mov rsi, [rdi + 40]",
        "cmp qword ptr [rdi + 56], 0", "je 2f",
        "mov rax, [rdi + 48]", "mov rdx, rax", "shr rdx, 32",
        "xsave64 [rsi]", "ret",
        "2:", "fxsave64 [rsi]", "ret",
    );
}

#[unsafe(naked)]
unsafe extern "C" fn restore_cpu_state(state: *const CpuStateSnapshot) {
    core::arch::naked_asm!(
        "mov rax, [rdi + 0]", "mov cr3, rax",
        "mov rax, [rdi + 8]", "mov rdx, rax", "shr rdx, 32",
        "mov ecx, 0xc0000100", "wrmsr",
        "mov rax, [rdi + 16]", "mov rdx, rax", "shr rdx, 32",
        "mov ecx, 0xc0000102", "wrmsr",
        "mov rax, [rdi + 24]", "mov gs:[4], rax",
        "mov rax, [rdi + 32]", "mov gs:[12], rax",
        "mov rsi, [rdi + 40]",
        "cmp qword ptr [rdi + 56], 0", "je 2f",
        "mov rax, [rdi + 48]", "mov rdx, rax", "shr rdx, 32",
        "xrstor64 [rsi]", "ret",
        "2:", "fxrstor64 [rsi]", "ret",
    );
}
```

Only audited assembly gates install a `CpuStateSnapshot`.
For internal resumption, the remaining assembly restores GPRs and executes `iretq` or the cooperative `sti; ret`; no compiled code may clobber the installed state first.
For return to the vCPU loop, the gate instead restores the loop's own state and uses a masked `ret`, after which that loop can execute Host Rust normally.
IRQ entry similarly installs Host state before entering compiled Host handlers.
The native `FpuContext::save/load` includes logging, so its allocation/feature machinery can be reused, but those wrappers cannot serve as the final machine tail.

This path targets the native x86-64 feature profile: the mask covers every enabled supported user XSTATE component, CR3 is installed with normal invalidation semantics, and CET or additional unhandled architectural state must not be enabled silently.
A new Task gets initialized FPU state and a validated kernel root/TLS/TSS environment; it must not inherit another tenant's live snapshot.
The return gate checks canonical addresses, kernel selectors, controlled stack bounds, xstate format and root ownership before the naked tail. Restore faults belong to a Host exception/recovery entry while these owners remain retained.
NMI uses independent GPR/supplementary storage, preserves the actual partially updated machine state and returns without scheduling or reusing the ordinary IRQ slot. Double fault has an independent entry stack.
**[unverified]** The helpers state the actual instructions; validating the feature profile, installing entry stacks/exception recovery and testing nested entry still require kernel implementation.

## Host IRQ entry, scheduling and return {#host-irq-entry}

The physical IRQ path runs Host handlers and acknowledges the interrupt; virtual callbacks remain kernelet checkpoint work.
An eligible internal pause returns `Pause` to the [vCPU execution loop](tasks.md#vcpu-loop). It preserves the interrupted internal Task; it does not request an internal Task switch.
Native `trap.S` does not yet redirect IRQ return to the vCPU Thread's saved call on its original stack.
The [Host preemption checkpoint contract](tasks.md#host-preemption-checkpoint) requires this path to reach Host `might_preempt()` without voluntary calls from an internal Task.
Interrupt completion alone is not a scheduling decision.
The added checkpoint applies to the vCPU pause/return path: identify the interrupted current Task as a vCPU Thread and check its execution phase before redirecting.
Ordinary native threads retain their existing IRQ return path; scheduler re-selection is not the trigger for this redirect.
The proposed integration is:

1. On entry, save all interrupted GPRs and the full kernel `TrapFrame` before using registers to classify the interrupted phase. Select separate, unoccupied supplementary snapshot storage for that phase, capture `CpuStateSnapshot`, then install Host CPU state before compiled Host handlers. An IRQ during the vCPU execution loop must not overwrite the internal snapshot whose return is still pending. The [Rust entry gate and return handoff](tasks.md#handler-physical-pause) cover interrupted kernelet handlers. Use a Host-controlled IRQ stack with separate NMI/double-fault storage; the native tree has not installed this stack arrangement.
2. Finish Host callbacks, EOI and interrupt-level cleanup. Native L1 bottom-half handling enables physical IRQs, so nested Host IRQs need separate stack/snapshot storage and must preserve the retained internal snapshot. Return to the outer epilogue with IRQs masked. An NMI uses its independent storage and returns without scheduling.
3. At the legal outer epilogue, check the actual Host phase. Kernelet execution or a resumable Host service may publish a pause snapshot and restore the pending `vcpu_loop_context`, returning to the same Thread's saved call on its vCPU Thread's original stack. That call and its callers return before the vCPU execution loop handles the new reason. An IRQ interrupting Host code uses the native Host IRQ path; it must neither consume that pending context again nor reset the Host stack. An eligible internal-execution return with a pending Host scheduling request must take the Host return checkpoint before resuming. If a native critical section prevents the handoff, retain the request and return to the Host operation; its deferred path must explicitly check preemption after the critical section and before internal return. If no redirect is needed, restore the interrupted state through the IRQ return path directly, subject to tick delivery below.
4. The [Host return handler](tasks.md#vcpu-dispatch) adds a Host `might_preempt` checkpoint after IRQ handling: native timer/wakeup paths set the request, and this check tests the request and native preemption guards before selecting another native thread. This checkpoint is new integration; the current x86 kernel IRQ return does not invoke it. Only an actual Host switch saves `vcpu_thread_task.ctx`; re-selection restores that native switching call, which eventually returns to the Host return handler. Its return gate checks stop and resource validity before restoring the saved internal execution. Without a Host switch, the Host return handler reaches the return check directly. An eligible virtual tick requires the additional entry below before ordinary internal continuation; the shown `iretq` tail alone is incomplete for that case.
5. Stop completes or cancels pending Host operations, then ends the vCPU execution loop and returns normally from `enter_vcpu`. Native Thread teardown owns the vCPU Thread's original stack; there is no separate dispatch stack to reclaim.

The replay tail accepts canonical **kernel** interrupt frames. Native user interrupt entry instead writes `RawUserContext` and returns through `user_run`; it cannot supply its synthesized zero CS/SS values to this `iretq` path. Exceptions must finish their own recovery before any scheduling redirect; NMI never takes this redirect.
For a pause inside pre/post, the snapshot is the actual in-progress machine state; neither the handler nor the older internal Task context is restarted.
`CpuSlot` and root ownership are installed before every return. TSS.sp0 remains the native user-return pointer, never a reset Host stack pointer.
**[unverified]** Machine entry, phase dispatch, late-stop handling and bounded stack storage still need kernel integration and register-pattern/IRQ/kill tests. The restore instructions alone do not establish those entry guarantees.

## The tick: CPU time and timer work {#tick}

A **tick** is one periodic timer event: the physical timer interrupts execution, and OSTD invokes the registered timer callbacks.
The callbacks update scheduling state, CPU-time statistics and expired timers.
A tick does not by itself switch Tasks; the scheduling callback may set a preemption request for a later scheduling check.
Native POSIX CPU-clock accounting samples the current Thread and interrupted user/kernel mode, then adds one jiffy (one tick interval) to the corresponding Thread and Process clocks.
That callback skips non-POSIX threads and samples taken while an interrupt bottom half is running (native L2); scheduler runtime accounting separately measures execution intervals.

A **virtual tick** means delivering a physical timer sample to the kernelet's own timer callbacks through a controlled vOSTD entry.
It runs on the existing vCPU Thread with the interrupted internal Task still current and its user/kernel mode preserved.
Host and vOSTD each have their own `INTERRUPT_CALLBACKS` list; running Host's physical timer callbacks does not run the image's list.
The device interrupt worker cannot deliver this CPU sample in its place: callbacks reading the current Task would see the worker.

**Accepted on 2026-09-15 (D66 revised):** Host records the timer sample when a physical tick interrupts internal execution.
On eligible return, it arranges virtual-tick delivery before ordinary continuation or an internal Task switch.
The proposed callback signature and body follow below; the [remaining delivery obligations](interrupts-and-time.md#tick-return) are not implemented.

### 1. The kernelet registers its own callbacks {#tick-registration}

The kernel sources below are compiled against vOSTD in a kernelet build.
Their `ostd::timer` calls access that image's timer module, and its `INTERRUPT_CALLBACKS` CPU-local list has one replica per vCPU.

```rust
// Existing kernel/core/src/process/process/timer_manager.rs.
// In a kernelet build, timer is vOSTD's timer module.
pub(super) fn init_on_each_cpu() {
    timer::register_callback_on_cpu(update_cpu_time);
}

// Existing ostd/src/task/scheduler/mod.rs; shared source.
// In vOSTD, the scheduler and CPU-local flag below belong to the kernelet.
pub fn enable_preemption_on_cpu() {
    timer::register_callback_on_cpu(|| {
        scheduler_singleton().mut_local_rq_with(&mut |local_rq| {
            let should_pick_next = local_rq.update_current(UpdateFlags::Tick);
            if should_pick_next {
                cpu_local::set_need_preempt();
            }
        });
    });
}
```

`register_callback_on_cpu` retains its native body from `ostd/src/timer/mod.rs`: append the callback to the current CPU's `INTERRUPT_CALLBACKS` list under an IRQ guard.
In vOSTD that guard is virtual and the list belongs to the current vCPU.
Registration happens during initialization; it does not execute the callbacks.

### 2. Host enters the vOSTD tick function {#tick-entry}

A physical tick first runs Host's timer callbacks and records a pending virtual tick for the interrupted internal execution.
The eligible pause path returns to the [vCPU loop](tasks.md#vcpu-loop), where Host's `might_preempt` may switch out the vCPU Thread.
When that call returns, the same vCPU Thread can enter the pending virtual tick with the interrupted internal Task still current.
Host must publish the interrupted Task's execution total before the internal scheduler callback reads its [runtime clock](tasks.md#tick-accounting).

The image stores `tick_entry` below in [EntryTable::tick](../kernelet-api-service.md#entry-table).
The argument carries only the interrupted user/kernel mode. The callbacks obtain the internal Thread from `Thread::current()` and do not consume a TrapFrame.
Host retains the original snapshot for restoration; tick delivery adds no copy of that snapshot and no Task identifier argument.
This scalar entry describes a sample taken during ordinary internal execution at virtual L0.
A sample taken during virtual interrupt handling must not later be delivered through this entry as an ordinary Task sample: that would charge CPU time which the native L2 callback skips.
Checking L0 at delivery time alone cannot establish the level at sampling time; handling such samples remains part of the delivery obligations below.

Before calling, Host's entry code must establish the interrupted Task's kernel stack and vOSTD CPU state, retain a return to the vCPU loop, and verify that virtual guards and transition state permit delivery.
The guard acquired inside `tick_entry` protects callback execution; it does not make an otherwise forbidden entry legal.

```rust
// Proposed vOSTD addition in ostd/src/irq/kernelet_tick.rs.
// TICK_KERNEL/TICK_USER are imported from this image's ostd::kernelet::abi.
// Host invokes this fixed entry only after establishing its stack and entry conditions.
pub(crate) unsafe extern "C" fn tick_entry(interrupted_mode: u32) {
    let interrupted_mode = match interrupted_mode {
        TICK_KERNEL => PrivilegeLevel::Kernel,
        TICK_USER => PrivilegeLevel::User,
        _ => panic!("invalid virtual tick mode"),
    };
    let _irq_guard = super::disable_local(); // vOSTD virtual guard; no physical CLI.
    assert_eq!(InterruptLevel::current(), InterruptLevel::L0);

    level::enter(|| {
        // This image's callbacks see the interrupted internal Task as current.
        timer::call_timer_callback_functions();
        bottom_half::process(TIMER_VIRQ);
    }, interrupted_mode);
    // Normal return restores L0 and drops the virtual guard.
    // C return reaches the trusted OSTD completion gate, not the interrupted instruction.
}
```

`level::enter`, from `ostd/src/irq/level.rs`, temporarily records L1 with the interrupted user/kernel mode and restores the level on normal return.
The internal Task stays current; callback execution does not select a worker or call `task_switch`.
`bottom_half::process` uses the [virtual-guard adaptation](interrupts-and-time.md#worker-delivery). Wall-time work follows its [separate deferred path](interrupts-and-time.md#wall-time-deadlines).

### 3. vOSTD invokes the registered callbacks {#tick-callbacks}

The following is the required vOSTD adaptation of `ostd/src/timer/mod.rs::call_timer_callback_functions`.
The callback iteration is retained. This private vOSTD variant omits the unused TrapFrame parameter; the native build retains its IRQ-callback signature.
The native BSP increment of `jiffies::ELAPSED` is also omitted: vOSTD reads elapsed wall time from Host's clock page.

```rust
// Proposed vOSTD body in ostd/src/timer/mod.rs; native builds keep their body.
#[cfg(feature = "kernelet")]
pub(crate) fn call_timer_callback_functions() {
    let irq_guard = irq::disable_local(); // Virtual guard.
    let callbacks_guard = INTERRUPT_CALLBACKS.get_with(&irq_guard);
    for callback in callbacks_guard.borrow().iter() {
        (callback)(); // Kernelet-local callbacks registered during initialization.
    }
    drop(callbacks_guard);
}
```

For a POSIX thread, the registered `update_cpu_time` callback reads vOSTD's current internal Thread and the virtual interrupt level established above.
Its existing accounting core is:

```rust
// Existing kernel/core/src/process/process/timer_manager.rs::update_cpu_time,
// excerpt after obtaining the current POSIX thread and its Process.
if is_kernel_interrupted {
    posix_thread.prof_clock().kernel_clock().add_jiffies(1);
    process.prof_clock().kernel_clock().add_jiffies(1);
    charge_cpu_time(&process, CpuStatKind::System);
} else {
    posix_thread.prof_clock().user_clock().add_jiffies(1);
    process.prof_clock().user_clock().add_jiffies(1);
    charge_cpu_time(&process, CpuStatKind::User);
    // Existing user CPU-timer expiry processing follows.
}
```

The callback skips non-POSIX threads and also processes CPU-timer expirations in its full native body.
The scheduler callback registered in step 1 separately updates the internal run queue and may set vOSTD's preemption request.
These callbacks operate on the internal Task; Host CFS has already handled the vCPU Thread through Host's own callback list.

### 4. Finish the tick and continue internal execution {#tick-return}

After the vOSTD body returns, the machine return gate must resume the saved vCPU-loop call and identify this tick as completed.
In the normal case with no internal switch requested, the loop restores the original interrupted state and continues the same internal Task.
An internal preemption request needs an eligible vOSTD scheduling checkpoint after tick completion; releasing the virtual guard alone does not invoke `might_preempt`.
**[unverified]** The existing `enter_internal` saves a loop return, but its Task/interrupt restore branches do not construct the tick call frame or completion path.
A plain `(image.entries.tick)(interrupted_mode)` call on the loop's stack does not supply those operations.
The entry must prepare a bounded call frame below the interrupted Task's live stack frames, retain the original snapshot and provide a trusted return to the loop.
An IRQ during tick handling needs storage for the newly interrupted tick handler as well as the original Task snapshot; one overwriteable `pause_frame` cannot hold both.
The tick completion and subsequent internal scheduling checkpoint still need to be wired into the Task chapter's return path.
Failure handling remains to be specified in the separate review of [Faults](../faults-and-reclamation.md).

Host may deschedule the vCPU before delivery; that does not change the internal Task identity or earn CPU time for the paused interval.
Host services and partial internal transfers require phase-aware handling: a tick there is not automatically an internal Task sample.
Host scheduler execution intervals remain distinct from the vOSTD tick-based Thread/Process clocks; see [Accounting](tasks.md#accounting).
A tick is not interchangeable with a wall-clock deadline notification for an idle vCPU.

### Keeping the timer callback path

vOSTD retains the timer callback registration path, including CPU-clock updates based on the interrupted current Task.
Device workers and ordinary wall-time work must not replay these CPU-clock callbacks while another Task is current.
Audit `time/cpu_time_stats.rs`, `process/process/timer_manager.rs`, `sched/stats/scheduler_stats.rs` and `time/softirq.rs` against the preserved Task identity and interrupted mode; source reuse is a goal, not a verified claim that every callback is unchanged.

**[unverified]** Define and test deferred delivery under virtual guards and transitions, duplicate/coalesced samples, ticks during Host services or virtual handlers, idle wall-time notifications and final exit ordering.
These cases determine when the scalar callback entry is legal; its signature alone does not settle them.

## Monotonic time and deadlines {#wall-time-deadlines}

`Jiffies::elapsed` reads the host-wide clock page; time advances while a kernelet is paused.
The source-tree tick rate is 1000 Hz. That is a source observation, not an internal timer-delivery latency guarantee.
A wall-clock deadline and a per-task CPU-time sample are different inputs and cannot be exchanged.

The current system-wide timer managers share their `Arc<TimerManager>` across CPU-local slots (`kernel/core/src/time/clocks/system_wide.rs`); the jiffies manager is also shared.
Use vCPU 0 as the designated processor for those shared wall-time queues and the sole publisher of their earliest deadline through `timer_arm(0, deadline_ns)`.
Other vCPUs arm/cancel logical timers in the shared queues and notify vCPU 0 to recompute the minimum; they must not overwrite its Host deadline with stale minima.
If a future timer queue has a different vCPU owner, it needs its own aggregator and notification target; the service already accepts a vCPU ID.
The host replaces or cancels the one-shot deadline; `u64::MAX` cancels, an elapsed deadline immediately publishes TIME_RECHECK, and a future one fires at the first host tick at or after it.
Cancellation does not retract an already published hint; consumers recheck the logical timer's validity and monotonic time.

The proposed wall-time path marks `TIME_RECHECK` pending and advances shared timer queues through the vCPU 0 worker's ordinary L0 work, outside device-callback guards.
This requires adapting `kernel/core/src/time/softirq.rs`: native timer registration raises `TIMER_SOFTIRQ_ID`, whose bottom half processes expired timers. That native callback cannot be reused unchanged while claiming timer/destructor work has moved to L0.
The vOSTD adaptation must leave wall-time work pending for the designated worker and keep CPU-clock callbacks on the interrupted Task; it remains part of the tick callback audit.
Long work can be queued to other ordinary internal Threads; Host interrupt handlers only publish notifications.
Unfinished work remains discoverable by idle checks.
Without deadline aggregation, Host posts wall-time recheck events to vCPU 0 at `policy.idle_tick_hz`, 100 Hz by default (chosen), waking that vCPU Thread if idle.
This fallback relies on the shared queues and designated processor above; it is not a per-vCPU CPU-clock sample.
A value of zero disables these periodic events; timers then wait for another event to wake the kernelet.
The one-shot deadline mechanism is the optional extension toward tickless idle. The idle tick rate bounds the posting interval, while delivery also depends on host scheduling and internal checkpoints.
It must not count as execution of a sleeping task.

## What a tenant sees

- Timer expiry follows the host clock, but delivery waits for the vCPU to run and reach an internal checkpoint. `idle_tick_hz` sets the periodic posting rate for an idle kernelet; it is not a total latency bound.
- `times(2)`, `getrusage` and process CPU timers use samples attributed to the task and mode that incurred them. Consuming an event on another task does not move that charge to the consumer. Host-descheduled time is not task execution time.
- `/proc/stat` requires separate internal user, system and idle accounting; the host scheduler's vCPU Thread statistics cannot supply those categories unchanged.
- `/proc/interrupts` shows virtual device lines on their assigned vCPU. Unbound devices use virtual CPU 0. Device interrupt latency includes host notification, vCPU scheduling and internal delivery.

## Costs

- Per virtual interrupt: host pending-state publication, a possible vCPU wakeup, internal delivery and the handler. The virtio handler still performs the two register accesses needed to read and acknowledge device status; the host no longer schedules a separate worker thread.
- Per virtual CPU tick: Host event capture and vOSTD delivery on the interrupted Task. Entry and callback costs remain to be measured.
- Per idle kernelet: periodic timer notifications at `idle_tick_hz` when enabled, or deadline-driven notifications with the optional one-shot mechanism.
- Per virtual CPU: one internal worker stack and execution record, alongside the idle and vCPU Thread resources counted in [Tasks](tasks.md#obligations).
- Virtual guard operations update internal state. Physical pause adds the separate machine-state capture and restoration cost.

## What this page decides

- **Virtual device interrupts are fetched by internal per-vCPU workers** (registers D9 and D11); virtual ticks use the interrupted Task under D66. Host interrupt handlers never call kernelet callbacks.
- **`InterruptLevel` describes the virtual delivery context** (register D18); virtual ticks preserve the interrupted Task and user/kernel mode.
- **Idle tick frequency remains configurable** (register D19), with one-shot deadlines as the extension toward tickless idle.
- **Virtual ticks run with the interrupted Task current before ordinary resumption or internal switching** (D66, revised 2026-09-15). The entry and deferral implementation remains pending.

## Verification boundary

The host may pause a virtual guard without moving internal current, but this requires the new physical capture/return mechanism.
The full interruption chain, callback assumptions, stable clock domain, tick delivery on the interrupted Task and full-mailbox discovery have not been verified by the old ABI probes.
A physical interrupt storm also has no time bound merely because a fixed number of IRQ storage slots is allocated.
