# Faults, termination, and reclamation

*Cuts across the resources. Specifies what ends a kernelet, how each of its tasks is stopped, and how everything it held is returned. Discharges invariant I7 (termination) in full and the destroy half of I4 (no retained reference).*

A kernelet dies in one of three ways: its kernel asks to (`stop` with `STOP_EXIT`), its kernel fails (`stop` with `STOP_PANIC`), or the host decides (`kill`). Whichever it is, the same thing then happens: the kernelet is marked dying, every one of its tasks is stopped at a point where it holds nothing of the host's, and the endovisor is told; when the last task has stopped the kernelet has exited, and `destroy` gives back its memory and every host object that named it. No code from the kernelet image runs on any of those paths after the mark, and no destructor of the kernelet's ever runs on the host's behalf.

## Three tiers

**Tier 1: the kernelet handles it.** A panic on one of the kernelet's threads unwinds to the catch the kernel proper wraps around every user task and kernel thread, the thread dies, and the kernelet continues; that is the kernel proper's own oops mechanism, which the host keeps switched off and the kernel proper's kernelet configuration switches on with one `cfg` line (checked on the tree: `kernel/core/src/thread/oops.rs`, `PANIC_ON_OOPS`; register D48). The only addition is that OSTD's `catch_unwind` reports the oops to the host with the `oops` service call, so that the endovisor's budget counts it ([The rest](virtualizing-ostd/the-rest.md)). A memory-allocation failure a fallible path can absorb, `ENOMEM` to a process, is tier 1 too ([Memory](virtualizing-ostd/memory.md)). Nothing on this tier involves the host beyond a counter.

**Tier 2: the kernelet ends.** Everything that ends the kernelet as a whole:

| cause | who detects it | where it is detected | `ExitReason` |
|---|---|---|---|
| the kernel calls `power::poweroff`, `restart` or `exit_with_code` | the kernelet | the `stop` service call with `STOP_EXIT`, task context | `Exited(code)` |
| a panic the oops handler cannot recover, or an allocation failure no fallible path absorbs | the kernelet | the `stop` service call with `STOP_PANIC`, task context | `Panicked(message)` |
| the endovisor asks | the endovisor | `Kernelet::kill(Requested)` or `HostPolicy(n)`, task context | `Killed(…)` |
| the oops budget is exhausted | the `oops` service call | task context | `Killed(OopsBudget)` |
| a task holds preemption off for `policy.preempt_off_ticks` ticks | the host tick | interrupt context, under the tick's interrupt guard | `Killed(PreemptOffTooLong)` |
| a service call finds the stack below the reserve | the service prologue | task context, on a stack already below the reserve | `Killed(StackReserve)` |
| a stack overflow in kernelet code | the double-fault handler on its own stack (assumption A6) | exception context, interrupts off | `Killed(StackOverflow)` |
| a page fault in kernelet code with no exception-table entry | the host's page-fault handler | exception context | `Killed(KernelFault { addr, ip })` |
| a panic in an endovisor hook on the kernelet's task | the service wrapper's `catch_unwind` | task context, on the hook stack | `Killed(HostHookPanicked)` |

**Tier 3: the host fails.** A panic in host code, in OSTD or in the endovisor's own threads, halts the machine as it does today; a panic in an endovisor hook on a kernelet's task is tier 2, since the service wrapper catches it ([service half](kernelet-api-service.md)). A kernelet cannot cause a tier-3 failure except through a bug in host code, which is the trusted base's to fix, or through the one gap the design names: a service path of OSTD's own deeper than the stack reserve (assumption A3, *estimated* at 64 KiB) double-faults outside `KW_TEXT` and halts the machine. The endovisor's hooks, which are the deep paths, run on a per-CPU host stack and do not count against the reserve, so A3 covers only OSTD's own shallow service code, which is measurable. The service half's pointer checks, the device models' descriptor checks and the fallible copies on window addresses are what keep every other kernelet input from reaching a host panic ([User mode](virtualizing-ostd/user-mode.md)).

## Marking

Every tier-2 cause converges on one host function, `Kernelet::mark_dying(reason)`, which must be callable from every context in the table's third column, interrupt and exception context included. It therefore does only what can be done there, once, under a compare-and-swap from `Running` to `Dying`:

1. Records the reason and the time.
2. Stores `dying` on the info page, so that every service call from now on terminates its caller at the prologue, and marks it off the host tick's list, one store, so that no further `JOB_TICK` is posted.
3. Signals the **reaper task**.

