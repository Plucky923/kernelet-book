# User mode

*Part of question 2. Virtualizes `user` and the fallible half of `mm::io`: the ring transition, system-call and exception return, and page faults taken in kernel mode by user copies. Discharges invariant I5 for the fixup path and invariant I3's backstop: a kernelet's fault ends the kernelet, not the machine.*

The tenant's processes run in user mode under the kernelet's own page tables. The instructions that switch rings, the `sysret` and `iretq` on the way out and the `syscall` and trap entries on the way in, belong to the host: the syscall MSRs and the interrupt descriptor table are the host's, and the entry code that lands on the kernel stack is host text. So the round trip into user mode is a service call, and everything around it, the loop, the hooks, the handling of what came back, stays in the kernelet where the kernel proper expects it.

## The round trip

`UserMode::execute` keeps the shape of OSTD's loop (checked on the tree: `ostd/src/arch/x86/cpu/context/mod.rs`); what moves into the host, behind `user_run`, is the ring transition, the interrupt dispatch, and the classification of what came back:

```rust
// vOSTD, the virtualized `UserContextApiInternal::execute`
fn execute<T: UserModeHooks>(&mut self, hooks: &T) -> ReturnReason {
    self.user_context.general.rflags |= (RFlags::INTERRUPT_FLAG | RFlags::ID).bits() as usize;
    loop {
        crate::task::scheduler::might_preempt();          // reads NEED_RESCHED from the mirror; yields
        let guard = crate::irq::disable_local();          // the aliased guard: preempt_count += 1
        hooks.pre_user_run(&guard);                       // the kernel loads FS base, GS base, FPU into the CPU
        let why = services().user_run(&mut self.user_context as *mut RawUserContext);
        let exception = (why == 1).then(|| CpuException::from_raw(     // built before the guard drops
            self.user_context.trap_num, self.user_context.error_code, self.user_context.fault_addr));
        drop(guard);
        match why {
            0 => return ReturnReason::UserSyscall,
            1 => { self.exception = exception; return ReturnReason::UserException; }
            _ => {}                                       // an interrupt the host handled; loop
        }
        if hooks.has_kernel_event() { return ReturnReason::KernelEvent; }
    }
}
```

The preemption count is raised from `pre_user_run` through `user_run` so that the state the kernel's hook loaded into the CPU, the thread's FS and GS bases and its FPU registers (checked on the tree: `kernel/core/src/process/posix_thread/thread_local.rs`), is still in the CPU when the host performs the transition; the host does not move a task whose count is nonzero ([Tasks](tasks.md)), and `user_run` is not a sleeping call, so the service prologue accepts it under the count. The count stays nonzero for the whole residence in user mode, seconds for a CPU-bound tenant; that is not a preemption-off violation, because `policy.preempt_off_ticks` counts only ticks taken in kernel mode in `KW_TEXT` at `preempt_switch` ([Tasks](tasks.md)), and ring-3 time slicing happens through `user_run` returning `2`.

`RawUserContext` is `#[repr(C)]` in `ostd::kernelet::abi`, with the general registers, the instruction and stack pointers, the flags, and three fields the transition fills in: the trap number, the error code, and the fault address (CR2) of a user-mode page fault, read by the host before it re-enables interrupts, so that the kernelet never reads CR2 after a point at which it may have migrated. Under the feature the trap module's own `RawUserContext`, which is `pub(super)` on the tree (`ostd/src/arch/x86/trap/mod.rs`), is a re-export of the ABI type, so that `UserContext.user_context` is that type and the pointer cast above is a cast between one type. The context is a local of the task's entry closure on the task's own kernel stack (checked on the tree: `kernel/core/src/thread/task.rs`), and `user_run` **requires** it there: the host's entry stubs use the context as a stack (`_trap_from_user` and `syscall_entry` load `rsp` from it and push onto it; `syscall_return` pops from it; checked on the tree: `trap.S`, `syscall.S`), with interrupts disabled and no exception-table entry, so a context on a window page the kernelet could unmap would fault with `rsp` in that page, a double fault. The kernel stack is host memory the kernelet cannot unmap. The check is two comparisons: above the stack pointer at entry plus the host's own frame headroom, so that the context overlaps no host frame, and below the stack's top; a context anywhere else is `-INVALID`. That is the one place the service half's general pointer rule is narrowed, and the [service half](../kernelet-api-service.md) says so.

