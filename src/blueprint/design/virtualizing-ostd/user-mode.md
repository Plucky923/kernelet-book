# User mode

*Part of question 2. Virtualizes `user` and the fallible half of `mm::io`: the ring transition, system-call and exception return, and page faults taken in kernel mode by user copies. Discharges invariant I5 for the fixup path and invariant I3's backstop: a kernelet's fault ends the kernelet, not the machine.*

The tenant's processes run in user mode under the kernelet's own page tables. The instructions that switch rings, the `sysret` and `iretq` on the way out and the `syscall` and trap entries on the way in, belong to the host: the syscall MSRs and the interrupt descriptor table are the host's, and the entry code that lands on the kernel stack is host text. So the round trip into user mode is a service call, and everything around it, the loop, the hooks, the handling of what came back, stays in the kernelet where the kernel proper expects it.

## The round trip {#user-round-trip}

### Start from the internal Task

The [task entry path](tasks.md#task-execution) has already restored this internal Task's kernel stack, installed vOSTD current and invoked its closure. For a user thread, `kernel/core/src/thread/task.rs::create_new_user_task` constructs that closure with TaskOptions and stores the ThreadLocal in the local Task. It then creates `UserMode::new(user_ctx)` on the running Task's stack.
The following are the relevant existing kernel calls; syscall tracing, signal handling and process-stop handling remain around them in that file:

```rust
// Kernel: the existing return branches, with tracing bookkeeping omitted.
let return_reason = user_mode.execute(&ctx);
let user_ctx = user_mode.context_mut();
match return_reason {
    ReturnReason::UserSyscall => {
        // The full source also performs ptrace checks before and after this call.
        handle_syscall(&ctx, user_ctx);
    }
    ReturnReason::UserException => {
        let exception = user_ctx.take_exception().unwrap();
        handle_exception(&ctx, user_ctx, exception);
    }
    ReturnReason::KernelEvent => {
        // Continue to the existing termination/signal checks after the match.
    }
}
```

A process's syscall returns to this same local loop; Host OSTD does not dispatch the tenant's Linux syscall or create another thread scheduled by Host. A later internal switch suspends this whole call chain on its existing stack.

### Keep the hook and loop inside the image

`UserMode::execute` keeps the shape of OSTD's loop (checked on the tree: `ostd/src/arch/x86/cpu/context/mod.rs`); what moves into the host, behind `user_run`, is the ring transition, the interrupt dispatch, and the classification of what came back. The vOSTD implementation replaces the native `self.user_context.run(guard)` and physical IRQ arm with the service call below. The return constants are defined in the service ABI; a service failure stops the instance rather than masquerading as a user exception:

```rust
// vOSTD, the virtualized `UserContextApiInternal::execute`
fn execute<T: UserModeHooks>(&mut self, hooks: &T) -> ReturnReason {
    self.user_context.general.rflags |= (RFlags::INTERRUPT_FLAG | RFlags::ID).bits() as usize;
    loop {
        crate::task::scheduler::might_preempt();          // honor an eligible internal reschedule request
        let guard = crate::irq::disable_local();          // exclude internal switching and virtual delivery
        hooks.pre_user_run(&guard);                       // restore this thread's supplementary state if needed
        let why = (service_table().user_run)(&mut self.user_context as *mut RawUserContext);
        if !matches!(why, USER_RETURN_SYSCALL | USER_RETURN_EXCEPTION | USER_RETURN_INTERRUPT) {
            crate::panic::abort();
        }
        let exception = if why == USER_RETURN_EXCEPTION {
            Some(CpuException::from_raw(
                self.user_context.trap_num,
                self.user_context.error_code,
                self.user_context.fault_addr,
            ).unwrap_or_else(|| crate::panic::abort()))
        } else {
            None
        };
        drop(guard);
        match why {
            USER_RETURN_SYSCALL => return ReturnReason::UserSyscall,
            USER_RETURN_EXCEPTION => { self.exception = exception; return ReturnReason::UserException; }
            _ => {}                                       // an interrupt the host handled; loop
        }
        if hooks.has_kernel_event() { return ReturnReason::KernelEvent; }
    }
}
```

The hook is the existing kernel implementation, registered by passing `&ctx` to execute; no hook registration or function pointer crosses the service table:

```rust
// Kernel, thread/task.rs: method of impl UserModeHooks for Context<'_>.
fn pre_user_run(&self, guard: &DisabledLocalIrqGuard) {
    self.thread_local.supp_user_context().before_user_exec(guard);
}

// Kernel, process/posix_thread/cpu_sync.rs: existing CpuSync method.
pub(super) fn before_user_exec(&self, guard: &DisabledLocalIrqGuard) {
    if self.location.get() == CanonicalValueLocation::InMemory {
        self.reg.borrow().restore_to_cpu_with_irq_disabled(guard);
    }
    self.location.set(CanonicalValueLocation::OnCpu);
}
```

SuppUserContext calls that method for FPU, FS and user GS.
For each supplementary register group, CpuSync restores the memory value only when the canonical location is InMemory, then marks it OnCpu.
It prepares entry to user mode; task-switch pre instead saves outgoing state, and task-switch post updates statistics and the address space.
The hook does not prevent host preemption or replace host snapshot handling.

The [virtual guard](synchronization.md#virtual-guards) covers `pre_user_run` through `user_run` to prevent an internal task switch or virtual callback from consuming another task's state.
It does not prevent a host pause. The [machine-state protocol](interrupts-and-time.md#host-saved-state) saves actual FS/user-GS/FPU and the exact continuation before host code can overwrite them, including an interruption midway through the hook.
The host preserves the active internal executor while a user_run service is outstanding.
A tick that samples user execution during `user_run` must be delivered after the virtual guard drops and before the next internal selection or ordinary continuation, preserving that sample's user mode.
The whole round trip is not user execution: a tick in `pre_user_run` samples internal kernel code, while a tick in Host service code requires the separate phase rules in the [tick protocol](interrupts-and-time.md#tick-return).
That insertion is still missing from the `execute` excerpt above; its guard drop and next `might_preempt` do not implement the tick gate by themselves ([tick return](interrupts-and-time.md#tick-return)).
Host preemption and internal preemption requests remain distinct; a virtual guard does not prevent Host from preempting the vCPU Thread.

### Let Host assembly enter and leave user mode {#user-context-stack}

Native `RawUserContext` contains GeneralRegs, trap_num and error_code (`ostd/src/arch/x86/trap/mod.rs`). The kernelet ABI preserves that prefix and appends fault_addr, captured from CR2 by Host entry before handlers can overwrite it:

```rust
// Proposed ostd::kernelet::abi, used by both sides of user_run.
#[repr(C)]
pub struct RawUserContext {
    pub general: GeneralRegs, // Same repr(C) field order as the native x86 type.
    pub trap_num: usize,
    pub error_code: usize,
    pub fault_addr: usize,
}
```

Both the native transition used by the Host service and vOSTD must use this layout. Appending a field does not change the existing prefix offsets, but the assembly offsets, type alignment and complete size still require checks against both builds. `CpuException::from_raw` is the proposed counterpart of native CpuException::new, returning `Option<CpuException>`: it constructs all supported native exception variants from the trap number and error code; only the page-fault branch substitutes captured fault_addr for a new CR2 read.

The native x86 instructions show how a Rust call can run user instructions and later return (`ostd/src/arch/x86/trap/syscall.S`; register pushes/pops and branch bodies are omitted here):

```asm
# Host syscall_return(regs): rdi is the validated RawUserContext pointer.
push rdi
mov gs:4, rsp       # TSS.sp0 remembers this kernel return stack.
mov rsp, rdi        # Pop the user register values from RawUserContext.
swapgs              # Host transition installs user GS; vOSTD never does this.
# Restore user registers, then the native sysret/iret branch enters ring 3.

# A subsequent syscall reaches Host syscall_entry through the Host LSTAR MSR.
swapgs              # Restore Host GS before accessing TSS fields.
mov gs:12, rsp      # Save user SP in TSS.sp1 scratch.
mov rsp, gs:4       # Find the saved kernel return stack.
pop rsp             # Recover the RawUserContext address saved above.
# Save user registers into that context; restore kernel callee registers.
# The final ret returns to the Rust caller of syscall_return.
```

GeneralRegs is the user register file; TaskContext is the suspended kernel call's callee-register context. They serve different transitions. The active resource's opaque pointer identifies the kernel stack/context; the separately borrowed RawUserContext pointer tells user_run where this invocation's user registers live.
While ring 3 executes, active GS is the process's user GS. Host syscall/trap entry restores Host GS before CPU-local access; vOSTD resumes only after that restoration. The [replica-address helper](tasks.md#cpu-local-address) is therefore a kernel-mode operation and is never used under user GS.

The selected process page-table root was activated by the internal post handler through pt_activate. It maps both the process's user pages and the Host-controlled kernel stack. User return keeps that process root; it changes privilege and registers, not which internal Task owns the vCPU.

Because the assembly uses RawUserContext as a stack, user_run requires it to lie in the active Task's Host-controlled stack, not merely in a mapped window page. The shared ABI does not permit an arbitrary writable buffer to become a return stack. Host entry validates the whole range before dereferencing it:

```rust
// Host OSTD: arithmetic check only. Stack/root ownership is already retained.
// entry_sp is captured by the service assembly before its Rust prologue.
fn context_fits_stack(
    context: *mut RawUserContext,
    entry_sp: usize,
    stack_bottom: usize,
    stack_top: usize, // Exclusive mapped end; excludes guard pages.
    host_reserve: usize, // Audited maximum downward stack use by Host entry/service code.
) -> bool {
    let start = context.addr();
    let Some(end) = start.checked_add(size_of::<RawUserContext>()) else { return false; };
    let Some(lowest_host_sp) = entry_sp.checked_sub(host_reserve) else { return false; };
    let Some(caller_frame_start) = entry_sp.checked_add(size_of::<usize>()) else { return false; };
    stack_bottom <= lowest_host_sp && entry_sp < stack_top
        && start % align_of::<RawUserContext>() == 0
        && caller_frame_start <= start && end <= stack_top
}
```

The bounds come from the current vCPU's retained active TaskExecRecord, never from arguments supplied by the kernelet. The vOSTD call borrows its local UserContext exclusively through the complete round trip. The context must lie above the service call's return-address slot, while the Host reserve is checked below entry_sp because the stack grows downward.
Native `syscall.S` temporarily uses the context for register pops/pushes within its validated range and keeps the service return stack separately in TSS.sp0.
The reserve value still needs an audited stack-depth bound; the arithmetic check alone does not prove NMI safety. A failure returns -INVALID without touching the context. The gate, complete headroom bound and hostile register-return paths remain **[unverified]**.

On the host side, `user_run` does what `RawUserContext::run` and the interrupt arm of `execute` do today (checked on the tree: `ostd/src/arch/x86/trap/syscall.rs`, `ostd/src/arch/x86/cpu/context/mod.rs`): it takes its own interrupt guard, re-checks the instance's authoritative Host stop state under that guard and immediately before `syscall_return`, so that a kill's reschedule interrupt cannot be consumed in the prologue and the vCPU then enter user mode until a later interrupt; it loads the user register file and returns to user mode. Whatever brings the CPU back, a `syscall`, an exception, or an interrupt, lands in the host's entry code on the task's kernel stack, which stores the user registers back into the context. `user_run` then classifies the result. The native run method consumes a physical IRQ guard:

```rust
// Existing Host OSTD, arch/x86/trap/syscall.rs.
pub(in crate::arch) fn run(&mut self, guard: DisabledLocalIrqGuard) {
    core::mem::forget(guard);
    unsafe { syscall_return(self) };
}
```

Here forgetting the guard leaves physical interrupts disabled until the native classification path explicitly enables them. vOSTD must not copy this forget operation: its virtual guard is dropped normally after the service returns. The Host service needs the additional stop check and supplementary-state entry/return machinery described above; calling this native method alone does not implement that machinery.

The reasons below are successful return values; a negative result denotes a service error, not a user exception:

- A system call: re-enable interrupts and return `USER_RETURN_SYSCALL`. The kernel proper handles it from its own loop, identically.
- A fault or trap in user mode (`is_fault_or_trap` on the tree): record the trap number, error code and CR2, re-enable interrupts, return `USER_RETURN_EXCEPTION`. The kernelet `take_exception`s it identically.
- An NMI or a machine check taken in user mode remains Host policy. The current native execute loop panics for an exception outside its fault/trap cases; it does not supply a generic recover-and-return implementation. Only a Host handler that establishes recovery may return USER_RETURN_INTERRUPT; a fatal machine event can still stop the machine. This part of the new service is **[unverified]**.
- An interrupt: run the host's top half and bottom halves for it on the current task, exactly the tree's `call_irq_callback_functions` arm, re-enable interrupts, return `USER_RETURN_INTERRUPT`.

Interrupts are re-enabled inside `user_run` on every return to kernelet code; the kernelet cannot do it itself.
The host's current is the vCPU Thread, while the active internal handle identifies the user thread for private root/state/accounting records.
Physical IRQ handling must tolerate the vCPU Thread without a host user ThreadLocal. **[unverified]** (A12): every reachable host top/bottom half meets this context contract.
The host first protects new user output, including supplementary state, before running code that may overwrite it.
A rejected entry restores the service input; a completed user round trip restores only its new output, never old input over that output. For example, if user code changes XMM0 after entry, the output snapshot must retain that new value before a Host IRQ handler uses SIMD. Restoring the pre-entry value would silently undo the user computation. CpuSync may still describe the canonical state as OnCpu, so return must make that new state available to the local switch hook.

The TSS return-stack fields are also part of the transition contract. syscall_return writes TSS.sp0, and syscall_entry temporarily uses TSS.sp1. Another thread scheduled by Host can overwrite them while this vCPU is paused. Before resuming a continuation that still depends on those fields, Host must reinstall this user_run invocation's saved return-stack/scratch state, together with its root and registers. Native assembly alone does not provide the per-vCPU ownership and partial-entry/NMI analysis required here.

The service does not select another internal task. An IRQ return produces USER_RETURN_INTERRUPT and the wrapper reaches an internal scheduling/event checkpoint; internal timer/reschedule decisions use the active task's attributed observations.
Host outer scheduling follows its own legal continuation rules and resumes the same user_run service or its classified output return.
It does not convert a host NEED_RESCHED flag directly into permission to run another internal task while that service is live.

**Killing a task in user mode.** A task in user mode is inside `user_run` and will reach the epilogue, where a dying kernelet's task is terminated, only when it comes back. The reaper therefore sends a reschedule interrupt to every host CPU on which a vCPU Thread of the kernelet is running, as the host already does to enforce `need_preempt` remotely (checked on the tree: `ostd/src/task/scheduler/mod.rs`, the inter-processor call); the interrupt returns `user_run` with `USER_RETURN_INTERRUPT`, and the phase-aware epilogue cleans host obligations and stops the vCPU Thread without returning to kernelet code. The re-check before `syscall_return` closes the window in which the interrupt lands inside `user_run` itself. The request costs one reschedule interrupt per running vCPU Thread, regardless of its internal task count.

The user-context value types and hook interface retain their roles. `CpuException::from_raw` supplies the captured fault address instead of reading CR2 later. FS/FPU operations require the preservation and validated-buffer contract in [Host interruption](interrupts-and-time.md#host-saved-state); user-GS access uses the fixed inactive MSR so that it does not temporarily replace the active host GS base. These operation contracts must be adapted even where the public value type remains the same.

## Page faults in user copies {#user-copy-fault}

> **To be written.** Adapt the earlier fault-handling design below to internal Tasks during the separate fault/reclamation discussion.

The kernel proper reads and writes user memory through `VmReader` and `VmWriter` with `Fallible`, whose copy routines carry exception-table entries: a fault inside one is resolved by the host's page-fault handler, which first asks the kernel's injected user-page-fault handler to map the page (demand paging, copy-on-write) and, if that fails, jumps to the routine's recovery address so that the copy reports how far it got (checked on the tree: `ostd/src/mm/fault/mod.rs`, `handle_user_page_fault`). Inside a kernelet the copy routines are the same code, in `KW_TEXT`, with their entries in the image's own exception table, whose bounds the entry table gives the host. What changes is who resolves the fault, because the host cannot call the kernelet's fault handler: that would enter the kernelet image from the host's fault path.

The host build's page-fault handler branches first on the CPU slot: if it names a kernelet, the tree's path, which panics on any address outside the user half before consulting the table and calls the host's injected handler first (checked on the tree: `handle_user_page_fault`; the host kernel proper's handler unwraps `Task::current().as_thread_local()`, which is not there on a kernelet task), is not taken. For a kernelet task the handler is **exception-table only**, at either depth and for any faulting address, kernel half included, since a host store or load through a window pointer the kernelet supplied may find the page unmapped ([service half](../kernelet-api-service.md)):

1. At depth zero, in kernelet text: if the faulting address is in the user half and the faulting instruction has an entry in the kernelet image's exception table, it writes the fault address and error code into the task's record (`fault_addr`, `fault_code`), sets the instruction pointer to the entry's recovery address, and returns. It validates the recovery address against `KW_TEXT`, which invariant I5 requires. Otherwise the fault is the kernelet's own bug or a stray pointer, at a window address, a host address, or a user address from code that made no provision for faulting: the host kills the kernelet with `KillReason::KernelFault { addr, ip }` and terminates the task there, since depth zero is a quiescent point. The machine continues.
2. At depth one, in host text: if the faulting instruction has an entry in the host's own exception table, the host's fallible copy recovers as today and the service call reports `-INVALID`; a fault in host text without one is a host bug and halts the machine, as today. The host's injected handler is never called for a kernelet task, at either depth.

The handler itself runs at depth zero in host text on the first path, and the tree's `trap_handler` re-enables interrupts inside it (checked: `enable_local_if(was_irq_enabled)`, always true for a kernelet task), so a kill's interrupt can land there. That is safe because `preempt_switch` applies to every return from the trap path into `KW_TEXT`, exceptions included, not only to interrupts' returns ([Tasks](tasks.md)): the handler's `iretq` to the recovery address is a switch point, and a dying kernelet's task is terminated there.

The kernelet's side of the first case is a retry loop in each of the virtualized fallible routines. The tree has four, each with its own exception-table entry and recovery label (checked: `__memcpy_fallible` and `__memset_fallible`, which return the count of bytes *not* done, and `__atomic_load_fallible` and `__atomic_cmpxchg_fallible`, which return `!0` on a fault; `ostd/src/arch/x86/mm/`, `ostd/src/mm/io/mod.rs`). The copy:

```rust
// vOSTD, ostd/src/mm/io/copy.rs under the feature
fn copy_from_user_fallible(dst: *mut u8, src: *const u8, len: usize) -> usize {
    let mut done = 0;
    loop {
        // The identical routine with its identical `ex_table` entry: `rep movsb` leaves rcx, rsi and
        // rdi advanced, so the routine's return is the bytes remaining and a restart at `done` is exact.
        let remaining = unsafe { memcpy_fallible(dst.add(done), src.add(done), len - done) };
        done = len - remaining;
        if done == len { return len; }
        let rec = current_task_record();
        let exception = CpuException::PageFault(RawPageFaultInfo {
            error_code: PageFaultErrorCode::from_bits(rec.fault_code.load(Relaxed) as usize).unwrap(),
            addr: rec.fault_addr.load(Relaxed) as usize,
        });
        match USER_PAGE_FAULT_HANDLER.get().expect("a page fault handler is missing")(&exception) {
            Ok(()) => continue,                              // the kernel mapped the page; resume where the copy stopped
            Err(()) => return done,                          // as today: a partial copy and `Error::PageFault`
        }
    }
}
```

`memset` is the same loop over its count. The two atomics loop on the sentinel: call the routine; if it returns `!0`, build the exception from the record, call the handler, and retry once the handler succeeds, or return `Err(PageFault)` when it fails; a successful retry is a fresh atomic access, which is what the `PodAtomic` callers, the futex paths, expect. This is the host kernel's behavior with one extra bounce: the host resolves the fault to the recovery address, the kernelet asks its own kernel to map the page, and the copy resumes. The injected handler is the kernel proper's, identical, and it runs on the faulting task, which is what it does on the host kernel too, only one call deeper. `VmReader::read_fallible` and the rest of the fallible API keep their code over these four routines; the `Infallible` cursors are untouched, since a kernel-space address that faults is the kill case.

**Faults in host code on window addresses.** The service half reads a kernelet's memory only through its three read-only pointer arguments, with fallible copies carrying the host's own exception-table entries, and writes through none, so a pointer that passed the range check but points at a page the kernelet unmapped becomes `-INVALID` rather than a host panic. The one host path that touches kernelet-supplied memory without a fixup is the entry-stub use of the user context above, which is why that context must be on the kernel stack.

## What a tenant sees

- System calls, signals, exceptions, demand paging and copy-on-write behave as on the host kernel.
- A physical interrupt that lands while a process is in user mode is served before the process's system call or return is handled, and its time is charged to the sandbox: latency and accounting a guest sees under a hypervisor too.
- NMI and machine-check handling remains Host policy; recovery is not promised for a fatal machine event.
- A `kill` requests an interrupt on each running vCPU Thread and completes host cleanup before any further guest return; no fixed stop-latency bound is established here.
- Time: each round trip pays a service prologue and epilogue on top of the transition, and each first-touch fault inside a user copy pays one more return-and-call than on the host kernel.

## Costs

- Per round trip into user mode: the service entry/return, state preservation, range validation and virtual guard operations, on top of the native ring transition. These costs have not been measured for this design; the booted prototype measured a cold page fault at 10,345 cycles in its guest against 9,791 in Asterinas itself (see the Paper's Evaluation section), an earlier-prototype comparison, not a bound or validation for the new service path.
- Per interrupt landing in user mode: the host's top and bottom halves, on the tenant's task; the same work as today, charged differently.
- Per first-touch fault in a user copy, *estimated*, to be measured: the host's handler with its CPU-slot and depth checks; the exception-table lookup, which on the tree is a linear scan of the image's table (checked: `ostd/src/mm/fault/ex_table.rs`) and now runs on every first-touch fault rather than only on a failed one; two task-record writes; the `iretq` to the recovery address and the routine's return; the kernelet's handler call; and the re-entry into the copy. About two returns and one indirect call more than the host kernel's path, plus the scan.
- Per kill of a kernelet with tasks in user mode: one reschedule interrupt per running vCPU Thread.

## What this page decides

- **The ring transition is a service call and the loop stays in the kernelet** (register D20). The alternative, moving the whole `execute` loop into the host, would make the kernel's `UserModeHooks` closures cross the boundary, against invariant I5, and would put the syscall dispatch path's first instructions on the wrong side of the crossing. What does move into `user_run` is the classification and the interrupt arm, since only host code can dispatch a physical interrupt or read CR2 safely.
- **Kernel-mode faults in user copies are resolved by fixup and retry, never by calling into the kernelet from the fault path** (register D21). The alternative, the host calling the kernelet's injected handler as it calls its own, breaks the entry rule and would run kernelet code on the host's fault path with the host's state.
- **A kernelet's own kernel-mode fault kills the kernelet** (register D22), not the machine, which is invariant I3's backstop made concrete: a stray pointer in vOSTD's own code, or in kernelet code at a window address it unmapped, ends one sandbox.
- **The user context lives on the task's kernel stack, and `user_run` checks that** (register D47). The alternative, a host-private copy of the context per task copied in and out, costs two copies of about 160 bytes (the size of the tree's `RawUserContext`) per round trip on the hottest path in the system; the stack is already host memory, and the complete context range, alignment and stack headroom must be validated.