That is all, because the double-fault handler that detects a stack overflow runs on its interrupt stack on a CPU that may itself hold the task table's lock, inside a `task_spawn` whose host frames overflowed, and a tick handler runs under an interrupt guard; neither may take a lock or send an interrupt and wait. The reaper does the rest, microseconds later:

4. For every task of the kernelet, under the task table's lock: sets `DYING` in its host-private flags and the mirror; if it is parked, sets `CANCEL_PARK` and unparks it; if it was spawned suspended and has never run, reaps it as `task_destroy` does, so that no never-run task is left to keep the table from emptying. A `task_spawn` racing with this step re-checks `dying` under the same lock after inserting and reaps its own task if set; the trampoline checks `DYING` before entering `run_task`, so a task unparked by the race never runs kernelet code.
5. Sends a reschedule interrupt to every host CPU on which a task of the kernelet is running, so that each returns from user mode or reaches its next interrupt return.

Everything else that may sleep or take long is the reaper's too: it calls `KerneletHooks::on_dying(reason)`, which is where the endovisor shuts down the host objects its device threads hold and marks the kernelet's vsock connections dead, without blocking, so that one stuck backend does not hold every other kernelet's death behind the single reaper ([control half](kernelet-api-control.md), [Channels](channels.md)), and it clears the kernelet's pending jobs and its per-virtual-CPU `timer_arm` deadlines. The reaper is one host task per machine, spawned by OSTD when the kernelet API initializes, with the host's ordinary 512 KiB stack; `on_dying` must not block, so that one kernelet's stuck backend never holds the reaper; `on_exited` may sleep.

`kill` from `Created`, before anything ran, skips steps 3 to 5 and the reaper's work and moves to `Exited` at once.

## Stopping a task

A task is stopped by **terminating it at its next quiescent point**, which is any point where its host-private `service_depth` is zero or where it is returning to zero. The four places a task can be when the kernelet is marked, and what stops it:

- **In a service call**, at depth one, including a park, a `job_wait` or a `user_run`: the call returns, or is canceled and returns, and the epilogue sees `dying` and terminates the task. A `user_run` returns because of the reschedule interrupt of step 5. A hook in progress runs to completion first, which is why hooks do not sleep (register D12).
- **Entering a service call**: the prologue sees `dying` and terminates the task there, at depth zero in host code with interrupts on, which is a quiescent point. No `-DYING` error is ever returned into `KW_TEXT`; the only calls exempt are `stop`, `task_exit` and `task_park`, which a dying kernelet's tasks must still complete, and those end in termination or `-CANCEL` themselves.
- **In kernelet code with preemption enabled**, at depth zero: the next interrupt return into `KW_TEXT` finds `DYING` in the task's flags and terminates it from the interrupt-return path. The host tick guarantees that return within a millisecond, since the host ticks every CPU at `TIMER_FREQ`, 1000 Hz on the tree (checked: `ostd/src/timer/mod.rs`), whatever a kernelet's own idle tick rate is.
- **In kernelet code with preemption disabled**, at depth zero: the same interrupt return terminates it regardless of the preemption count. The count protects the kernelet's own locks and per-CPU state, and a dying kernelet has no future in which those matter; the host's locks are what quiescence protects, and depth zero means none is held. This is the one place the host disregards the count.
- **Parked in kernelet-side state that never crosses**, which cannot happen: every wait in a kernelet bottoms out in `task_park` ([Tasks](virtualizing-ostd/tasks.md)).

**Termination** itself is the host's task-exit path entered from host code without returning to the kernelet. From the service prologue or epilogue it is the path `task_exit` uses, which is the tree's `exit_current` (checked on the tree: `ostd/src/task/scheduler/mod.rs`: it asserts it may sleep, finishes the RCU grace period, and switches under an interrupt guard), entered after the caller has dropped every on-stack host object, in particular the `Arc<Kernelet>` the prologue took, since `exit_current` never returns and would leak it, as the tree's own task entry warns (checked: `ostd/src/task/mod.rs`). From the interrupt-return path it is the variant `preempt_switch` uses, entered with interrupts disabled. Either way the task's kernel stack, with the kernelet's frames on it, is discarded unread, and before the switch the task's entry is removed from the kernelet's task table under its lock and the name's index retired. No kernelet frame is resumed and no kernelet destructor runs. That is sound because at depth zero the task holds no host lock and no host object that must be released by its own code; everything host-side that the task owned is in the host's tables, which destroy drains.