On the host side, `user_run` does what `RawUserContext::run` and the interrupt arm of `execute` do today (checked on the tree: `ostd/src/arch/x86/trap/syscall.rs`, `context/mod.rs`): it takes its own interrupt guard, re-checks `dying` on the info page under that guard and immediately before `syscall_return`, so that a kill's reschedule interrupt cannot be consumed in the prologue and the task then enter user mode for up to a tick; it loads the user register file and returns to user mode. Whatever brings the CPU back, a `syscall`, an exception, or an interrupt, lands in the host's entry code on the task's kernel stack, which stores the user registers back into the context. `user_run` then classifies:

- A system call: re-enable interrupts and return `0`. The kernel proper handles it from its own loop, identically.
- A fault or trap in user mode (`is_fault_or_trap` on the tree): record the trap number, error code and CR2, re-enable interrupts, return `1`. The kernelet `take_exception`s it identically.
- An NMI or a machine check taken in user mode: these are the machine's, not the tenant's; `user_run` handles them as the host handles them (on the tree the loop panics on them; here the host's own handlers run) and returns `2`.
- An interrupt: run the host's top half and bottom halves for it on the current task, exactly the tree's `call_irq_callback_functions` arm, re-enable interrupts, return `2`.

Interrupts are re-enabled inside `user_run` before it returns, on every path, because the kernelet cannot do it (`enable_local` is absent): kernelet code runs with the interrupt flag set, always. The fourth arm means that a physical interrupt landing while a tenant is in user mode is served on the tenant's task, at service-call depth one, with the kernelet's page table active and the host's `Task::current()` naming a kernelet task. That is what happens to a guest under a hypervisor too, and it has two consequences the design states rather than hides: the interrupt's CPU time is charged to the kernelet, which qualifies invariant I6 ([Boundaries and trust](../principles.md)); and the host's interrupt-path code, including its tick callbacks, must tolerate a current task that is not one of the host kernel proper's threads. On the tree `Task::as_thread` returns `None` for such a task (checked: `kernel/core/src/thread/mod.rs`) and the tick statistics take that path, but **[unverified]** (register A12): that every host top half and bottom half on the tree is correct with a kernelet task current. The host does not switch tasks inside `user_run`; before returning `2` it copies the need-preempt decision its own tick made inside the interrupt into the task's `NEED_RESCHED` flag and mirror, so that the kernelet yields at `might_preempt` at the top of the next iteration ([Tasks](tasks.md), register D17); the loop top is also where a pending tick is consumed ([Interrupts and time](interrupts-and-time.md)).

**Killing a task in user mode.** A task in user mode is inside `user_run` and will reach the epilogue, where a dying kernelet's task is terminated, only when it comes back. the reaper therefore sends a reschedule interrupt to every host CPU on which a task of the kernelet is running, as the host already does to enforce `need_preempt` remotely (checked on the tree: `ostd/src/task/scheduler/mod.rs`, the inter-processor call); the interrupt returns `user_run` with `2`, and the epilogue terminates the task. The re-check before `syscall_return` closes the window in which the interrupt lands inside `user_run` itself. The cost is one interrupt per running task per kill.

`UserContextApi`, `UserModeHooks`, `ReturnReason`, `UserContext` and its register types, `CpuException`, `FpuContext` and `FsBase` are identical, as the [taxonomy](index.md) lists; `CpuException::from_raw` is the one addition, the tree's `new` with the fault address as an argument instead of a CR2 read. `GsBase` is virtualized to a direct MSR access, since its `swapgs` bracket is safe only with interrupts really disabled.

## Page faults in user copies

The kernel proper reads and writes user memory through `VmReader` and `VmWriter` with `Fallible`, whose copy routines carry exception-table entries: a fault inside one is resolved by the host's page-fault handler, which first asks the kernel's injected user-page-fault handler to map the page (demand paging, copy-on-write) and, if that fails, jumps to the routine's recovery address so that the copy reports how far it got (checked on the tree: `ostd/src/mm/fault/mod.rs`, `handle_user_page_fault`). Inside a kernelet the copy routines are the same code, in `KW_TEXT`, with their entries in the image's own exception table, whose bounds the entry table gives the host.

**This rests on a host property the API does not state, and a second host is what found it.** Dereferencing a user address from kernel mode is allowed only because Asterinas leaves the processor's supervisor-access check disabled: its control-register setup enables five features and not that one, and nothing in the tree emits the instructions that would open a window in it. The check is a hardware feature present on every processor since Broadwell and Zen, and a host that enables it — Linux does, on every machine that has it — refuses the access outright, before any page-table walk and before any exception table is consulted. So this paragraph's mechanism is not a property of the API but of the host beneath it, and vOSTD's contract has to say which. On such a host the tenant's own address may not be dereferenced at all, and the memory must be reached through the linear-map alias of the frame instead, which is what [Design for Linux](../../linux-mode/virtualizing-ostd/memory.md#copies) specifies and measures (register D82). That is a change inside these copy routines, which are OSTD's; the kernel proper still calls `VmReader` and `VmWriter` and its source does not change. What changes is who resolves the fault, because the host cannot call the kernelet's fault handler: that would enter the kernelet image from the host's fault path.

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
- An NMI or machine check in user mode is the machine's event, handled by the host; the process sees nothing.
- A `kill` of the sandbox reaches a process in user mode within one interrupt delivery.
- Time: each round trip pays a service prologue and epilogue on top of the transition, and each first-touch fault inside a user copy pays one more return-and-call than on the host kernel.

## Costs

- Per round trip into user mode: the crossing's prologue and epilogue, *estimated* at 25 cycles, plus two stores to the shared task record for the preemption count and two `dying` loads, on top of the transition itself; the booted prototype measured a cold page fault at 10,345 cycles in its guest against 9,791 in Asterinas itself (see the Paper's Evaluation section), which bounds what this design should expect for a whole fault.
- Per interrupt landing in user mode: the host's top and bottom halves, on the tenant's task; the same work as today, charged differently.
- Per first-touch fault in a user copy, *estimated*, to be measured: the host's handler with its CPU-slot and depth checks; the exception-table lookup, which on the tree is a linear scan of the image's table (checked: `ostd/src/mm/fault/ex_table.rs`) and now runs on every first-touch fault rather than only on a failed one; two task-record writes; the `iretq` to the recovery address and the routine's return; the kernelet's handler call; and the re-entry into the copy. About two returns and one indirect call more than the host kernel's path, plus the scan.
- Per kill of a kernelet with tasks in user mode: one reschedule interrupt per such task.

## What this page decides

- **The ring transition is a service call and the loop stays in the kernelet** (register D20). The alternative, moving the whole `execute` loop into the host, would make the kernel's `UserModeHooks` closures cross the boundary, against invariant I5, and would put the syscall dispatch path's first instructions on the wrong side of the crossing. What does move into `user_run` is the classification and the interrupt arm, since only host code can dispatch a physical interrupt or read CR2 safely.
- **Kernel-mode faults in user copies are resolved by fixup and retry, never by calling into the kernelet from the fault path** (register D21). The alternative, the host calling the kernelet's injected handler as it calls its own, breaks the entry rule and would run kernelet code on the host's fault path with the host's state.
- **A kernelet's own kernel-mode fault kills the kernelet** (register D22), not the machine, which is invariant I3's backstop made concrete: a stray pointer in vOSTD's own code, or in kernelet code at a window address it unmapped, ends one sandbox.
- **The user context lives on the task's kernel stack, and `user_run` checks that** (register D47). The alternative, a host-private copy of the context per task copied in and out, costs two copies of about 160 bytes (the size of the tree's `RawUserContext`) per round trip on the hottest path in the system; the stack is already host memory, and the check is one comparison.
