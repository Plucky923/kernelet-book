# Tasks, virtual CPUs, and carriers

*Part of question 2. Virtualizes `task`, the sleeping half of `sync`, `cpu`, and the schedule handlers. Discharges invariants I4 (no retained reference), I5 (no closure crosses), I6 (charged CPU time) and the per-task half of I7 (termination).*

A kernelet's tasks are **its own**. They are OSTD `Task`s, created by the identical `TaskOptions`, switched by the kernelet's own copy of `switch_to_task`, and the host has never heard of them. What the host schedules is a **carrier**: one of a fixed number of host kernel threads, one per virtual CPU, on which the kernelet runs whichever of its tasks it chooses.

That is the change this chapter's earlier version did not make. Before, every task of a kernelet was a thread of the host kernel proper, with the host's `Task` and `Thread`, a 512 KiB host stack, and a place in the host's scheduler; the kernelet's own scheduler was inert, and a tenant's thread count bought it share of the machine. Now the currency is the number of virtual CPUs its operator gave it, and the tenant's kernel schedules the tenant's threads — real-time policies, `nice` within the sandbox and `/proc/loadavg` become true instead of inert. What it costs is stated on [Scheduling](scheduling.md) and in [What this page decides](#decides).

## Carriers {#carriers}

A **carrier** is a kernel thread of the host kernel, created by the endovisor at `START`, one for each of the sandbox's virtual CPUs. It is created suspended, enters the image at the entry table's virtual-CPU entry point, and from then on runs kernelet code until the sandbox dies. Carrier 0 is the one that runs the kernel proper's boot; the others enter at the secondary entry point, as application processors do on a machine.