**Who frees the stack, and on whose time.** On the tree the previous task's `Arc` is dropped by the next task, in `after_switching_to`, and the drop tears down the 512 KiB stack's virtual area (checked: `ostd/src/task/processor.rs`, `kernel_stack.rs`). Left as it is, a terminated kernelet task's stack would be freed by whatever runs next on that CPU, possibly another tenant's task at depth zero with no reserve check, on that tenant's time. OSTD therefore changes `after_switching_to` in one place: when the previous task belongs to a kernelet, its last `Arc` is handed to the reaper task instead of being dropped inline. The reaper drops it, which frees the stack and the `Task`, and, when it was the last task in the kernelet's table, moves the kernelet to `Exited`, records the `ExitStatus`, wakes `wait_exited`, and calls `on_exited`. So `Exited` is reached only when no stack of the kernelet is in use anywhere, and no kernelet task's teardown is charged to anyone but the reaper. Adopted device threads are not the kernelet's tasks and are not stopped; `on_dying` told them to finish, and `destroy` is `Zombie` until they disown.

**The page table on the way out.** When the host switches from a kernelet task to a task of another kernelet, or to a host task, it loads the host kernel's page-table root into CR3 and clears its own `ACTIVATED_VM_SPACE` cache ([service half](kernelet-api-service.md)). Without that, CR3 would keep the root the kernelet task last activated, a user root in a grant frame, since the tree's `switch_to_task` never writes CR3 and the kernel proper activates a space only for a task with a `Vmar` (checked on the tree: `kernel/core/src/thread/mod.rs`); a host task running on that CPU after the kernelet's death would then walk a frame that destroy has returned to the allocator. Loading the host root also invalidates the CPU's non-Global translations, so no flush of the window is needed at destroy; the design assumes the host does not enable PCID, which the tree does not (checked: no PCID use under `ostd/src/arch/x86`).

## Destroy

`destroy` runs on a host task, after `Exited`, and is the drain list made executable. Its steps, each against a field of the [per-kernelet host state](kernelet-api-control.md), in this order:

1. **Enter.** Compare-and-swap `Exited → Destroying`, or return `NotExited(state)`; wait for `ops_in_progress` to reach zero; if any run's pin count is nonzero or any adopted task has not disowned, restore `Exited` and return `Zombie { pins, adopted }`: nothing has been released, and the endovisor's retry thread calls again when a pin or an adoption is released ([endovisor](endovisor.md)).
2. **Tasks.** Assert the task table is empty; free the table and the task records.
3. **Jobs, timers, interrupts.** Clear the pending-interrupt bitmap, the per-virtual-CPU tick bits and counts, and the `timer_arm` deadlines; assert the kernelet is off the host tick's list, which the mark did.
4. **Roots.** Assert every registered root's active set is empty, which the switch-away rule guarantees; forget the roots. Their frames are in the grant.
5. **Devices and channels.** Drop the device table; the endovisor's models and threads were told to stop at `on_dying` and have disowned by step 1; assert the vsock switch holds no connection naming the id.
6. **The budget.** Remove the kernelet from the host tick's charge and throttle bookkeeping and free its throttle queue, which is empty, since the tasks are gone and the adopted threads have disowned.
7. **The image and the metadata window.** Unmap the instance's image range and free its private writable frames, leaving the kind's shared read-only frames alone; then free every page-table frame under the metadata window; decrement the kind's text reference count; free the template frames, the replicas, the shared pages and the metadata frames; free the page-table frames that mapped the image. They outlive every root registered under them, which step 4's order guarantees.
8. **Memory.** For every run, in grant-table order: clear its owner-array entries, then drop the host `Segment` that holds it, which returns its frames to the host's allocator through OSTD's own frame lifecycle (checked on the tree: `Segment::drop` releases one frame per page and `dealloc` returns them unzeroed). The frames were zeroed when they were granted ([control half](kernelet-api-control.md), register D55), so a tenant's data never reaches the next holder of the frame, and the host's metadata for those frames was never written by the kernelet and needs no reset.
9. **Accounts.** Uncharge the host bytes; produce the `ReclaimReport`.
10. **Identity.** Retire the slot: advance its generation, drop the slot table's `Arc`, move to `Destroyed`, drop the hooks.

Steps 2 to 10 are the drain list, and the claim that it is complete is the one argued, not checked, claim of this page: it is complete if every field of `Kernelet` and every host-wide structure that can name a `KerneletId` appears in it. The fields are enumerated on the control half's page; the host-wide structures are the slot table, the owner array, the host tick's list and the vsock switch's table, and each has a step here or in the mark. A field added to `Kernelet`, or a host-wide table that learns to name a kernelet, without a step here is the bug class to review for.