A carrier owns an address space. That matters because the host's own scheduling machinery must restore the page-table root when it switches back to a carrier, and a kernel thread's root was nobody's job until recently: *checked on the tree at `e01871f0c`*, `post_schedule_handler` activates a kernel thread's `vm_space` when it has one, through `as_kernel_thread().and_then(|kt| kt.vmar())`, which upstream added in `d76b4dcc0`, *Allow kernel threads to use a process VMAR*; *checked* absent at `ab9a4cfdc`, where the handler early-returns for a task with no thread-local state. So the endovisor gives each carrier the sandbox's `Vmar` and the host restores its root with no new hook and no second handler slot. What the host still does **not** do for a carrier is save the tenant's floating-point state and the FS and GS bases, because `pre_schedule_handler` returns early for a task with no thread-local state at both commits; that is what is left of [register D17](#decides), and the endovisor does it per carrier.

Nothing else about a carrier is special to the host. It is a kernel thread in the host's scheduler, with the sandbox's `nice`, confined by its affinity to the sandbox's processor set, and the host may preempt it at any moment the sandbox is not holding the processor under the contract [Scheduling](scheduling.md#cooperative) states.

## Tasks are the kernelet's own

`Task`, `TaskOptions`, `CurrentTask`, the run queue and `switch_to_task` are **identical** to the tree's, with one substitution: the per-processor cells they are built on are the kernelet's own, indexed by virtual CPU rather than by the host's `gs:` base ([per-CPU data](#vcpus)). There is no `TaskName`, no `RUNNING` table, no `BODIES` slab, no `task_spawn` hook and no per-task record: eight of the service half's twenty-one calls and one hook leave with them ([The kernelet API: service half](../kernelet-api-service.md)).

- `TaskOptions::build` and `Task::run` make **no crossing at all**. A spawn is an allocation and an enqueue, inside the kernelet, at the cost the tree pays.
- **A trap for the implementer**: `TaskOptions::spawn` ends in `might_preempt()`, so a context that spawns during bootstrap can be abandoned mid-setup before it has finished. It cost the prototype two boots, on both the host side and the kernelet side (*measured on the booted Asterinas prototype*).
- `Task::yield_now`, `need_yield`, waiting and waking are the tree's code over the kernelet's run queue. `WaitQueue`, `Waiter`, `Waker`, `Mutex` and `RwMutex` keep their code *and* their mechanism: a park and a wake are now a run-queue operation inside the kernelet, not a crossing each.
- **Idle tasks come back** (register D67, retired). A kernelet has virtual CPUs to idle, so the tree's own idle loops are spawned and the two `cfg` lines that suppressed them go. An idle carrier does not spin: the idle loop calls `vcpu_idle`, which sleeps the carrier until a deadline or a kick ([Interrupts and time](interrupts-and-time.md)).
- `nice` and affinity builders on `TaskOptions` are withdrawn (register D31, retired), because the kernel proper's own scheduler honors them now; so are the `cfg` lines the kernel proper carried to call them.

## Kernelet stacks {#stacks}

A task's stack comes from a **per-sandbox pool** of host memory, charged to the sandbox and bounded by `max_tasks`, not from the host's 512 KiB thread stacks. How much stack a kernel proper needs is a property of the kernel proper, not of who is hosting it, so the kernelet build sets OSTD's stack size and both hosts use the same figure.

There is **no two-stack rule**. Kernelet code and the host service code it calls run on one stack, and this works because `syscall_return` stores `rsp` into `gs:4` itself (*measured on the booted Asterinas prototype*: the shared stack is what the prototype runs, and no coroutine switch was needed). What survives of the question is how *much* stack, not whose: the reserve that the service prologue checks, and the function-entry check behind it.

Two prerequisites the tree does not meet, and they are prerequisites rather than assumptions:

- **An interrupt-stack-table entry for vector 8, and a double-fault stack in the task-state segment.** *Checked on the tree*, and confirmed by the prototype: no stack index is ever set in `trap/idt.rs` and `trap/gdt.rs` installs a bare `TaskStateSegment::new()`. Without one, a kernelet stack overflow does not merely share a stack — it hits the guard page, the fault handler panics further down the exhausted stack into the guard, and the double fault's own frame push faults again, which is a **triple fault and a machine reset**. The function-entry check is therefore the primary mechanism and the double-fault handler the backstop, not the reverse; [assumption A6](../faults-and-reclamation.md) is restated as this prerequisite.
- **The reserve must be re-estimated against upcall depth.** The stack a service call's prologue measures may already carry a hardware trap frame, the upcall's hand-off and the whole stub with its handlers ([Scheduling](scheduling.md#upcall)), which the reserve's first estimate did not count.

## Virtual CPUs and per-CPU data {#vcpus}

A kernelet's configuration names a set of host processors; its virtual CPU *i* is carried by carrier *i*. `num_cpus`, `all_cpus` and `CpuId::current` are virtualized without a crossing, the last from the carrier's own record.

`cpu_local!` and `cpu_local_cell!` are virtualized without a crossing. The image's `.cpu_local` section is replicated once per virtual CPU after the writable segment ([Builds and images](../builds-and-images.md)), and vOSTD addresses a replica as `replica_base + vcpu × replica_bytes + (static_addr − cpu_local_start)`. On the tree the address is formed from the `gs:` base, which is the host's; vOSTD forms it from the carrier's record instead.

What changes with upcalls is the guard. `CpuLocalCell`'s single-instruction operations are `gs:`-relative read-modify-writes on the tree precisely so that their callers need no guard, and the earlier version of this page raised a preemption count around each of them to stop a *migration* between forming the address and using it. That is no longer sufficient, and the prototype found out why by hanging: **the kernelet's spin locks must take an interrupt guard, not a preemption guard.** The scheduler held its run-queue lock, the tick arrived as an upcall on that very task, the handler re-entered the run queue, and the virtual CPU deadlocked against itself (*measured on the booted Asterinas prototype*, finding F7). Once virtual interrupts are delivered by upcall a kernelet *does* run code in interrupt context, so the sentence "nothing in a kernelet runs in interrupt context" is withdrawn, and with it the aliasing of `irq::disable_local` to the preemption count that rested on it ([Interrupts and time](interrupts-and-time.md), register D18).

## When a carrier dies {#death}

A carrier is the unit of termination as well as of scheduling. Killing a sandbox parks every carrier at its next safe point and runs the exit stub on each; a carrier that will not leave kernelet code is the case the [bound](scheduling.md#cooperative) exists for. Because no kernelet task is a host task, *Exited* means **no carrier is in kernelet text** — not "no stack of the kernelet is in use anywhere", which named a host object that no longer exists ([Faults, termination, and reclamation](../faults-and-reclamation.md)).

One piece of the old design becomes dead code and is swept with it: the hand-off of a dead kernelet task's reference to the reaper in `after_switching_to` has nothing to hand off, because the host's `after_switching_to` never sees a kernelet task. The consequence is benign — the kernelet's own replica frees its stacks by the same previous-task mechanism, on its own time.

## Costs

- Per task: a stack from the sandbox's pool and the tree's own `Task`, both charged to the sandbox; **no host `Thread`, no host `Task`, no 512 KiB host stack, no process identifier**. A thousand tenant threads cost the host *N* carriers.
- Per spawn, park, wake and yield: what the tree pays, **no crossing**.
- Per task switch: the tree's `switch_to_task` — about 1,000 cycles, *measured on the booted prototype of the Linux design* and **[unverified]** here; the prototype performed 178 of them through the kernelet's own scheduler without measuring one.
- Per `cpu_local!` access: two loads more than a `gs:`-relative access.
- Per virtual CPU: one carrier with its host stack, one `.cpu_local` replica, and an idle task.

## What a tenant sees

- `nproc` and `/proc/cpuinfo` report the kernelet's virtual CPUs.
- `nice`, `sched_setaffinity`, `sched_setscheduler` and the real-time policies **work and mean what they mean on a machine**, because the kernel proper's own scheduler is the one that runs. `/proc/loadavg`, the run-queue fields of `/proc/stat` and `sysinfo(2)`'s load figures report the sandbox's real load.
- The sandbox's share of the machine is its virtual CPUs' share, not its thread count's. A tenant can no longer buy machine share by running more threads.
- Nothing else: process creation, threads, waiting, signals and timing behave as on the host kernel.

## What this page decides {#decides}

- **A kernelet's tasks are the kernelet's own, and the host schedules carriers** (register D116; it retires D61, under which every kernelet task was a host thread). The alternative is what this chapter said before: a host thread per tenant thread, which makes the kernel proper's scheduler inert, makes a tenant's thread count a host resource, and gives one sandbox with eight threads four times the share of one with two.
- **A carrier owns an address space, and the host's existing post-schedule handler restores its root** (register D123, new). The alternative, a kernelet branch in the host kernel's two schedule functions or a second OSTD handler slot, is unnecessary since upstream `d76b4dcc0`; both handler slots are `Once` and already claimed, so it was also the only alternative available.
- **The endovisor saves the tenant's floating-point state and the FS and GS bases per carrier** (register D17, narrowed to that half). The host does not, because `pre_schedule_handler` early-returns for a task with no thread-local state. **[unverified]**: the prototype's per-carrier save ran but does not yet save the FS and GS bases.
- **Kernelet stacks come from a per-sandbox pool, on one stack shared with host service code** (register D116, whose stack pool this host shares; there is no Asterinas counterpart of the Linux two-stack rule). The two-stack rule is not needed, because `syscall_return` stores `rsp` into `gs:4` itself.
- **Idle tasks return** (register D67, retired), because a kernelet now has virtual CPUs to idle.
- **The kernelet's spin locks take an interrupt guard** (register D18, revised), because upcalls give a kernelet handlers and a preemption guard does not exclude one.