With the second version's lending devices ([Zero-copy I/O](zero-copy-io.md)), step 1 also returns `Zombie` while any grain's lend count is nonzero, and a host driver's in-flight request over lent frames is one more host-wide structure that can name a kernelet's memory: it is found empty through that count, which the completion path takes down even for a dying kernelet.

**Why release is safe.** After the last task's switch-away no CPU holds the window's translations or a root of the kernelet in CR3, after step 2 no task can run kernelet code, and after step 1 no `guest_memory` is in progress and no adopted thread is acting for the kernelet; so when step 8 returns a frame to the host allocator, nothing in the machine can reference it through a window address or a page-table walk, and nothing in the host references it at all except through the host's own metadata, which is intact. This is invariant I4's destroy half, and it holds because the host never stored a window address anywhere but the mapping tables step 7 frees.

## What a kernelet's death does not do

- It does not run kernelet code: not a destructor, not a signal handler, not an `atexit`. A tenant that wants a clean shutdown asks its init to do one before exiting.
- It does not touch any other kernelet: no shared frame, no shared table entry, no shared lock exists between two kernelets except through the endovisor's device models, which see a connection reset ([Channels](channels.md)).
- It does not leave the host with a reference into the window, a task in its scheduler, a deadline in its timers, a frame it cannot reuse, or a frame that still holds the tenant's data.
- It does not halt the machine, for any cause in the tier-2 table, including a kernel-mode fault in the kernelet's own code.

## What a tenant sees

An exit status the runtime reports: the code its kernel passed, the panic message, or the kill reason. Processes inside the kernelet see nothing; they stop existing. Connections from other sandboxes or the host to this kernelet are reset. Block I/O the device thread had issued at the mark may or may not have reached the image: `on_dying` cancels what has not been issued and waits for nothing, so a killed sandbox can leave a torn write in its image, as a powered-off machine can.

## Costs

- `mark_dying`: one compare-and-swap, two stores, a signal to the reaper. The task walk, one flag store and, if parked, one unpark per task, one reschedule interrupt per CPU running the kernelet's tasks, `on_dying`, and the job and deadline clearing are the reaper's.
- Per task stopped: the host's task-exit path; the stack's virtual-area teardown on the reaper, one unmap and flush of 128 pages plus the guard; nothing kernelet-side.
- Per grain granted: a 2 MiB zeroing, *estimated* at tens of microseconds, paid at grant rather than at destroy (register D55).
- `destroy`: linear in tasks, runs, devices and the tables of step 7; per run, `Segment::drop` at one frame per page, which is OSTD's own cost for freeing a segment; no flush, since the switch-away rule paid it.
- A `Zombie` costs its grant and host objects until the pin or the adopted thread goes away; it is a sandbox in the state a process in uninterruptible sleep is in, bounded by one host I/O's duration.
- With lending devices a `Zombie` also lasts until the host driver completes or cancels the kernelet's requests, which is bounded only if the driver times requests out ([Zero-copy I/O](zero-copy-io.md), assumption A14).

## What this page decides

- **A dying kernelet's tasks are terminated at depth zero regardless of their preemption count** (register D34). The alternative, honoring the count until a budget expires, would let a dying kernelet delay its own reclamation by the budget for no one's benefit.
- **Termination discards the stack rather than unwinding it** (register D35): unwinding would run kernelet code, the tenant's destructors, on the host's behalf, and could not be done at all beneath a trap frame.
- **Destroy never waits for a pin or an adoption** (register D36); it returns `Zombie` and is retried by the endovisor. Waiting inside `destroy` on a device thread's host I/O would block the caller for as long as that I/O takes.
- **The mark is a compare-and-swap, a store and a signal; the reaper task does the rest, and reaches `Exited`** (register D56, revised). The alternative, walking the task table and sending interrupts in the detecting context, would take a lock inside the double-fault handler on a CPU that may hold it, and would run a Linux-side hook inside the tick's interrupt handler or on a task already below its stack reserve.
- **The switch away from a kernelet task loads the host root into CR3** (register D57). The alternative, flushing at destroy, leaves a freed frame as some CPU's live page-table root between the last task's death and the flush.
- **Grains are zeroed at grant** (register D55). The alternative, scrubbing at destroy, puts 2 MiB per grain on the destroy path and leaves the data in the frame while the kernelet is a zombie.
