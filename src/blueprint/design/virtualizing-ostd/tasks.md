# Tasks, scheduling, and CPUs

*Part of question 2. Virtualizes `task`, `cpu`, and the schedule handlers. Synchronization is described in its [own chapter](synchronization.md). Covers the design obligations for invariants I4 (no retained reference), I5 (no closure crosses), I6 (charged CPU time) and the per-task half of I7 (termination).*

A **vCPU Thread** is the native Thread that Host schedules for one vCPU.
It uses `SchedPolicy::Fair(nice)` and competes with runnable fair-class threads on its assigned physical CPU through Host's CFS scheduler.
CFS uses scheduling weights and virtual runtime to distribute CPU time among runnable threads; `nice` determines the vCPU Thread's weight.
The kernelet scheduler chooses which internal Task that vCPU Thread executes.
Each internal Task has a Host-managed kernel stack and saved register context, plus a Rust `Task` object and closure inside the kernelet. The initial stack size is 512 KiB, measured on the source tree.
vOSTD keeps a pointer to the Host resource and passes it to stack/context services. It never dereferences that pointer; dropping its wrapper requests release through a service.
The kernel keeps the existing `Task` API. Internal Tasks do not enter the Host run queue.

The kernel's `Thread`, `ClassScheduler` and run queues use the same source in both builds.
The shared `task::scheduler` code calls processor/CPU operations selected by `cfg`. The kernelet build adds event polling and selects the exit service inside `exit_current` at the marked statements.
The `kernelet` Cargo feature selects the vOSTD build, as defined in [Builds and images](../builds-and-images.md#two-builds).
Common types, algorithms and callers keep their native source without `cfg`.
When a whole module differs, select it once at the module declaration; its contents need no repeated attributes.
Use item, field or statement attributes only for differences inside a file compiled in both builds:

```rust
// ostd/src/task/mod.rs: native processor.rs remains the default.
#[cfg_attr(feature = "kernelet", path = "processor/kernelet.rs")]
mod processor;

// ostd/src/task/preempt/mod.rs: native cpu_local.rs remains the default.
#[cfg_attr(feature = "kernelet", path = "cpu_local/kernelet.rs")]
mod cpu_local;

// ostd/src/lib.rs: both builds need kernelet::abi.
pub mod kernelet;

// ostd/src/kernelet/mod.rs
pub mod abi; // Same service tables and shared-page layouts in both builds.
#[cfg(feature = "kernelet")]
mod entry; // _kernelet_entry and the image entry table.
#[cfg(not(feature = "kernelet"))]
mod host;
#[cfg(not(feature = "kernelet"))]
pub use host::*; // Keep Kernelet, enter_vcpu and service at ostd::kernelet::*.

// ostd/src/kernelet/host/mod.rs: all contents inherit the Host selection.
pub mod control; // Kernelet, configuration, hooks and lifecycle APIs.
pub use control::*;
pub mod service;

// kernel/core/src/lib.rs: the endovisor is also a Host-only module.
#[cfg(not(feature = "kernelet"))]
mod endovisor;
```

The Host definitions below belong to `ostd::kernelet::host`, and the hook belongs to `kernel::endovisor`. Their module declarations already select the build, as do the two vOSTD backend files.
`ostd::kernelet::abi` stays in both builds. `KernelStack`, `TaskContext` and the native scheduler also keep their existing definitions; Host code reuses them.
The [vCPU execution loop](tasks.md#vcpu-loop) connects the closure, startup, internal switching, physical interruption and final stop.
The remaining sections expand those paths, then describe accounting and CPU-local access.
Proposed code is distinguished from native code; [remaining implementation work](tasks.md#obligations) is listed at the end.

## The vCPU execution loop {#vcpu-loop}

The vCPU execution loop is Host OSTD code run by the vCPU Thread.
It handles the work requested when internal execution leaves an internal Task's code and returns to this loop: checking Host preemption after an interrupt, completing an internal Task switch, finishing an internal Task, or stopping the vCPU.
After handling a return, it resumes the interrupted internal Task or enters the selected next Task; after vCPU stop cleanup, it finishes the thread's closure.
Leaving internal execution does not necessarily mean the internal Task has finished: `Pause` and `SwitchTask` preserve a continuation for that Task.

Only the designated pause, switch, exit and stop paths return to this loop.
An ordinary OSTD service can return directly to its internal caller; a blocking service can resume that call after the vCPU Thread is scheduled again.
Native thread switching suspends and resumes the scheduling call without creating a `VcpuReturnReason`.
An IRQ interrupting the loop itself uses the corresponding native IRQ return path and must not restore the already-active loop again.

The [creation hook](tasks.md#vcpu-creation) defines the vCPU Thread's closure as:

```rust
// Endovisor: this closure is the vCPU Thread's entry function.
move || ostd::kernelet::enter_vcpu(kernelet_id, vcpu)
```

`enter_vcpu` runs the vCPU execution loop on that thread's original stack.
It stays active for the vCPU's lifetime; preemption and internal Task switches do not restart the closure.
The `enter_vcpu` function below first enters the startup context once.
vCPU 0 initializes the kernel; secondary vCPUs run their bootstrap entry.
Startup's first `task_switch` request returns to the loop with the first internal Task already selected; the loop transfers to that Task and retires the startup stack.
The loop does not rerun startup after a pause or an internal switch.
The listing below covers startup, pause, internal switch, exit and stop; its machine entry helper follows it.
Virtual-tick entry/completion still needs to be connected to this loop as specified in [Interrupts and time](interrupts-and-time.md#tick-return). The ordinary restore branches below do not implement that extension.
These functions are proposed Host OSTD additions, not functions already implemented in the native tree.

### The closure calls enter_vcpu {#host-entry}

```rust
// Host OSTD: this entire call chain uses the existing vcpu_thread_task.kstack.
pub fn enter_vcpu(kernelet_id: KerneletId, vcpu_id: u16) {
    let vcpu = lookup_vcpu_for_current_task(kernelet_id, vcpu_id);
    let (start, startup_cpu_state) = vcpu.startup_context_pointers();
    let irq_guard = crate::irq::disable_local(); // Native atomic_mode must be zero.
    let (vcpu_loop_context, vcpu_loop_cpu_state) = vcpu.prepare_vcpu_loop_return();
    core::mem::forget(irq_guard);
    unsafe {
        enter_internal(start.cast(), startup_cpu_state, vcpu_loop_context, vcpu_loop_cpu_state,
                       resume_task_context as *const ());
    }
    // A saved call in the vCPU execution loop has returned; all internal/IRQ entry frames are off this stack.
    let mut returned = Some(take_vcpu_return_reason());
    while let Some(reason) = returned {
        // Handles this return, resumes internal execution, then reports its next return.
        // None means stop cleanup has completed and this loop can finish.
        returned = handle_vcpu_return(reason);
    }
    vcpu.acknowledge_vcpu_cleanup();
    drop(vcpu);
} // The original closure and native Thread wrapper return/exit normally.
```

The initial `enter_internal` call enters the startup context; later calls in [resume_vcpu_or_stop](tasks.md#vcpu-dispatch) restore either a paused internal Task or the Task selected by an internal switch.
The lookup and return-preparation helpers identify the current vCPU and retain its return storage; [entry resource requirements](tasks.md#startup-entry-state) specify their contracts.

`VcpuReturnReason` expresses why internal execution returned to this loop:

- `Pause`: retain the interrupted internal Task, check Host preemption, then resume it when allowed.
- `SwitchTask`: complete an internal `task_switch(next)` service request; vOSTD has already selected `next`. This does not ask the Host scheduler to select a vCPU Thread.
- `Exit`: finish the outgoing internal Task and transfer to the selected next Task.
- `Stop`: complete or cancel outstanding operations and finish vCPU cleanup.

The loop's handler returns `Some(next_reason)` only after another interval of internal execution has ended and returned to the vCPU loop.
It returns `None` after stop cleanup; the outer closure can then finish.
Thus returning from internal execution is different from returning from the vCPU Thread's closure.

### enter_internal saves a return position, then enters the target {#internal-entry-gate}

`enter_internal` is the machine entry function called by `enter_vcpu` above and by `resume_vcpu_or_stop` later.
It saves the current stack pointer, return address and callee-saved registers into this vCPU's `vcpu_loop_context`, and saves the loop's supplementary CPU state separately.
It then invokes the selected restore gate, which loads the target's registers and stack and resumes execution there.
The target is startup on the first call, a newly selected internal Task on an internal switch, or a retained interrupt frame on resumption.

```rust
// Called with physical IRQs masked and a retained, validated target.
// target is TaskContext or TrapFrame, selected together with the trusted restore gate.
// The saved call in the vCPU loop is restored later; its stack and locals stay alive.
#[unsafe(naked)]
unsafe extern "C" fn enter_internal(
    target: *const (), target_cpu_state: *const CpuStateSnapshot,
    vcpu_loop_context: *mut TaskContext, vcpu_loop_cpu_state: *mut CpuStateSnapshot,
    restore_gate: *const (),
) {
    core::arch::naked_asm!(
        "mov [rdx + 0], rsp", "mov [rdx + 8], rbx", "mov [rdx + 16], rbp",
        "mov [rdx + 24], r12", "mov [rdx + 32], r13",
        "mov [rdx + 40], r14", "mov [rdx + 48], r15",
        "mov rax, [rsp]", "mov [rdx + 56], rax",
        "mov r12, rdi", "mov r13, rsi", "mov r14, r8",
        "mov rdi, rcx", "sub rsp, 8", "call {capture}", "add rsp, 8",
        "mov rdi, r12", "mov rsi, r13", "jmp r14",
        capture = sym capture_cpu_state,
    );
}
```

In this x86-64 entry, `rdx` points to `vcpu_loop_context`; the initial `mov` instructions save the loop's call context there.
The final `jmp r14` enters the trusted target restore gate supplied by the caller.
The function therefore does not immediately return to the next Rust statement: execution is now at the target.

### return_to_vcpu_loop resumes the saved call {#vcpu-loop-return-gate}

For an eligible IRQ pause, the IRQ return code retains the snapshot, publishes `VcpuReturnReason::Pause`, and calls `return_to_vcpu_loop`.
The return gate is defined here:

```rust
#[unsafe(naked)]
unsafe extern "C" fn return_to_vcpu_loop(
    vcpu_loop_context: *const TaskContext, vcpu_loop_cpu_state: *const CpuStateSnapshot,
) -> ! {
    // Restore the pending enter_internal call on the vCPU Thread's original stack.
    // No Rust prologue runs after the Host return phase has been published.
    core::arch::naked_asm!(
        "mov r12, rdi",
        "mov rdi, rsi", "sub rsp, 8", "call {install}",
        "mov rdi, r12", "jmp {restore}",
        install = sym restore_cpu_state,
        restore = sym restore_vcpu_loop_context,
    );
}
```

The loop return restores the loop's supplementary state, then uses the following masked register tail.
Physical IRQs remain disabled until `handle_vcpu_return` publishes the loop phase; the internal Task's `sti; ret` tail would enable them too early here.

```rust
// Host OSTD: restore the suspended enter_internal call with physical IF clear.
#[unsafe(naked)]
unsafe extern "C" fn restore_vcpu_loop_context(ctx: *const TaskContext) -> ! {
    core::arch::naked_asm!(
        "mov rsp, [rdi + 0]", "mov rbx, [rdi + 8]", "mov rbp, [rdi + 16]",
        "mov r12, [rdi + 24]", "mov r13, [rdi + 32]",
        "mov r14, [rdi + 40]", "mov r15, [rdi + 48]",
        "mov rax, [rdi + 56]", "mov [rsp], rax", "ret",
    );
}
```
Execution continues after `enter_internal`; `take_vcpu_return_reason` consumes the published reason, and the callers pass it back to the loop.
The `Pause` handler then calls Host `might_preempt()` and, when allowed, resumes the interrupted internal Task through another `enter_internal` call.
This completes the cycle without starting another thread, allocating another stack, or recursively accumulating loop frames.

`vcpu_loop_context` saves the position to return from internal execution to the vCPU loop.
The `ctx` field of the vCPU Thread's native OSTD Task separately saves the position inside Host scheduling when that thread is actually switched out.
`VcpuReturnReason` is a Rust control-flow value in this proposed implementation, not a new service-table call or another preemption flag.
The IRQ/service gate temporarily stores it in the vCPU record because restoring the saved registers does not itself return a Rust enum; the loop consumes that stored value once.
An IRQ during the loop or an ineligible execution phase follows its own return rules and must not restore the pending loop call twice.

### How the rest of the chapter follows the loop

1. [Creation](tasks.md#vcpu-creation) constructs the vCPU Thread and its closure; it also prepares the resources used by internal execution.
2. [Startup](tasks.md#task-execution) enters the image once and reaches the first internal `task_switch` request.
3. [Internal switching](tasks.md#task-switch) suspends an internal service call and returns `SwitchTask` to the loop, which transfers to the selected internal Task.
4. [Physical interruption and Host preemption](tasks.md#execution-flow) preserve the interrupted internal Task and return `Pause` to the loop. Host scheduling may suspend the loop's call; re-selection continues that call before internal restoration.
5. [Internal exit](tasks.md#task-exit) returns `Exit` with the next internal Task; [vCPU stop](tasks.md#stack-return) ends the execution loop and its outer closure after cleanup.

An ordinary service is not automatically a return to this loop.
For example, [idle waiting](tasks.md#waiting) may block the vCPU Thread inside `vcpu_wait`; when woken and selected again, it continues that service call and returns to the internal idle Task.
Host thread switching itself produces no `SwitchTask` reason: it suspends and later resumes the native scheduling call already in progress.

## Task creation

### Create the vCPU Thread {#vcpu-creation}

Only vCPU creation calls `create_vcpu_thread` in the endovisor.
`create` builds all vCPU Threads without starting them; `start` then enqueues them. The [control API](../kernelet-api-control.md) defines failure cleanup and prevents start from racing stop.

**OSTD: prepare all vCPUs before returning the instance.**

```rust
impl Kernelet {
    pub fn create(
        config: KerneletConfig,
        hooks: Arc<dyn KerneletHooks>,
    ) -> Result<Arc<Self>, CreateError> {
        validate_config(&config)?; // Includes online CPUs, vCPU count and nice.
        let cpus = config.cpus.clone();
        let nice = config.nice;
        let mut creation = KerneletCreation::new(config, hooks)?;

        for (index, host_cpu) in cpus.iter().enumerate() { // Ascending CPU order defines IDs.
            let vcpu = index as u16; // Validation bounds the count by MAX_VCPUS.
            creation.prepare_vcpu(vcpu, host_cpu)?;

            let kernelet = creation.kernelet();
            let task = kernelet
                .hooks
                .create_vcpu_thread(kernelet, vcpu, host_cpu, nice)
                .map_err(|error| match error {
                    VcpuThreadError::NoMemory => CreateError::NoMemory,
                })?;

            creation.attach_host_thread(vcpu, task);
        }

        creation.commit()
    }

    pub fn start(&self) -> Result<(), StateError> {
        let launch = self.begin_start()?; // Once-only start; resources retained against kill.
        for vcpu in self.vcpus.iter() {
            vcpu.vcpu_thread_task.run(); // Native Task::run: enqueue this host Task once.
        }
        drop(launch);
        Ok(()) // Enqueued, not necessarily booted or ready for tenant requests.
    }
}
```

**Endovisor: implement the hook using the host's kernel-thread builder.**

```rust
// kernel/core/src/endovisor/: module already Host-only; reuses ThreadOptions.
fn create_vcpu_thread(
    &self,
    kernelet: &Kernelet,
    vcpu: u16,
    host_cpu: CpuId,
    host_nice: i8,
) -> Result<Arc<ostd::task::Task>, VcpuThreadError> {
    let mut affinity = CpuSet::new_empty();
    affinity.add(host_cpu);
    let nice = Nice::try_from(host_nice).expect("validated vCPU Thread configuration");

    let kernelet_id = kernelet.id();
    // Building stores this closure; the host scheduler executes it after start().
    ThreadOptions::try_new(move || ostd::kernelet::enter_vcpu(kernelet_id, vcpu))
        .map_err(|_| VcpuThreadError::NoMemory)? // Fallible closure allocation too.
        .cpu_affinity(affinity)
        .sched_policy(SchedPolicy::Fair(nice))
        .try_build() // Proposed fallible counterpart of native build().
        .map_err(|_| VcpuThreadError::NoMemory)
}
```

The vCPU Thread's closure calls the public `ostd::kernelet::enter_vcpu` once. That [call](tasks.md#host-entry) returns only after the vCPU stops and Host cleanup finishes.
On a Host-provided startup stack, vCPU 0 enters `_kernelet_entry`; secondary vCPUs enter `secondary_bootstrap` through `EntryTable::run_task`.
Each starts with no internal current Task. [Initialization and first scheduling](tasks.md#task-execution) establish one. The hook above supplies the endovisor side of this proposal.

### Kernelet internal task object {#task-lookup}

The internal `Task` holds its closure and kernel data. Its `TaskResource` stores a pointer to the Host's `TaskExecRecord`, which owns the stack and saved registers.
vOSTD passes this pointer only to services; it never reads the record directly. Cloning `Arc<Task>` shares the same wrapper, whose Drop calls `task_release` once.
`CurrentTask` borrows the internal Rust Task. Host never receives that Rust pointer.

```rust
use core::ffi::c_void;

// vOSTD: owns one Host resource claim; deliberately neither Copy nor Clone.
#[cfg(feature = "kernelet")]
struct TaskResource {
    ptr: NonNull<c_void>,
}

#[cfg(feature = "kernelet")]
impl TaskResource {
    fn as_ptr(&self) -> *mut c_void {
        self.ptr.as_ptr() // Borrowed for a service call; no ownership transfer.
    }
}

// SAFETY: The pointer is never dereferenced here. Host ownership survives moves
// between vCPUs; Host services serialize resource state and validate every use.
#[cfg(feature = "kernelet")]
unsafe impl Send for TaskResource {}
#[cfg(feature = "kernelet")]
unsafe impl Sync for TaskResource {}

#[cfg(feature = "kernelet")]
impl Drop for TaskResource {
    fn drop(&mut self) {
        let status = (service_table().task_release)(self.ptr.as_ptr());
        if status != 0 { crate::panic::abort(); }
    }
}

// ostd/src/task/mod.rs: common Task fields plus the cfg-selected resource fields.
pub struct Task {
    #[cfg(not(feature = "kernelet"))]
    ctx: SyncUnsafeCell<TaskContext>,
    #[cfg(not(feature = "kernelet"))]
    kstack: KernelStack,
    #[cfg(not(feature = "kernelet"))]
    switched_to_cpu: AtomicBool,
    #[cfg(feature = "kernelet")]
    resource: TaskResource,
    func: ForceSync<Cell<Option<Box<dyn FnOnce() + Send>>>>,
    data: Box<dyn Any + Send + Sync>,
    local_data: ForceSync<Box<dyn Any + Send>>,
    schedule_info: TaskScheduleInfo,
}

// Same local borrowed-current API as native OSTD.
pub struct CurrentTask(NonNull<Task>);
impl !Send for CurrentTask {}
impl !Sync for CurrentTask {}
```

The kernelet Thread builder stores its local `Arc<Thread>` in `data`, as on the native path; no separately scheduled native thread is created for this internal Task.
The vCPU Thread has its own native `Task`. Each internal Task instead has a `TaskExecRecord` for its stack and saved context.

`task_create` returns a non-null pointer that the wrapper must eventually release.
The [service ABI](../kernelet-api-service.md#task-resource-abi) carries the full pointer value; it is not shortened to a task number:

```rust
// ostd/src/task/mod.rs: one builder, with native and vOSTD resource branches.
// kernel_task_entry moves to module scope; see the first-entry section below.
impl TaskOptions {
    pub fn build(self) -> Result<Task> {
        let data = self.data.unwrap_or_else(|| Box::new(()));
        let local_data = self.local_data.unwrap_or_else(|| Box::new(()));
        let schedule_info = TaskScheduleInfo { cpu: AtomicCpuId::default() };

        #[cfg(not(feature = "kernelet"))]
        let (kstack, ctx) = {
            // Existing native stack/context initialization.
            let kstack = KernelStack::new_with_guard_page()?;
            let mut ctx = TaskContext::new();
            ctx.set_instruction_pointer(
                crate::arch::task::kernel_task_entry_wrapper as *const () as usize,
            );
            ctx.set_stack_pointer(kstack.end_vaddr() - 16);
            (kstack, ctx)
        };

        #[cfg(feature = "kernelet")]
        let resource = {
            let result = (service_table().task_create)(); // Added SERVICE call.
            match result.status {
                0 => TaskResource {
                    ptr: NonNull::new(result.resource)
                        .unwrap_or_else(|| crate::panic::abort()),
                },
                status if status == -i64::from(INVALID) => return Err(Error::InvalidArgs),
                status if status == -i64::from(LIMIT) => return Err(Error::NotEnoughResources),
                status if status == -i64::from(NOMEM) => return Err(Error::NoMemory),
                _ => crate::panic::abort(),
            }
        };

        Ok(Task {
            func: ForceSync::new(Cell::new(self.func)),
            data,
            local_data: ForceSync::new(local_data),
            schedule_info, // Existing TaskScheduleInfo; currently only its cpu field.
            #[cfg(not(feature = "kernelet"))]
            ctx: SyncUnsafeCell::new(ctx),
            #[cfg(not(feature = "kernelet"))]
            kstack,
            #[cfg(not(feature = "kernelet"))]
            switched_to_cpu: AtomicBool::new(false),
            #[cfg(feature = "kernelet")]
            resource,
        })
    }
}
```

The initializer shows two alternative builds: native Task retains `ctx`, `kstack` and `switched_to_cpu`; vOSTD replaces those three fields with `resource`.
`func`, `data`, `local_data` and `schedule_info` are the existing fields in both builds; the initializer never contains both resource representations.

`TaskOptions::build` completes all local preparation that can fail before calling `task_create`. If the service fails, Host cleans up the resources allocated by that call.
Idle and interrupt-worker Tasks use this same builder. Only the builder constructs `TaskResource`; no public constructor accepts an arbitrary address. Its private `as_ptr` is for service calls while the wrapper remains alive.
Unused Tasks request release on Drop. Exited Tasks remain alive through the outgoing-reference handoff; Host resources wait for [execution accounting and lifetime checks](tasks.md#accounting).
Host frees a stack only when no execution, service or IRQ path can use it again. It keeps the record while a wrapper or notification can still refer to it.

### Host execution resources {#task-resources}

One vCPU can alternate between many Tasks. Each needs its own stack and saved context so it can continue where it stopped; these belong in `TaskExecRecord`.
The [Vcpu](tasks.md#vcpu-state) records the active internal Task, the pending vCPU-loop return and any retained interrupt snapshot.
Native Host descheduling separately saves the vCPU Thread's scheduling call in its native Task's `ctx`.

The instance's `tasks` map indexes the allocated resources by their opaque addresses; it is not a run queue.
`task_create` inserts a resource, and `task_switch` looks up the resource selected by the kernelet scheduler.
The instance-wide index lets a Task move between vCPUs without changing its resource address. `active_task` keeps the current record alive and records which resource this vCPU has the exclusive right to execute.

```rust
// ostd::kernelet::host
// New Host OSTD code: one record per internal Task, allocated by task_create.
struct TaskExecRecord {
    kstack: SyncUnsafeCell<Option<KernelStack>>, // This internal Task's stack.
    ctx: SyncUnsafeCell<TaskContext>, // Its saved task_switch call, not a vCPU IRQ frame.
    cpu_state: SyncUnsafeCell<CpuStateSnapshot>, // Actual root/TLS/FPU at the suspended call.
    switched_to_cpu: AtomicBool,
    execution_total: AtomicU64, // Cumulative Task execution; initially zero.
}

// One map entry per internal Task; record's allocation stays at one address.
struct TaskAllocation {
    record: Arc<TaskExecRecord>,
    diagnostic_id: TaskId, // Only for the host on_oops hook.
    vostd_owner: bool, // task_create's owning claim has not been released.
    started: bool,     // Host has committed a first selection of this resource.
    execution_disabled: bool, // Permanently forbid entering this stack; storage may remain.
}

// New Host OSTD code: one resource map per kernelet instance, used by all its vCPUs.
struct Kernelet {
    tasks: SpinLock<BTreeMap<usize, TaskAllocation>>,
    // Other instance fields omitted.
}

impl Kernelet {
    // task_switch -> resolve_task: retain the existing Host record.
    // self comes from the current vCPU Thread, not a service argument.
    fn resolve_task(
        &self,
        resource: *mut c_void,
    ) -> Option<Arc<TaskExecRecord>> {
        let tasks = self.tasks.lock();
        let allocation = tasks.get(&resource.addr())?;
        if !allocation.vostd_owner { return None; }
        Some(allocation.record.clone())
    }
}
```

The address and initial ownership state come from the same allocation:

```rust
// ostd::kernelet::host
// New Host OSTD code, inside task_create after allocating and initializing record.
let (resource, allocation) = {
    let resource = Arc::as_ptr(&record).cast_mut().cast::<c_void>();
    let allocation = TaskAllocation {
        record, // Move this Arc into the map entry; the pointee does not move.
        diagnostic_id,
        vostd_owner: true,
        started: false,
        execution_disabled: false,
    };
    (resource, allocation)
};
// Publish allocation under resource.addr() before returning resource to vOSTD.
```

Moving an `Arc` into the map leaves the record at the same address. The Arc stays in Host; vOSTD later calls `task_release` through its wrapper's Drop.
Before reporting success, `task_create` must allocate the stack, counter slot and map entry. Failure releases all resources allocated by that call.
Native `BTreeMap::insert` cannot report allocation failure, so reserve its storage or use a map with fallible insertion.

`resolve_task` clones a Host-owned `Arc`; [claim_task](tasks.md#transfer) separately checks whether this vCPU may use the stack.

`task_exit_to` sets `execution_disabled` before releasing this vCPU's exclusive use of the stack. No later switch may enter that Task. `task_release` later clears `vostd_owner`; for a never-started Task it also disables execution.
Release requires that no vCPU owns execution and that a started Task has completed exit. Host still waits for remaining users and [final accounting](tasks.md#accounting) before freeing its resources. These services are new Host OSTD code.

## From vCPU startup to running tasks {#task-execution}

This is the startup interval of the [vCPU execution loop](tasks.md#vcpu-loop): follow the boot vCPU from its startup stack to the first internal Task.

### 1. Enter the kernel and register its scheduler

On vCPU 0, the vCPU Thread's closure calls `enter_vcpu`, which enters `_kernelet_entry` on the startup stack described in [Boot](the-rest.md#boot).
[Boot](the-rest.md#boot) defines `_kernelet_entry`: it installs `SERVICES`/`BOOT_ARGS`, initializes vOSTD, then calls `__ostd_main`, generated by the existing `#[ostd::main]` macro.
That macro calls this image's kernel `main` on the startup stack with `Task::current() == None`.

Kernel `main` retains this native order (`kernel/core/src/init.rs`; logging omitted):

```rust
// Existing kernel code: kernel/core/src/init.rs.
pub(super) fn main() {
    component::init_all(InitStage::Bootstrap, component::parse_metadata!()).unwrap();
    init(); // Includes sched::init and registration below.
    init_on_each_cpu();
    ostd::boot::smp::register_ap_entry(ap_init);

    ThreadOptions::new(bsp_idle_loop)
        .cpu_affinity(CpuId::bsp().into())
        .sched_policy(SchedPolicy::Idle)
        .spawn();
}
```

Kernel initialization registers its native scheduler and pre/post handlers in this image:

```rust
// Existing registrations in thread/mod.rs and sched/sched_class/mod.rs.
ostd::task::inject_pre_schedule_handler(pre_schedule_handler);
ostd::task::inject_post_schedule_handler(post_schedule_handler);
let scheduler = Box::leak(Box::new(ClassScheduler::new()));
inject_scheduler(scheduler);
set_stats_from_scheduler(scheduler);
```

The handlers and ClassScheduler stay in this kernelet's static variables; Host never receives or invokes them.
Each secondary vCPU starts through `EntryTable::run_task(ENTRY_SECONDARY_VCPU, vcpu_id)`. That function calls `secondary_bootstrap` on the secondary's startup stack. As in native AP startup, it calls the registered kernel entry and then yields:

```rust
// Proposed vOSTD boot/smp.rs entry for this task model.
// Runs on this secondary vCPU's startup stack, with Task::current() == None.
#[cfg(feature = "kernelet")]
fn secondary_bootstrap() -> ! {
    let ap_entry = AP_LATE_ENTRY.wait();
    ap_entry();
    Task::yield_now();
    unreachable!("first scheduling from bootstrap must not return");
}
```

vCPU 0 publishes the kernel's `ap_init` through `register_ap_entry`. Each secondary waits for that function, then calls it on its own vCPU:

```rust
// Kernel, kernel/core/src/init.rs. Same source in both builds.
fn ap_init() {
    init_on_each_cpu();
    ThreadOptions::new(ap_idle_loop)
        .cpu_affinity(CpuId::current_racy().into())
        .sched_policy(SchedPolicy::Idle)
        .spawn();
}
```

`init_on_each_cpu` initializes the scheduler, process, filesystem and time state for this vCPU. Spawning idle may immediately start scheduling on this vCPU. If execution returns from `ap_init`, the following `Task::yield_now()` asks the scheduler to select a Task.
The startup contract and registrations above define this proposal's entry sequence.

#### Prepare the startup registers {#startup-registers}

The initial instruction pointer names `enter_startup` below. Creation prepares registers containing the validated image entry and its two arguments; `startup_cpu_state` is passed separately to `enter_internal`:

```rust
// Proposed helper inside ostd/src/arch/x86/task/mod.rs, where the fields are private.
impl TaskContext {
    pub(crate) fn for_kernelet_startup(
        sp: usize, trampoline: usize, image_entry: u64, args: [u64; 2],
    ) -> Self {
        let mut ctx = Self::new();
        ctx.regs.rsp = sp as u64;
        ctx.rip = trampoline;
        ctx.regs.r13 = image_entry;
        ctx.regs.r14 = args[0];
        ctx.regs.r15 = args[1];
        ctx
    }
}

// Host OSTD creation. BSP arguments: services, boot; secondary: selector, id.
let start = TaskContext::for_kernelet_startup(
    startup_stack.end_vaddr() - 16, enter_startup as *const () as usize,
    image_entry_address, [first_argument, second_argument],
);

#[unsafe(naked)]
unsafe extern "C" fn enter_startup() -> ! {
    core::arch::naked_asm!(
        // resume_task_context has already installed the startup CPU snapshot.
        "mov rdi, r14", "mov rsi, r15",
        "jmp r13",
    );
}
```

### 2. Build a kernelet Thread and its internal Task

The existing kernel builder wraps the closure and attaches its internal Thread to Task data:

```rust
// Kernel: existing thread/kernel_thread.rs::ThreadOptions::build.
pub(crate) fn build(mut self) -> Arc<Task> {
    let task_fn = self.func.take().unwrap();
    let thread_fn = move || {
        let _ = oops::catch_panics_as_oops(task_fn);
        // Ensure that the thread exits.
        current_thread!().exit();
    };

    Arc::new_cyclic(|weak_task| {
        let thread = Arc::new(Thread::new(
            weak_task.clone(), KernelThread, self.cpu_affinity, self.sched_policy,
        ));
        TaskOptions::new(thread_fn).data(thread).build().unwrap()
    })
}
```

The kernel Thread builder stays unchanged. `TaskOptions::build` selects native allocation or the Host service with `cfg`.
Idle is built in the same way and counts against `max_tasks`; failure to allocate it stops startup. Its idle priority is used by the internal scheduler.
The calls from Thread spawn to Task enqueue are also unchanged:

```rust
impl ThreadOptions {
    pub(crate) fn spawn(self) -> Arc<Thread> {
        let task = self.build();
        let thread = task.as_thread().unwrap().clone();
        thread.run();
        thread
    }
}

impl Thread {
    pub(crate) fn run(&self) {
        self.task.upgrade().unwrap().run();
    }
}

impl Task {
    pub fn run(self: &Arc<Self>) {
        scheduler::run_new_task(self.clone());
    }
}
```

`Task::run` passes the Task to `run_new_task`; the scheduler decides when its stored closure starts.

### 3. Enqueue the Task, then select what runs next {#task-enqueue}

#### Enqueue a new or woken Task {#enqueue-runnable-task}

`run_new_task` enqueues the Task. If `enqueue` requests rescheduling on a CPU, it sets that CPU's request flag, then checks for preemption on the calling CPU. The new Task may run now or later.
`unpark_target` does the same enqueue/request steps for a woken Task, but leaves the calling CPU's preemption check to its caller.
Both builds use these native bodies; `set_need_preempt` and the lower CPU operations select the required implementation:

```rust
// Existing ostd/src/task/scheduler/mod.rs; no cfg needed for these bodies.
pub(super) fn run_new_task(runnable: Arc<Task>) {
    let preempt_cpu = scheduler_singleton().enqueue(runnable, EnqueueFlags::Spawn);
    if let Some(preempt_cpu_id) = preempt_cpu {
        set_need_preempt(preempt_cpu_id);
    }
    might_preempt();             // Existing native scheduling point.
}

pub(crate) fn unpark_target(runnable: Arc<Task>) {
    let preempt_cpu = scheduler_singleton().enqueue(runnable, EnqueueFlags::Wake);
    if let Some(preempt_cpu_id) = preempt_cpu {
        set_need_preempt(preempt_cpu_id);
    }
}

```

#### Notify the target CPU {#notify-scheduling-target}

`set_need_preempt` keeps its guard and same-CPU path in both builds.
For a remote target, native OSTD sends an IPI whose callback sets the target CPU's rescheduling flag.
vOSTD emulates this rescheduling IPI between vCPUs with two operations: `request_preemption` records the target's request, and `vcpu_notify` provides the Host notification that wakes its vCPU Thread if blocked in idle.
Together they form the virtual IPI path for rescheduling; `vcpu_notify` supplies its notification mechanism, while vOSTD determines what the request means and how to handle it.
The `might_preempt()` above is a scheduling checkpoint on the calling CPU: it checks whether rescheduling is requested and currently allowed, then asks the local scheduler to select a Task.
It may return without switching; it cannot run the target vCPU's scheduler for it:

```rust
// ostd/src/task/scheduler/mod.rs: keep the native guard and local path.
fn set_need_preempt(cpu_id: CpuId) {
    let preempt_guard = disable_preempt();
    if preempt_guard.current_cpu() == cpu_id {
        cpu_local::set_need_preempt(); // Selected CPU-local backend in either build.
    } else {
        #[cfg(not(feature = "kernelet"))]
        {
            crate::smp::inter_processor_call(&CpuSet::from(cpu_id), || {
                cpu_local::set_need_preempt();
            });
        }
        #[cfg(feature = "kernelet")]
        {
            // vOSTD: record the target CPU's internal scheduling request.
            cpu_local::request_preemption(cpu_id);
            // Enter Host OSTD: wake the target's vCPU Thread if idle-blocked.
            let status = (service_table().vcpu_notify)(cpu_id.as_usize() as u32);
            assert_eq!(status, 0, "notification of a configured vCPU failed");
        }
    }
}
```

Suppose vCPU 1 has put its vCPU Thread to sleep in [vcpu_wait](tasks.md#waiting), and vCPU 0 wakes an internal Task pinned to vCPU 1.
Native `ClassScheduler` gives a non-idle Task higher priority than idle, so enqueue requests rescheduling on vCPU 1. An equal-priority Task on a busy CPU need not cause such a request.
The request flag changes only kernelet memory. Host does not inspect that memory, so `vcpu_notify(1)` must wake the sleeping vCPU Thread through its [native Waker](synchronization.md#vcpu-wait). Otherwise the queued Task waits until an unrelated event wakes the vCPU.

After Host selects the woken vCPU Thread, vCPU 1 returns from idle wait and checks its internal queue. Notification does not choose an internal Task or guarantee immediate Host scheduling.
A Host-descheduled but runnable vCPU does not need waking. A notification arriving just before idle sleeps is remembered by the event-sequence check. Local requests need no Host notification because their vCPU is already executing.

This virtual IPI path describes request publication and idle wakeup.
`vcpu_notify` does not itself interrupt an already-running internal Task, execute a remote callback, or wait for the target to handle the request.
The proposed [tick-return checkpoint](interrupts-and-time.md#tick-return) can check requests on a running target, but that path and its response timing remain **[unverified]**. `vcpu_notify` alone does not provide it.

#### Store the scheduling request {#scheduling-request-state}

The remote branch of `set_need_preempt` above runs in vOSTD: it records the request, then enters Host OSTD through `vcpu_notify` to wake an idle target.
The flag helpers below all execute in vOSTD; their comments identify where each is used in that flow.

```rust
// vOSTD: ostd/src/task/preempt/cpu_local/kernelet.rs (proposed backend).
// Per-instance state. Host OSTD does not access this array.
static NEED_PREEMPT: [AtomicBool; MAX_VCPUS as usize] =
    [const { AtomicBool::new(false) }; MAX_VCPUS as usize];

// 1. Sending CPU: set_need_preempt(target) calls this before vcpu_notify.
pub(in crate::task) fn request_preemption(cpu: CpuId) {
    NEED_PREEMPT[cpu.as_usize()].store(true, Ordering::Release);
}

// Same-CPU request: no Host wakeup is needed.
pub(in crate::task) fn set_need_preempt() {
    request_preemption(CpuId::current_racy());
}

pub(in crate::task) fn need_preempt() -> bool {
    NEED_PREEMPT[CpuId::current_racy().as_usize()].load(Ordering::Acquire)
}

// 2. Target CPU: might_preempt() calls this at a scheduling checkpoint.
pub(in crate::task) fn should_preempt() -> bool {
    internal_switch_allowed() && need_preempt()
}

// 3. Target CPU: select_next_task() clears the request before queue selection.
pub(in crate::task) fn clear_need_preempt() {
    NEED_PREEMPT[CpuId::current_racy().as_usize()].swap(false, Ordering::AcqRel);
}
```

`CpuId` keeps its OSTD name and identifies a vCPU in this build; initialization validates it against the array bound.
Host OSTD uses its own native `PREEMPT_INFO` to schedule Host Tasks.
`internal_switch_allowed` checks the [virtual guards and transition state](synchronization.md#virtual-guards).
The target's next step is the vOSTD checkpoint below; asynchronous entry into that checkpoint still depends on the [tick-return integration](interrupts-and-time.md#tick-return).

#### Select a Task at a scheduling checkpoint {#scheduling-checkpoint}

`might_preempt` connects a pending request to an actual internal Task switch.
In vOSTD it first polls virtual events, which may make the interrupt-worker Task runnable, then checks `should_preempt`.
If there is no request or an internal guard forbids switching, it returns without selecting a Task and leaves any pending request for a later checkpoint.
Otherwise, the kernelet scheduler's `try_pick_next` decides whether another Task should run.

If a different Task is selected, `reschedule` calls `processor::switch_to_task`, whose vOSTD backend passes the selected resource to the Host `task_switch` service.
The kernelet chooses the Task; Host validates the resource and switches its stack and register context.
This switches Tasks within the current vCPU; Host scheduling of the vCPU itself is a separate operation.
After a switch, the outgoing Task continues this call and returns from `might_preempt` only when it is selected again.

Keep the native `ReschedAction` variants. Extract the existing queue-selection loop into `select_next_task`, shared by ordinary scheduling and [Task exit](tasks.md#task-exit).
The helper returns the selected Task after releasing run-queue access; each caller performs its own transfer.

```rust
// ostd/src/task/scheduler/mod.rs: native actions and selection logic.
// In the kernelet build, these functions execute in vOSTD.
// select_next_task extracts the existing loop; both builds use these functions.
enum ReschedAction {
    DoNothing,
    Retry,
    SwitchTo(Arc<Task>),
}

pub(crate) fn might_preempt() {
    #[cfg(feature = "kernelet")]
    crate::irq::poll_events_at_checkpoint(); // May enqueue the internal interrupt worker.
    // vOSTD: read the internal request and check virtual guards.
    if !cpu_local::should_preempt() { return; }
    // The kernelet kernel's scheduler chooses the next internal Task.
    reschedule(|rq| match rq.try_pick_next() {
        Some(next) => ReschedAction::SwitchTo(next.clone()),
        None => ReschedAction::DoNothing,
    });
}

fn reschedule<F>(select: F)
where
    F: FnMut(&mut dyn LocalRunQueue) -> ReschedAction,
{
    if let Some(next) = select_next_task(select) {
        // Still vOSTD. This wrapper enters Host OSTD via task_switch
        // after preparing the internal handoff (shown in the next section).
        processor::switch_to_task(next);
    }
}

fn select_next_task<F>(mut select: F) -> Option<Arc<Task>>
where
    F: FnMut(&mut dyn LocalRunQueue) -> ReschedAction,
{
    #[cfg(feature = "kernelet")]
    processor::begin_internal_scheduling(); // Exclude tick delivery before rq.current can change.
    cpu_local::clear_need_preempt(); // Before selection, so new requests survive.
    loop {
        let mut action = ReschedAction::DoNothing;
        scheduler_singleton().mut_local_rq_with(&mut |rq| action = select(rq));
        // Run-queue access has ended before returning the selected Task.
        match action {
            ReschedAction::DoNothing => {
                #[cfg(feature = "kernelet")]
                processor::cancel_internal_scheduling();
                return None;
            }
            ReschedAction::Retry => continue,
            ReschedAction::SwitchTo(next) => return Some(next),
        }
    }
}
```

Ordinary scheduling ends at the same `processor::switch_to_task(next)` call. `exit_current` uses the same selection helper, then calls `processor::exit_to_task` in vOSTD or `processor::switch_to_task` in the native build. Host OSTD receives the chosen resource; it never calls the kernelet's `try_pick_next`.

#### Start the idle Task {#start-idle-task}

During startup, `Task::current()` is None. Enqueueing idle may select it immediately; otherwise `__ostd_main` calls `Task::yield_now()` after kernel initialization returns.
Both paths reach `reschedule` and `switch_to_task`, starting idle through the ordinary Task-switch path.
Idle then creates the pinned interrupt-worker Thread. Later [event checks](interrupts-and-time.md#worker-delivery) wake that worker through the same enqueue functions.

### 4. vOSTD asks OSTD to switch stacks {#task-switch}

This path supplies the execution loop's [internal switch request](tasks.md#vcpu-loop). The service call stays suspended until the outgoing Task is selected again; returning to the vCPU loop does not return to that caller.

After the kernel scheduler selects `next`, native OSTD switches using `next.ctx`.
vOSTD instead passes `next.resource` to SERVICE `task_switch`; Host validates the resource and restores its stack and registers. Pre/post handlers still run inside the kernelet.

Keep the current/previous ownership convention from `ostd/src/task/processor.rs`:

```rust
// Existing OSTD storage and accessor, compiled into the kernelet image.
// Each non-null slot owns one Arc<Task> converted with Arc::into_raw.
cpu_local_cell! {
    static CURRENT_TASK_PTR: *const Task = core::ptr::null();
    static PREVIOUS_TASK_PTR: *const Task = core::ptr::null();
}

pub(super) fn current_task() -> Option<NonNull<Task>> {
    NonNull::new(CURRENT_TASK_PTR.load().cast_mut())
}
```

These are the kernelet image's own statics, addressed through the virtual CPU-local backend. They contain pointers to internal Tasks, never Host Tasks.
Host has its separate native `CURRENT_TASK_PTR`. The public `Task::current()` accessor is reused in each build and reads that build's processor state.

```rust
// ostd/src/task/processor/kernelet.rs: selected by cfg_attr above.
// Called by select_next_task before borrowing or changing the run queue.
pub(super) fn begin_internal_scheduling() {
    atomic_mode::might_sleep();
    // SAFETY: No internal RCU reader remains, and no run-queue borrow is held.
    unsafe { crate::sync::finish_grace_period(); }
    begin_internal_switch();
}

pub(super) fn cancel_internal_scheduling() {
    end_internal_switch(); // Selection left the current Task unchanged.
}

fn before_switching_to(next: Arc<Task>) -> Option<*mut c_void> {
    // select_next_task already established transition exclusion.
    let current = CURRENT_TASK_PTR.load();
    if core::ptr::eq(current, Arc::as_ptr(&next)) {
        end_internal_switch();
        return None; // No pre, transfer or post when current is selected again.
    }
    run_pre_schedule_handler(); // Outgoing is still current.
    let resource = next.resource.as_ptr();
    assert!(PREVIOUS_TASK_PTR.load().is_null());
    CURRENT_TASK_PTR.store(Arc::into_raw(next));
    PREVIOUS_TASK_PTR.store(current);
    Some(resource) // Both owning Arcs are now in the CPU-local slots.
}

pub(super) fn switch_to_task(next: Arc<Task>) {
    let Some(resource) = before_switching_to(next) else { return; };
    (service_table().task_switch)(resource); // SERVICE: suspend outgoing.
    // Reached when this suspended Task is selected again.
    // SAFETY: This Task has just resumed its suspended switch.
    unsafe { after_switching_to(); }
}

pub(super) fn exit_to_task(next: Arc<Task>) -> ! {
    let resource = before_switching_to(next).expect("exiting Task cannot select itself");
    (service_table().task_exit_to)(resource) // SERVICE: retire outgoing; never returns.
}

```

Transition exclusion starts before run-queue selection and ends after post on the incoming stack, or immediately when selection makes no change.
This covers the interval when the run queue names `next` but `CURRENT_TASK_PTR` still names the outgoing Task, as well as the later interval when that pointer changes before the stack.
A tick cannot enter either interval and attribute its callbacks to inconsistent current-Task state.
Host may pause this code, but must resume the interrupted instruction. During this interval its accounting uses `vcpu.active_task`, because the kernelet current pointer already names the next Task.
Host now performs the native stack-mapping and exclusive-use checks. vOSTD uses virtual guards and the switch/exit services instead of masking physical IRQs and switching directly.
`PREVIOUS_TASK_PTR` keeps the outgoing Task alive until the incoming Task finishes post.

Virtual ticks are handled with the interrupted Task still current, before it resumes ordinary execution or an internal switch proceeds, as specified in [Interrupts and time](interrupts-and-time.md#tick).

#### Run pre on the old Task and post on the new Task {#schedule-handlers}

The registration in step 1 uses the existing kernel handlers. vOSTD invokes those same image-local function pointers; only the guard backend changes:

```rust
// ostd/src/task/processor/kernelet.rs; caller holds transition exclusion.
fn run_pre_schedule_handler() {
    let irq_guard = crate::irq::disable_local(); // Virtual, scoped to this call.
    if let Some(handler) = PRE_SCHEDULE_HANDLER.get() {
        handler(&irq_guard);
    }
} // Drop the borrowed guard; keep the vCPU's switch-in-progress state.

fn run_post_schedule_handler() {
    if let Some(handler) = POST_SCHEDULE_HANDLER.get() {
        handler(); // CURRENT_TASK_PTR already identifies incoming.
    }
}
```

Here is the existing kernel pre-handler and its x86 supplementary-state save, from `kernel/core/src/thread/mod.rs` and `process/posix_thread/thread_local.rs`:

```rust
fn pre_schedule_handler(irq_guard: &DisabledLocalIrqGuard) {
    let Some(task) = Task::current() else { return; }; // Bootstrap has no Task.
    let Some(thread_local) = task.as_thread_local() else { return; };
    thread_local.supp_user_context().before_schedule(irq_guard);
}

// Existing SuppUserContext methods; x86-64 branch shown.
impl SuppUserContext {
    pub(crate) fn before_schedule(&self, guard: &DisabledLocalIrqGuard) {
        self.fpu.before_schedule(guard);
        self.fs_base.before_schedule(guard);
        self.gs_base.before_schedule(guard);
    }
}

// Existing CpuSync<R>::before_schedule; each field above uses this wrapper.
impl<R: UserReg> CpuSync<R> {
    pub(super) fn before_schedule(&self, guard: &DisabledLocalIrqGuard) {
        // self.reg is a memory copy. location tells whether to use it or the CPU value.
        let loc = self.location.get();
        if loc == CanonicalValueLocation::OnCpu {
            // Only hardware has the canonical value: copy it into self.reg.
            self.reg.borrow_mut().save_from_cpu_with_irq_disabled(guard);
        }
        if loc != CanonicalValueLocation::InMemory {
            // Both already has a valid memory copy; it needs no register save.
            self.location.set(CanonicalValueLocation::InMemory);
        }
    }
}
```

Pre runs on the outgoing Task's stack. For a Task with `ThreadLocal`, it prepares the memory copies of that thread's FPU state, FS base and user GS base for later use.
If a value is `OnCpu`, `before_schedule` copies it from hardware into `self.reg`. If it is `Both`, the memory copy is already up to date. If it is `InMemory`, the method keeps that memory value, which may include changes not yet loaded into hardware.
The method leaves `location` set to `InMemory`; it does not load another thread's register values.

After Host switches stacks, the incoming Task runs `after_switching_to`, which calls post.
`Task::current()` below reads the kernelet's own current pointer, already set to the incoming Task by `before_switching_to`. It does not return the vCPU's Host Task.
Post obtains that incoming thread's `VmSpace` and activates it if present through the virtualized page-table operation.

The service boundary follows the current root-management protocol: Host must keep the hardware root, saved continuation's root use and physical-CPU TLB tracking consistent.
If vOSTD only writes CR3, Host may omit that physical CPU from a later TLB shootdown, leaving a removed mapping usable there; a stale saved-root association can also restore the wrong address space.
vOSTD chooses the address space; Host commits its activation and the associated records together. The [root-retention contract](memory.md) defines the required failure ordering.

```rust
// Existing kernel post_schedule_handler body, statistics increment omitted.
fn post_schedule_handler() {
    let task = Task::current().unwrap(); // The internal Task now executing this post handler.
    let Some(thread_local) = task.as_thread_local() else { return; };
    let vmar = thread_local.vmar().borrow();
    if let Some(vmar) = vmar.as_ref() {
        vmar.vm_space().activate(); // vOSTD backend -> SERVICE: pt_activate.
    }
}
```

Post does not call `before_user_exec`. That happens later, when this thread's [user-mode loop](user-mode.md#user-round-trip) calls `pre_user_run` before entering user mode.
For each FPU/FS/user-GS group, the following native method loads the memory value if needed:

```rust
// Existing CpuSync<R>::before_user_exec, called through pre_user_run.
impl<R: UserReg> CpuSync<R> {
    pub(super) fn before_user_exec(&self, guard: &DisabledLocalIrqGuard) {
        if self.location.get() == CanonicalValueLocation::InMemory {
            self.reg.borrow().restore_to_cpu_with_irq_disabled(guard);
        }
        self.location.set(CanonicalValueLocation::OnCpu);
    }
}
```

#### Preserve a handler across a physical interrupt {#handler-physical-pause}

The [virtual guards](synchronization.md#virtual-guards) protect internal switching; Host physical IRQs can still interrupt pre, post or `before_user_exec`.
Interruption itself is harmless if the interrupted machine state is restored before execution continues.
The failure to prevent is Host handling or another thread scheduled by Host overwriting a still-live register that the return path does not restore.
For example, assume the current internal Task's FS state is `OnCpu`; this is a counterexample to an incomplete save/restore path:

```rust
// Kernelet pre-handler for the current internal Task:
self.fpu.before_schedule(guard);
// Physical IRQ arrives before this Task's FS value has been copied to memory.
// Host handling or another thread scheduled by Host loads its own FS base.
// BUG: return restores only TrapFrame, leaving the Host's FS base in the CPU.
self.fs_base.before_schedule(guard); // Incorrectly saves the Host's FS base for this Task.
self.gs_base.before_schedule(guard);
```

Restoring the instruction pointer alone resumes at the right line with the wrong input state; the internal Task's next user entry could then load the wrong TLS base.
The correct return restores the interrupted Task's FS base first, so the unfinished handler saves the Task's own value and completes normally.
If Host leaves a register untouched, or an existing path already preserves it, no additional save is needed for that register; the protocol must establish coverage for every state it can change.
Host therefore saves the actual machine state, not the handler's partly updated `CpuSync.reg`.
This is the asynchronous physical-interrupt path: save the interrupted `TrapFrame` and `CpuStateSnapshot` before Host handling can clobber them, whether or not Host subsequently schedules another Thread.
It differs from the voluntary internal `task_switch` call, which saves the outgoing Task at a known service-call boundary.
The following proposed Host OSTD code connects the [machine-state helpers](interrupts-and-time.md#machine-state-code) to the IRQ and return paths.

##### Save the interrupted state before Host Rust runs

The architecture IRQ prefix first saves **all** interrupted GPRs and the hardware return frame, before using registers to pass arguments to this function.
It calls this assembly-only gate with physical IRQs masked and active Host GS unchanged; the interrupted stack is retained, and execution has moved to a Host-controlled IRQ stack.
This gate is only for an IRQ that interrupted internal kernel execution with no earlier pause snapshot pending.
The entry prefix must classify that phase before choosing capture storage; IRQs in the vCPU loop or another Host phase use separate Host snapshot storage.
`interrupted` points to the vCPU's exclusive, preinitialized `pause_cpu_state`; `host` points to the retained `vcpu_loop_cpu_state`, captured before this internal entry.
Both FPU areas and the IRQ stack remain mapped under both roots.

```rust
// Host OSTD addition. Called only by the IRQ entry assembly after saving GPRs.
// SAFETY: trusted, live, disjoint snapshots; initialized XSTATE areas;
// Host GS active, physical IRQs masked, SysV call alignment on a Host IRQ stack.
#[unsafe(naked)]
unsafe extern "C" fn enter_host_irq_machine(
    interrupted: *mut CpuStateSnapshot,
    host: *const CpuStateSnapshot,
) {
    core::arch::naked_asm!(
        "push r12",          // Preserve the callee-saved register; align calls.
        "mov r12, rsi",      // Preserve host across capture's scratch-register use.
        "call {capture}",    // rdi = interrupted: save actual FS/GS/FPU/CR3/TSS.
        "mov rdi, r12",
        "call {install}",    // Install Host state before any compiled Host handler.
        "pop r12",
        "ret",               // Back to IRQ entry assembly, now in Host state.
        capture = sym capture_cpu_state,
        install = sym restore_cpu_state,
    );
}
```

Now the native Host IRQ handler can run without destroying the saved handler state.
This gate deliberately changes live TLS/FPU/root state; it must never be called from an ordinary Rust caller expecting its execution environment to remain unchanged.
An NMI needs its own stack and snapshot, including when it interrupts capture or install; it must not reuse `pause_cpu_state`.

##### Hand the completed IRQ to Host scheduling {#host-dispatch-stack}

The vCPU's Host Task already owns `kstack`; the same Thread uses this original stack for Host scheduling, internal-transfer handling and cleanup.
Before entering internal execution, it saves a return context on that stack, called `vcpu_loop_context` below.
The pause/transfer entry restores this context to continue the suspended call in the vCPU execution loop; it never resets the stack pointer to the stack's top.
No additional Host working stack is allocated.
The [entry loop](tasks.md#host-entry) shows how these temporary returns unwind normally before handling the next return.

After Host handlers, EOI and interrupt-level guard cleanup have finished, the eligible outer IRQ epilogue calls this function with physical IRQs still masked.
This branch is for interrupted kernelet handler execution; Host-service, nested-entry and stop classification remain the responsibility of the [IRQ epilogue](interrupts-and-time.md#host-irq-entry).
The vCPU's execution record and root-use owners already retain the interrupted resources, and `vcpu_loop_context` names the outstanding call that entered internal execution.
`irq_slot_busy` is the Host-owned reservation flag for this ordinary IRQ slot; releasing it does not free or unmap the IRQ stack.

```rust
// Host OSTD addition. pause_cpu_state was captured by enter_host_irq_machine.
// SAFETY: frame is the retained, complete kernel TrapFrame from IRQ entry.
// IRQ handlers have unwound; no owning local or live IRQ-stack borrow may
// survive the tail jump. vCPU state and both stacks are retained by Host owners.
unsafe extern "C" fn suspend_handler_after_irq(
    frame: *const TrapFrame,
    irq_slot_busy: *const core::sync::atomic::AtomicBool,
) -> ! {
    let saved_frame = unsafe { *frame }; // Copy; the original IRQ slot can retire.
    let (vcpu_loop_context, vcpu_loop_cpu_state) = with_current_vcpu(|vcpu| {
        assert!(vcpu.pause_frame.is_none());
        assert!(vcpu.return_reason.is_none());
        vcpu.pause_frame = Some(saved_frame);
        vcpu.return_reason = Some(VcpuReturnReason::Pause);
        // active_task and the kernelet's CURRENT_TASK_PTR are not changed.
        vcpu.take_vcpu_loop_return_pointers() // Consume the outstanding internal-entry return.
    }); // Release the state lock; only plain values remain on this stack.

    // IRQs stay masked until execution has left this stack; NMI uses another slot.
    // SAFETY: Host entry supplied this live slot's exclusively held reservation.
    unsafe { (*irq_slot_busy).store(false, core::sync::atomic::Ordering::Release); }
    unsafe { return_to_vcpu_loop(vcpu_loop_context, vcpu_loop_cpu_state) }
}
```

After the saved call in the vCPU execution loop returns to `enter_vcpu`, the proposed [Host return handler](tasks.md#vcpu-dispatch) consumes `VcpuReturnReason::Pause` and calls Host `scheduler::might_preempt()` after IRQ handling has ended.
This new checkpoint checks the native Host preemption request and guard count; ordinary x86 kernel IRQ return does not currently provide it.
If another thread scheduled by Host runs, this vCPU later resumes that same return-handler call; the interrupted internal handler has not run meanwhile.
Snapshot/stack ownership stays with the vCPU and execution records, not with an `Arc` or guard abandoned on the IRQ stack.
The IRQ prefix must acquire that reservation before capture and supply its authentic address; it cannot obtain either pointer from kernelet input. After release, IRQs stay masked through the stack jump so the ordinary slot cannot be reused while this code still runs on it.

##### Restore the handler through the assembly return gate

The canonical [resume branch](tasks.md#resume-decision) chooses `ResumeDecision::Interrupted`, installs the retained `CpuStateSnapshot`, and restores the interrupt frame through `iretq`.
The handler continues at the saved instruction; Host does not write `CpuSync.reg` or rerun pre/post.
Pending ticks still follow the [entry and deferral rules](interrupts-and-time.md#tick).

**[unverified]** IRQ/NMI entry storage, reservation, stop/exception recovery and virtual tick integration still require implementation.
Validation must interrupt between state-save/load operations, exercise nested entry and Host rescheduling, and check both the physical registers and vOSTD's logical state.

### Start a new Task or resume a saved Task {#task-first-entry}

Every internal Task starts through `ENTRY_FRESH_TASK` once, on the vCPU that first selects it. Secondary-vCPU bootstrap is a separate entry.
`task_create` prepares the new Task's stack and registers without running it. The initial instruction pointer names `first_internal_task`.
`image.entries.run_task` is the vOSTD function recorded in this proposal's validated [image entry table](../kernelet-api-service.md#entry-table). Host knows this fixed function, but never receives the Task's closure:

```rust
// ostd::kernelet::host
{
    // Host OSTD addition: task_create; resource and image entry are retained.
    let kstack = KernelStack::new_with_guard_page()?; // Existing OSTD allocator.
    let ctx = TaskContext::for_kernelet_task(
        kstack.end_vaddr() - 16, first_internal_task as *const () as usize,
        image.entries.run_task as usize as u64, Arc::as_ptr(&record).addr() as u64,
    );
    // SAFETY: record is still unpublished and initialized only by this creator.
    unsafe {
        record.ctx.get().write(ctx);
        record.kstack.get().write(Some(kstack)); // Previously None; record is unpublished.
    }
    // Initialize record.cpu_state and its FPU backing before publishing the map entry.
}
```

`Task::run` enqueues the Task and may start scheduling. On first selection, vOSTD runs pre, sets internal current to this Task and calls SERVICE `task_switch` with its resource pointer.
Host obtains exclusive use of the record, sets `vcpu.active_task` and calls [resume_task_context](tasks.md#transfer). It restores the prepared stack and `r12`/`r13`, then begins executing at `ctx.rip`, which points to `first_internal_task` below.
The same vCPU Thread executes this new stack:

```rust
// ostd::kernelet::host: the initial ctx.rip points here.
#[unsafe(naked)]
unsafe extern "C" fn first_internal_task() -> ! {
    core::arch::naked_asm!(
        "mov rdi, {fresh}", // First argument: ENTRY_FRESH_TASK.
        "mov rsi, r13",    // Second argument: this Task's resource address.
        "jmp r12",        // Enter this image's registered run_task function.
        fresh = const ENTRY_FRESH_TASK,
    );
}
```

The creation helper belongs beside the native context type, so Host resource code does not access its private fields:

```rust
// Proposed addition in ostd/src/arch/x86/task/mod.rs.
impl TaskContext {
    pub(crate) fn for_kernelet_task(
        sp: usize, trampoline: usize, run_task: u64, resource: u64,
    ) -> Self {
        let mut ctx = Self::new();
        ctx.regs.rsp = sp as u64;
        ctx.rip = trampoline;
        ctx.regs.r12 = run_task;
        ctx.regs.r13 = resource;
        ctx
    }
}
```

The jump passes `ENTRY_FRESH_TASK` and the resource address to vOSTD `run_task` on the new stack.
That branch calls `kernel_task_entry`, which runs post, takes `Task.func` and calls the closure:

```rust
// vOSTD adaptation: function installed in EntryTable::run_task.
// SAFETY: Caller establishes the selected stack, vCPU identity and bootstrap
// or fresh-Task state before entering; a fresh Task is entered exactly once.
#[cfg(feature = "kernelet")]
unsafe extern "C" fn run_task(entry: u32, arg: u64) -> ! {
    match entry {
        ENTRY_SECONDARY_VCPU => secondary_bootstrap(),
        ENTRY_FRESH_TASK => {
            // before_switching_to published and retained incoming before the service.
            let current = Task::current().expect("selected internal Task");
            let resource = current.resource.as_ptr();
            if resource.addr() as u64 != arg { crate::panic::abort(); }
            // SAFETY: Host claimed this resource and entered its fresh stack once.
            unsafe { kernel_task_entry() }
        }
        _ => crate::panic::abort(),
    }
}
```

The address check confirms that internal current matches Host's selected resource; vOSTD does not dereference Host memory.
When the Task later switches out, Host overwrites its initial `ctx` with the saved service-call registers and return address. The next selection resumes after that service call, not at `first_internal_task`, so the closure is not called again.
A physical Host pause also resumes the interrupted instruction rather than this first-entry function.

After an internal switch, Host has already moved execution to the incoming Task's stack, but vOSTD still holds the switch-in-progress state and the temporary reference to the outgoing Task.
The incoming Task calls `after_switching_to` to run its post-handler, end that internal exclusion, and release the reference to the outgoing Task.
These actions complete the transition started by the outgoing Task; they run on the incoming Task because the outgoing Task's call is now suspended.
They do not perform another stack switch or restart the outgoing Task.

```rust
// vOSTD: executes on the incoming Task's stack, after Host has switched to it.
pub(super) unsafe fn after_switching_to() {
    let previous = PREVIOUS_TASK_PTR.load();
    PREVIOUS_TASK_PTR.store(core::ptr::null()); // Consume this owning pointer once.
    let previous = if previous.is_null() {
        None // First entry from bootstrap.
    } else {
        // SAFETY: Published from the owning current slot by switch_to_task;
        // the slot is cleared before reconstructing its one Arc.
        Some(unsafe { Arc::from_raw(previous) })
    };
    // Host has already released the old stack's switched_to_cpu flag.
    run_post_schedule_handler(); // Incoming is current; its stack is executing.
    end_internal_switch();       // The internal transition is now complete.
    drop(previous);              // Release the reference retained for this transition.
}

// Native task entry moved out of build into task/mod.rs module scope.
// Shared body: the cfg-selected processor supplies after_switching_to.
#[unsafe(no_mangle)]
unsafe extern "C" fn kernel_task_entry() -> ! {
    // SAFETY: First entry after the processor has switched to this Task.
    unsafe { processor::after_switching_to(); }
    let current = Task::current().expect("Task is current");
    // SAFETY: Only the executing Task accesses its own func.
    let func = unsafe { current.func.get() }
        .take()
        .expect("task closure is consumed only on first entry");
    func();
    scheduler::exit_current(); // Internal exit uses SERVICE: task_exit_to.
}
```

On the first switch, `func()` runs `bsp_idle_loop`; after starting its [virtual interrupt worker](tasks.md#objects), it starts `first_kthread` through the same build → enqueue → switch path:

```rust
// Existing statements in kernel/core/src/init.rs::bsp_idle_loop.
ThreadOptions::new(first_kthread)
    .cpu_affinity(CpuId::bsp().into())
    .sched_policy(SchedPolicy::default())
    .spawn();
```

`first_kthread` finishes initialization and starts the init process. Later resumes continue its suspended calls without taking `func` again.
As a kernel Thread without `ThreadLocal`, it takes the early-return branch in the kernel pre/post handlers: no supplementary user-state save or user `VmSpace` activation is needed there.
The handler calls still occur, and post still increments the context-switch counter before that early return.
vOSTD must also complete `after_switching_to`, including ending internal exclusion and releasing the previous Task reference; Host still saves/restores the execution state needed to run the kernel Thread.

### Exit the current Task {#task-exit}

When the closure returns, the Thread wrapper marks exit and `scheduler::exit_current` removes the Task from its queue.
Before removal, finish required [virtual tick handling](interrupts-and-time.md#tick) and switch away from the user page-table root with SERVICE `pt_activate(kernel_pt_root)`. Root activation may wait, so finish it before blocking internal switching.
Exit removes current once and retries until another Task is available. It then calls `processor::exit_to_task` in vOSTD or `processor::switch_to_task` in the native build:

```rust
// Inside ostd/src/task/scheduler/mod.rs::exit_current, after the preparation above.
// Both builds share the native dequeue/selection logic.
let mut is_first_try = true;
let next = select_next_task(|rq: &mut dyn LocalRunQueue| {
    let next = if is_first_try {
        is_first_try = false;
        let should_pick_next = rq.update_current(UpdateFlags::Exit);
        let _current = rq.dequeue_current();
        should_pick_next.then(|| rq.pick_next())
    } else {
        rq.try_pick_next()
    };
    match next {
        Some(next) => ReschedAction::SwitchTo(next.clone()),
        None => ReschedAction::Retry,
    }
}).expect("exit selection retries until it has a successor");

#[cfg(not(feature = "kernelet"))]
{
    processor::switch_to_task(next); // Native exit uses the ordinary processor switch.
    unreachable!("exiting Task must never resume");
}
#[cfg(feature = "kernelet")]
processor::exit_to_task(next) // SERVICE task_exit_to; never returns.
```

After queue access ends, `exit_current` calls `exit_to_task`. As with an ordinary switch, this runs pre and updates current/previous pointers.
SERVICE `task_exit_to` tells Host that current has finished, so Host does not save a return to it. The next Task runs post and drops the outgoing Arc. The [root-retention contract](memory.md) defines when the old page tables and TLB mappings can be released.

## Host OSTD saves and restores execution {#host-contexts}

Host OSTD performs both internal stack transfers and physical pause/resume.
The kernelet scheduler selects internal Tasks; the Host scheduler selects their vCPU Threads.

### Preempt and resume a vCPU running an internal Task {#execution-flow}

This is the `Pause` path of the [vCPU execution loop](tasks.md#vcpu-loop): Host may preempt the vCPU Thread while its internal Task is paused, then select the vCPU Thread again.
Host may run another vCPU Thread or an ordinary native thread in between.
Each vCPU Thread has a native OSTD `Task` object that Host uses to save and restore its execution.
The examples name that object `vcpu_thread_task`; its `ctx` field stores this thread's scheduling context.
Two saved states serve different return positions:

- The internal Task's interrupt snapshot records its interrupted instruction, stack pointer, registers and supplementary CPU state.
- The vCPU Thread's scheduling context, stored in its native Task's `ctx` field, records the suspended Host scheduling call. Native switching saves it only when Host actually switches that thread out.

#### 1. The vCPU Thread executes an internal Task

Host has selected the vCPU Thread, which now executes kernelet code on the internal Task's stack.
Host still identifies the current native Task as `vcpu_thread_task`; entering internal execution does not select another native thread.

#### 2. A physical IRQ saves the internal execution state

A physical timer interrupt enters Host OSTD.
The [IRQ entry path](tasks.md#handler-physical-pause) preserves the internal Task's interrupted registers and supplementary CPU state before compiled Host handlers can overwrite them.
This snapshot will let the internal Task continue at the interrupted instruction, including an instruction inside an unfinished pre/post handler.

The native Host timer callback updates the current vCPU Thread's scheduling state and may record a request:

```rust
// Existing Host OSTD timer callback, inside enable_preemption_on_cpu.
scheduler_singleton().mut_local_rq_with(&mut |local_rq| {
    let should_pick_next = local_rq.update_current(UpdateFlags::Tick);
    if should_pick_next {
        cpu_local::set_need_preempt();
    }
});
```

`set_need_preempt` records a native Host CPU request to reconsider the running vCPU Thread.
It does not set vOSTD's internal scheduling flag or switch threads.
The same vCPU Thread is still executing, now in Host's interrupt handling path.

#### 3. The Host return path checks whether to switch threads

The physical IRQ initially directs the CPU to Host's interrupt handler, not to `might_preempt`.
The proposed [vCPU return path](tasks.md#host-pause-resume) must explicitly connect the end of that handler to the new checkpoint:

1. Before internal execution began, `enter_internal` saved a return position on the vCPU Thread's original stack in `vcpu_loop_context`. That call in the vCPU execution loop remains suspended while internal code runs.
2. After IRQ handling ends, the eligible IRQ epilogue retains the internal snapshot, records `VcpuReturnReason::Pause`, and calls the trusted `return_to_vcpu_loop` assembly gate. It does not return directly to the interrupted internal instruction on this branch.
3. The gate restores the original stack pointer and saved Host continuation. Execution continues after the suspended `enter_internal` call, and its callers return normally to the `enter_vcpu` loop.
4. That loop calls `handle_vcpu_return`. Its `Pause` branch explicitly calls Host `might_preempt`, shown below.

These are transfers and calls executed by the same physical CPU while the current native Task still belongs to the vCPU Thread.
The internal Task does not initiate the call, and the Host scheduler has not selected a different thread yet.
The saved `vcpu_loop_context` supplies the stack return in step 2; the native Task's `ctx` field is saved only if the later scheduling check actually switches threads.
This handoff is proposed integration, not an automatic effect of setting `need_preempt` or returning from an IRQ handler.

```rust
// Host OSTD, on the vCPU Thread's original stack.
// The internal Task's interrupt snapshot remains retained.
crate::task::scheduler::might_preempt();
// Returns directly without a switch, or after this vCPU Thread is selected again.
resume_vcpu_or_stop();
```

`might_preempt` checks native Host `need_preempt` and preemption guards before asking the Host run queue's `try_pick_next` to select a thread.
If no switch is needed, execution proceeds directly to step 6; the vCPU Thread's scheduling context does not need to be saved for this IRQ.
If another thread is selected, native switching proceeds to step 4.
Finishing an IRQ handler alone does not permit scheduling or cause a switch.

This added checkpoint applies to eligible vCPU execution, identified from the interrupted current native Task and its execution phase.
Ordinary native threads retain their existing IRQ return path.
An IRQ during the vCPU execution loop must preserve any earlier internal snapshot and follow the Host phase's return rules.
If native guards prevent the handoff, the interrupted Host operation retains the request and must reach an explicit deferred checkpoint after the critical section; dropping a guard does not itself check preemption.
vOSTD's virtual guards restrict internal switching, not Host preemption of the vCPU Thread.

The added check is necessary because native OSTD checks preemption before user entry in `UserContext::execute`, at the end of `halt_cpu`, and after new Task enqueue in `run_new_task`, but current x86 kernel IRQ return restores registers and executes `iretq` without calling `might_preempt`.
An internal Task can keep running without reaching any Host checkpoint; its vOSTD scheduling checks only inspect internal scheduling state.
Physical IRQ entry plus this Host checkpoint provides a path independent of voluntary Host service calls.

#### 4. Native switching saves the vCPU Thread's scheduling state

The current vCPU Thread has called Host OSTD's `might_preempt()` in step 3.
Execution is now inside that function and the native switching functions it calls; no other thread has started running yet.
The internal Task's interrupt snapshot remains retained while this call proceeds.
Native switching saves the vCPU Thread's current stack pointer, continuation address and callee-saved registers in the `ctx` field of the vCPU Thread's native Task, then restores the selected thread's context.
The saved continuation is inside the Host scheduling path, not at the internal Task's interrupted instruction.

While the other thread runs, Host retains both the vCPU Thread's suspended stack and the internal Task's snapshot and stack.
The internal Task remains selected within the vCPU; Host preemption does not invoke vOSTD to choose a replacement.

#### 5. Host selects the vCPU Thread again

Host restores the vCPU Thread's scheduling context from its native Task's `ctx` field, so that thread first resumes its unfinished Host scheduling call.
The native switching call and its callers continue until `might_preempt()` returns at the point shown in step 3.
The vCPU Thread is executing Host OSTD code again; the internal Task has not yet resumed.
Neither vCPU startup nor the internal Task's entry function is called again.

#### 6. The vCPU Thread restores internal execution

`resume_vcpu_or_stop` checks stop and resource validity under the [return protocol](tasks.md#vcpu-dispatch).
Once return conditions allow and required [virtual tick handling](interrupts-and-time.md#tick) is complete, `enter_internal` saves a fresh return position for the vCPU loop, then restores the retained supplementary CPU state and interrupt frame, including the internal stack pointer and instruction pointer.
For this pause, `prepare_resume` selects `ResumeDecision::Interrupted`; the saved interrupt frame determines where execution resumes. It does not use the internal Task's voluntary switch context as a substitute.
The interrupted internal Task continues from that position.
This Host preemption sequence adds no internal Task selection or pre/post pair; virtual tick handling remains subject to its own delivery and internal scheduling rules.

**[unverified]** The added Host checkpoint, stack return gates and deferred paths require implementation and machine validation.
The sequence establishes ordering, not a maximum preemption delay; physical IRQ delivery and native critical-section duration still require measurement.
The [implementation obligations](tasks.md#obligations) include switching out a continuously running internal Task and restoring its interrupted state.

### Reuse native TaskContext {#saved-task-context}

The vCPU Thread's native OSTD Task and each internal Task's execution record use separate values of OSTD's existing `TaskContext` type:

```rust
#[repr(C)]
struct TaskContext {
    regs: CalleeRegs,
    rip: usize, // Address at which the suspended call will continue.
}

#[repr(C)]
struct CalleeRegs {
    rsp: u64, // Position in this Task's own stack; the stack allocation remains alive.
    rbx: u64,
    rbp: u64,
    r12: u64,
    r13: u64,
    r14: u64,
    r15: u64,
}
```

The layout is native; the architecture-owned constructors above are proposed additions.
Keep compile-time assertions in `ostd/src/arch/x86/task/mod.rs` beside the private fields used by the assembly offsets:

```rust
const _: () = {
    assert!(core::mem::size_of::<TaskContext>() == 64);
    assert!(core::mem::offset_of!(TaskContext, regs) == 0);
    assert!(core::mem::offset_of!(TaskContext, rip) == 56);
    assert!(core::mem::offset_of!(CalleeRegs, rsp) == 0);
    assert!(core::mem::offset_of!(CalleeRegs, rbx) == 8);
    assert!(core::mem::offset_of!(CalleeRegs, rbp) == 16);
    assert!(core::mem::offset_of!(CalleeRegs, r12) == 24);
    assert!(core::mem::offset_of!(CalleeRegs, r13) == 32);
    assert!(core::mem::offset_of!(CalleeRegs, r14) == 40);
    assert!(core::mem::offset_of!(CalleeRegs, r15) == 48);
};
```

Interrupt restoration uses the separate [physical-pause protocol](tasks.md#handler-physical-pause).

### State stored for each vCPU {#vcpu-state}

`vcpu_thread_task` is the Host OSTD Task belonging to this vCPU Thread; `active_task` is the execution record of the internal Task currently using the vCPU.
Stop and resource-lifetime state are defined in the [service contract](../kernelet-api-service.md).

```rust
// Proposed Host OSTD state excerpt; backing resources are prepared before start.
// Phase/root/service records and per-physical-CPU IRQ-slot reservations are omitted;
// their ownership and nesting are specified in Interrupts and time.
struct Vcpu {
    vcpu_thread_task: Arc<ostd::task::Task>, // Native Task scheduled by Host.
    active_task: Option<Arc<TaskExecRecord>>, // Current internal execution resource.

    vcpu_loop_context: SyncUnsafeCell<TaskContext>, // Return position in the vCPU loop.
    vcpu_loop_cpu_state: CpuStateSnapshot, // Supplementary state for that return.
    loop_return_pending: bool, // Prevent restoring the same loop call twice.
    return_reason: Option<VcpuReturnReason>, // Reason consumed by the loop.

    pause_frame: Option<TrapFrame>, // Interrupted internal execution.
    pause_cpu_state: CpuStateSnapshot, // Supplementary state of that interruption.
    internal_execution_started_at_ns: Option<u64>, // Some only during counted internal execution.
    internal_execution_total_ns: u64, // Closed internal intervals on this vCPU; initially zero.
    attributed_execution: u64, // Execution time already attributed to internal Tasks.
}

// Return reasons consumed by the vCPU loop.
enum VcpuReturnReason {
    Pause,
    SwitchTask { next: *mut c_void, captured: TaskContext },
    Exit { next: *mut c_void },
    Stop,
}
```

### Prepare startup state and retain entry resources {#startup-entry-state}

The [entry gates](tasks.md#internal-entry-gate) save and restore the pending vCPU-loop call. Their preparation helpers supply retained storage:

- `prepare_vcpu_loop_return` reserves that return slot before entry; `enter_internal` writes the registers into it.
- `take_vcpu_loop_return_pointers` consumes the reservation before restoration. `loop_return_pending` rejects overlapping entry or duplicate return.
- `prepare_current_vcpu_loop_return` applies the same preparation to the current vCPU. All three helpers briefly hold the vCPU state lock with physical IRQs masked and release it before transferring execution.

Save the stack pointer of the actual pending call, not `kstack.end_vaddr()`, which is only the allocation boundary.
Host selects `resume_task_context` for startup or a selected Task and `resume_kernelet_vcpu` for a retained kernel interrupt frame; vOSTD supplies neither restore-gate address.
Physical IRQs remain masked throughout the transfer. Publish the target phase before enabling them; no blocking call belongs in that interval.

Allocate startup stacks and loop/IRQ save areas before `start`; the native Task already owns the vCPU Thread's original stack.
`task_create` prepares each later internal Task's stack and saved state before publication.
Keep every stack, snapshot and root used by the gates mapped until its final use ends. Retire the startup stack only after the first transfer has left it and finished capturing from it.
The [startup register helper](tasks.md#startup-registers) and [first-Task helper](tasks.md#task-first-entry) initialize the two initial contexts.

### Prepare a new Task's first instruction {#restore-context}

Task construction and its entry function are defined in [first entry](tasks.md#task-first-entry).
At the initial x86-64 C ABI function entry, the restored stack pointer must be 8 modulo 16, matching the stack position after a normal call pushes its 8-byte return address.
The examples set the saved stack pointer to the aligned stack top minus 16.
`restore_task_context` writes the entry address at that position and executes `ret`, adding eight to SP; the compiled entry therefore sees stack top minus 8, as required.

### Save the Task at its switch call {#save-service-call}

The [vCPU loop overview](tasks.md#vcpu-loop) defines the internal-switch flow and service return semantics.
The capture fragment below starts after the required caller/stack validation and physical IRQ masking.
It cannot be installed directly in `ServiceTable`: the omitted prefix must preserve the original service-call registers and stack position before reaching it.
That prefix and its failure path remain **[unverified]**.

At SERVICE `task_switch` entry, capture callee registers and the return address before Host Rust code changes the call frame:

```rust
// ostd::kernelet::host
// Host OSTD capture fragment, NOT the complete ServiceTable entry. The prefix must validate the calling vCPU,
// controlled stack and 64-byte capture area without altering the call boundary.
// rdi contains next_resource, from the service-table call in step 4.
#[unsafe(naked)]
unsafe extern "C" fn task_switch_entry(next_resource: *mut c_void) {
    core::arch::naked_asm!(
        "sub rsp, 64",         // Temporary TaskContext; not a new Task or stack.
        "lea rax, [rsp + 64]", // rsp at the original service-call entry.
        "mov [rsp + 0], rax",
        "mov [rsp + 8], rbx",
        "mov [rsp + 16], rbp",
        "mov [rsp + 24], r12",
        "mov [rsp + 32], r13",
        "mov [rsp + 40], r14",
        "mov [rsp + 48], r15",
        "mov rax, [rsp + 64]", // Original return address, back into vOSTD.
        "mov [rsp + 56], rax",
        "mov r12, rdi",        // Keep next_resource after saving the original r12.
        "mov r13, rsp",        // Keep the temporary TaskContext address.
        "mov rax, [rip + {slot_offset}]",
        "mov rdi, gs:[rax]",   // Host-installed current execution's CpuStateSnapshot.
        "sub rsp, 8", "call {capture_cpu}", "add rsp, 8",
        "mov rdi, r12", "mov rsi, r13",
        "jmp {enter_host}",
        slot_offset = sym CURRENT_TASK_CPU_STATE_OFFSET,
        capture_cpu = sym capture_cpu_state,
        enter_host = sym return_switch_to_vcpu_loop,
    );
}

// with_current_vcpu identifies Host current, masks physical IRQs and locks its
// vCPU record only for this closure; it returns with no state borrow or lock.
// Called by the capture tail above; never returns on the outgoing stack.
unsafe extern "C" fn return_switch_to_vcpu_loop(
    next: *mut c_void, captured: *const TaskContext,
) -> ! {
    // The omitted, validated entry prefix must have masked physical IRQs and captured CpuStateSnapshot
    // before Host Rust, and retained the outgoing stack. Only plain register
    // values are copied; no Arc/guard from this call survives the stack change.
    let (vcpu_loop_context, vcpu_loop_cpu_state) = with_current_vcpu(|vcpu| {
        assert!(vcpu.return_reason.is_none());
        vcpu.return_reason = Some(VcpuReturnReason::SwitchTask {
            next, captured: unsafe { captured.read() }, // Opaque address + Host-captured registers.
        });
        vcpu.take_vcpu_loop_return_pointers() // Consume the outstanding internal-entry return.
    });
    unsafe { return_to_vcpu_loop(vcpu_loop_context, vcpu_loop_cpu_state) }
}
```

The `task_exit_to` service reaches the same vCPU Thread's original stack without constructing an outgoing `TaskContext`:

```rust
// ostd::kernelet::host
// Host OSTD addition, after the exit service's validated, IRQ-masked entry prefix has captured the actual CPU state and cleaned up its entry obligations.
unsafe extern "C" fn task_exit_to_entry(next: *mut c_void) -> ! {
    let (vcpu_loop_context, vcpu_loop_cpu_state) = with_current_vcpu(|vcpu| {
        assert!(vcpu.return_reason.is_none());
        vcpu.return_reason = Some(VcpuReturnReason::Exit { next });
        vcpu.take_vcpu_loop_return_pointers() // Consume the outstanding internal-entry return.
    });
    // No owning locals remain; the outgoing stack will never be resumed.
    unsafe { return_to_vcpu_loop(vcpu_loop_context, vcpu_loop_cpu_state) }
}
```

`CURRENT_TASK_CPU_STATE_OFFSET` locates a private pointer in Host CPU-local storage. Host initializes the offset once and installs the pointer for the current execution before entry or resume. Entry code validates the caller and disables physical IRQs before reading it; the kernelet cannot supply this pointer.
The return gate follows the [vCPU loop return protocol](tasks.md#vcpu-loop-return-gate), including one-time context consumption and restoration of the loop's CPU state before Host scheduling.

#### Save the old Task, then change active_task {#transfer}

The vCPU Thread now uses its original stack. Save the outgoing internal Task's context and end its exclusive execution use before activating the selected next Task.
Entry assembly has already saved `CpuStateSnapshot` in the outgoing record. The code below saves `TaskContext` under the same protection used when exit disables that resource:

```rust
// ostd::kernelet::host
// New Host OSTD code; already on vcpu_thread_task.kstack. No kernelet code can execute here.
impl Vcpu {
    unsafe fn suspend_current(
        &mut self, captured: Option<TaskContext>, host_execution_total: u64,
    ) -> Result<Option<Arc<TaskExecRecord>>, &'static str> {
        self.attribute_execution(host_execution_total)?;
        let previous = self.active_task.take();
        if let Some(task) = &previous {
            if let Some(context) = captured {
                unsafe { task.ctx.get().write(context); } // The actual Task context save.
            }
            // Exit must set execution_disabled before releasing stack ownership.
            // Stack/context/root uses have moved to Host-owned storage or drained.
            task.switched_to_cpu.store(false, Ordering::Release);
        } // Bootstrap instead retires the now-unused startup stack.
        Ok(previous)
    }
}
```

The current `ClassScheduler::select_cpu` returns a Thread's `last_cpu` on wake; first enqueue establishes that CPU.
Its normal scheduling path does not move a Task to another vCPU while the previous vCPU is still switching away.
There is therefore no normal target-stack contention to wait for in this protocol.
OSTD still validates the resource and atomically claims execution ownership, since an invalid or duplicate resource request must not cause concurrent use of one stack.
Failure is a protocol error, not a blocking wait:

```rust
// Host OSTD: validate and claim the selected execution resource without waiting.
impl Kernelet {
    fn claim_task(&self, resource: *mut c_void) -> Result<Arc<TaskExecRecord>, TransferError> {
        let _irq_guard = crate::irq::disable_local(); // Native IRQ exclusion before resource locking.
        let tasks = self.tasks.lock();
        if self.is_stopping() { return Err(TransferError::Stopped); }
        let Some(allocation) = tasks.get(&resource.addr()) else {
            return Err(TransferError::Invalid);
        };
        if !allocation.vostd_owner || allocation.execution_disabled {
            return Err(TransferError::Invalid);
        }
        allocation.record.switched_to_cpu.compare_exchange(
            false, true, Ordering::Acquire, Ordering::Relaxed,
        ).map_err(|_| TransferError::Invalid)?; // Already in use: invalid transfer.
        Ok(allocation.record.clone())
    }
}
```

Validate and retain the next resource before releasing the current one.
A failed final claim enters the [transfer-failure stop path](tasks.md#stack-return).
Move the acquired Arc into `active_task` under the switch protection and mark first execution as started. A stop racing this update is caught by the final return check, which disables the selected resource, releases its execution use and returns to the pending call in the vCPU loop.

`active_task` keeps the selected context and stack alive through the [return check](tasks.md#vcpu-dispatch) and register restore.
`resume_task_context` installs [CpuStateSnapshot](interrupts-and-time.md#machine-state-code), then restores the remaining registers. No Rust code runs after restoring TLS/FPU:

```rust
// ostd::kernelet::host
#[unsafe(naked)]
unsafe extern "C" fn resume_task_context(
    ctx: *const TaskContext, cpu_state: *const CpuStateSnapshot,
) -> ! {
    core::arch::naked_asm!(
        "mov r12, rdi", // Outgoing registers are already saved; r12 is scratch here.
        "mov rdi, rsi",
        "sub rsp, 8", // Align the assembly-only call.
        "call {install}",
        "mov rdi, r12",
        "jmp {restore}",
        install = sym restore_cpu_state,
        restore = sym restore_task_context,
    );
}
```

This assembly follows the native context-restore register order, using the selected record:

```rust
// ostd::kernelet::host
// New Host restore gate: native first_context_switch register order plus STI.
// Requires retained target context/stack and the correct return environment.
#[unsafe(naked)]
unsafe extern "C" fn restore_task_context(next: *const TaskContext) -> ! {
    core::arch::naked_asm!(
        "mov rsp, [rdi + 0]",   // Use the selected Task's saved stack position.
        "mov rbx, [rdi + 8]",
        "mov rbp, [rdi + 16]",
        "mov r12, [rdi + 24]",
        "mov r13, [rdi + 32]",
        "mov r14, [rdi + 40]",
        "mov r15, [rdi + 48]",
        "mov rax, [rdi + 56]",  // First-entry address or saved service-call return.
        "mov [rsp], rax",
        "sti", // Complete the physical-IRQ exclusion; ret is the shadowed instruction.
        "ret",
    );
}
```

The restore gates follow the [entry resource and mapping requirements](tasks.md#startup-entry-state).
The build must use no red zone or cross-image unwinding; the restore sequence keeps physical IRQs disabled through `sti; ret`.
**[unverified]** Entry integration, stack-reserve sizing and machine tests are still required.

### A physical interrupt pauses the same vCPU Thread {#run-to-cpu}

The pause and restore sequence is defined in the [execution-flow section](tasks.md#execution-flow); this section supplies service identity checks and IRQ entry integration.

#### Identify the calling vCPU Thread {#vcpu-execution-identity}

OSTD identifies the calling vCPU from the native Task of the currently running vCPU Thread, recorded by native `CURRENT_TASK_PTR`.
It matches that Task to the instance's `vcpu_thread_task` record before accessing the vCPU's execution resources.
The caller cannot select another vCPU's record by supplying an ID.
Internal Task switches leave this native Thread identity unchanged.

#### Save the interrupted state and enter the vCPU execution loop {#host-pause-resume}

The [IRQ handoff code](tasks.md#handler-physical-pause) saves the interrupted state and returns to the vCPU loop.
`with_current_vcpu` uses the [calling-thread identity](tasks.md#vcpu-execution-identity) to find the vCPU record, then holds its state lock with physical IRQs disabled while executing the supplied closure.
The protected data is OSTD's `Vcpu` struct for the calling vCPU Thread.
For the IRQ return shown earlier, the closure writes `pause_frame` (the interrupted internal Task's register frame) and `return_reason = Some(VcpuReturnReason::Pause)` (the work to process in the vCPU loop).
Internal switching updates `active_task`, and execution-time accounting updates `attributed_execution` under the same lock.
These are concrete field updates to `Vcpu`, not changes to the scheduler's run queue.
For example, the vCPU Thread may hold this lock while writing `return_reason`.
If a timer IRQ interrupts it and the callback requests the same lock, the callback waits for the interrupted code to release the lock, but that code cannot continue until the callback returns.
Keeping physical IRQs disabled during this short update prevents that deadlock on the same CPU.
The closure may inspect or update the record, but must not sleep or switch stacks; the helper releases the lock and restores the prior IRQ state before returning.
Tick accounting and `active_task` updates use the same protection.

Entry also records whether it interrupted kernelet or Host service code and keeps the required stack and page tables alive. It leaves `active_task` unchanged. Nested IRQs and NMIs use separate storage.
An ordinary IRQ return resumes the interrupted OSTD service at its interrupted instruction. Service completion and cancellation follow the [ordinary service contract](../kernelet-api-service.md#ordinary-service-return).
When an IRQ interrupts user-mode execution, the [user_run return path](user-mode.md#user-context-stack) first saves the user registers and returns execution to OSTD kernel code.
Any subsequent handoff to the vCPU loop must preserve that kernel continuation while keeping the user context alive.
The kernel-context restore path shown here cannot directly substitute for the user-mode return path; the [user-return contract](user-mode.md#user-context-stack) defines that transition.

#### Host preemption checkpoint contract {#host-preemption-checkpoint}

The [nested preemption sequence](tasks.md#execution-flow) defines the checkpoint, deferred requests and save/restore ordering; the [implementation obligations](tasks.md#obligations) track validation.

### Resume Host scheduling, then restore internal execution {#vcpu-dispatch}

The following functions implement the [vCPU loop](tasks.md#vcpu-loop) using the return reason already described there.

```rust
// ostd::kernelet::host
// IRQs are masked on return from internal execution. Consume exactly one reason.
fn take_vcpu_return_reason() -> VcpuReturnReason {
    with_current_vcpu(|vcpu| {
        vcpu.return_reason.take().expect("internal execution returned without a reason")
    })
}

// Host OSTD addition. Some(reason) is the next internal return; None completes stop.
fn handle_vcpu_return(reason: VcpuReturnReason) -> Option<VcpuReturnReason> {
    // Publish vCPU-loop phase before enabling IRQs. Nested IRQs now use
    // native Host entry; they cannot reset this vCPU Thread's original stack.
    enter_vcpu_loop_phase();
    unsafe { crate::arch::irq::enable_local(); }

    match reason {
        VcpuReturnReason::Pause => {
            settle_vcpu_execution(); // Host collector -> attribute_execution, once.
            // The vCPU Thread runs here; the internal Task's frame remains saved.
            // If this Thread is switched out, native context_switch saves into vcpu_thread_task.ctx.
            crate::task::scheduler::might_preempt(); // Host PREEMPT_INFO, not vOSTD flags.
            // Either no switch occurred, or this vCPU Thread was selected again
            // and its native scheduling call has now returned.
            // Still on the vCPU Thread's original stack; internal execution has not resumed yet.
            resume_vcpu_or_stop()
        }
        VcpuReturnReason::SwitchTask { next, captured } => {
            complete_internal_switch(next, Some(captured))
        }
        VcpuReturnReason::Exit { next } => complete_internal_switch(next, None),
        VcpuReturnReason::Stop => finish_vcpu(),
    }
}
```

For a switch, `next` is the resource chosen by vOSTD. Host saves current, acquires next and prepares to resume:

```rust
// ostd::kernelet::host
fn complete_internal_switch(
    next: *mut c_void, captured: Option<TaskContext>,
) -> Option<VcpuReturnReason> {
    let instance = current_kernelet(); // Retained Host instance, not a kernelet object.
    let Some(selected) = instance.resolve_task(next) else {
        instance.request_stop();
        drop(instance);
        return finish_vcpu();
    };
    let exiting = captured.is_none();
    let total = settle_vcpu_execution(); // Close Task execution; dispatch is framework work.
    let saved = with_current_vcpu(|vcpu| {
        // Lock order: vCPU state -> instance resource map.
        let mut tasks = instance.tasks.lock();
        if exiting { vcpu.finish_current_execution(&mut tasks); }
        // Prefix already moved all stack/IRQ uses off the outgoing stack.
        unsafe { vcpu.suspend_current(captured, total) }
    });
    match saved {
        Ok(previous) => drop(previous),
        Err(_) => { drop(selected); instance.request_stop(); drop(instance);
                    return finish_vcpu(); }
    }

    // Validate exclusive ownership; a conflict is a protocol error, not a wait.
    let claimed = instance.claim_task(next);
    drop(selected);
    match claimed {
        Ok(record) => with_current_vcpu(|vcpu| {
            let mut tasks = instance.tasks.lock();
            // The successful claim retains the allocation; stop cannot remove it
            // until this vCPU has released its execution use.
            tasks.get_mut(&next.addr()).unwrap().started = true;
            vcpu.active_task = Some(record); // Transfer the claim into persistent ownership.
        }),
        Err(_) => { instance.request_stop(); drop(instance);
                    return finish_vcpu(); }
    }
    drop(instance); // Release the temporary owner before entering internal execution.
    resume_vcpu_or_stop()
}
```

On exit, `finish_current_execution` marks the counters final and sets `execution_disabled` under the map lock. Bootstrap has no current Task allocation.
Before any target instruction runs, both pause and switch check stop and resource ownership.

### Check whether internal execution may resume {#resume-decision}

Host CFS already controls the vCPU Thread's CPU allocation; the vCPU loop adds no budget check or wait.
After the Host scheduling call returns, `prepare_resume` checks whether the instance is stopping and whether the required resource and page-table state can be prepared.
It returns `Stop` for stopping or failed preparation, or the saved context to restore.
Stop, failed restoration and pending-service cleanup follow [vCPU stop contract](tasks.md#stack-return); this section only selects the corresponding control-flow branch.
The two successful choices are `Interrupted` for the retained interrupt frame and `Selected` for the selected internal Task's saved call context.
The [virtual tick entry](interrupts-and-time.md#tick) must be integrated before eligible ordinary internal resumption; the code below does not implement that additional entry:

```rust
// ostd::kernelet::host
// Host OSTD addition. These are return choices, not Task lifecycle states.
// Raw pointers are backed by persistent vCPU/active-resource owners.
// The caller keeps physical IRQs masked through restore; stop cannot free those owners.
enum ResumeDecision {
    Stop,
    Interrupted { frame: TrapFrame, cpu_state: *const CpuStateSnapshot },
    Selected { ctx: *const TaskContext, cpu_state: *const CpuStateSnapshot },
}

fn prepare_resume() -> ResumeDecision {
    with_current_vcpu(|vcpu| {
        let interrupted = vcpu.pause_frame.is_some();
        if interrupted && vcpu.paused_in_host_service() {
            // A live Host service must resume to complete/cancel its own obligations.
            // Stop cannot discard its stack here; its epilogue prevents kernelet reentry.
            if vcpu.stop_requested() { vcpu.propagate_service_cancellation(); }
        } else if vcpu.stop_requested() {
            return ResumeDecision::Stop;
        }
        let prepared = if interrupted {
            vcpu.prepare_paused_root_and_cpu_slot()
        } else {
            vcpu.prepare_active_root_and_cpu_slot()
        };
        if prepared.is_err() {
            vcpu.request_stop();
            return ResumeDecision::Stop; // Preserve frame/owners for Host recovery.
        }
        if interrupted {
            ResumeDecision::Interrupted {
                frame: vcpu.pause_frame.take().unwrap(),
                cpu_state: &vcpu.pause_cpu_state,
            }
        } else {
            let task = vcpu.active_task.as_ref().expect("selected resource");
            ResumeDecision::Selected {
                ctx: task.ctx.get().cast_const(),
                cpu_state: task.cpu_state.get().cast_const(),
            }
        }
    })
}

fn resume_vcpu_or_stop() -> Option<VcpuReturnReason> {
    let irq_guard = crate::irq::disable_local(); // Physical Host IRQ exclusion.
    match prepare_resume() {
        ResumeDecision::Stop => {
            drop(irq_guard);
            finish_vcpu()
        }
        ResumeDecision::Interrupted { frame, cpu_state } => {
            core::mem::forget(irq_guard); // The assembly tail restores physical IF.
            let (vcpu_loop_context, vcpu_loop_cpu_state) = prepare_current_vcpu_loop_return();
            // enter_internal saves the next loop return before restoring this IRQ frame.
            unsafe {
                enter_internal((&frame as *const TrapFrame).cast(), cpu_state,
                               vcpu_loop_context, vcpu_loop_cpu_state, resume_kernelet_vcpu as *const ());
            }
            Some(take_vcpu_return_reason()) // Propagate the next reason to the loop.
        }
        ResumeDecision::Selected { ctx, cpu_state } => {
            core::mem::forget(irq_guard);
            let (vcpu_loop_context, vcpu_loop_cpu_state) = prepare_current_vcpu_loop_return();
            unsafe {
                enter_internal(ctx.cast(), cpu_state, vcpu_loop_context, vcpu_loop_cpu_state,
                               resume_task_context as *const ());
            }
            Some(take_vcpu_return_reason())
        }
    }
}
```

The `prepare_*_root_and_cpu_slot` helpers perform the [service return checks](../kernelet-api-service.md#ordinary-service-return): check that stack/page-table mappings remain valid, invalidate the cached native page-table activation, install CpuSlot and the register-save pointer, and record the execution/accounting state for return.
These helpers still need implementation. They cannot run kernelet code or free storage needed by the restore assembly.
The final check runs with physical IRQs disabled. If stop arrives just after the check, a Host interrupt must remain pending until the vCPU blocks new entry or acknowledges stop. One Boolean check alone does not close that race.

Here is the complete **general-register restore**, using native `TrapFrame` order. `restore_cpu_state` is defined in [Interrupts and time](interrupts-and-time.md#machine-state-code):

```rust
// ostd::kernelet::host
#[unsafe(naked)]
unsafe extern "C" fn resume_kernelet_vcpu(
    frame: *const TrapFrame, cpu_state: *const CpuStateSnapshot,
) -> ! {
    core::arch::naked_asm!(
        "mov r12, rdi",
        "mov rdi, rsi",
        "sub rsp, 8",
        "call {install}",
        "mov rdi, r12",
        "jmp {restore}",
        install = sym restore_cpu_state,
        restore = sym restore_interrupt_frame,
    );
}

#[unsafe(naked)]
unsafe extern "C" fn restore_interrupt_frame(frame: *const TrapFrame) -> ! {
    core::arch::naked_asm!(
        "mov rsp, rdi",
        "pop rax", "pop rbx", "pop rcx", "pop rdx",
        "pop rsi", "pop rdi", "pop rbp",
        "pop r8", "pop r9", "pop r10", "pop r11",
        "pop r12", "pop r13", "pop r14", "pop r15",
        "add rsp, 16", // Skip trap_num and error_code.
        "iretq",       // Restore rip, cs, rflags, rsp and ss.
    );
}
```

This assembly is the interrupt-frame restore at the end of the [pause sequence](tasks.md#execution-flow), after any required virtual tick handling.
It restores the interrupted execution, not the older `TaskExecRecord.ctx`, and does not call `Task::run`, restart the closure or repeat internal pre/post.
The required tick-entry integration remains **[unverified]**; this `iretq` sequence alone does not implement it.
Keep the frame on `vcpu_thread_task.kstack`, selected page tables and additional register state alive and mapped until restore finishes. No Rust runs after the final machine-state installation.

### Stop finishes the vCPU execution loop and returns normally {#stack-return}

`ResumeDecision::Stop` and `VcpuReturnReason::Stop` call `finish_vcpu`, a placeholder for the stop implementation.
It may return `None` only after the vCPU's outstanding Host operations have completed or been cancelled; the loop then ends and `enter_vcpu` returns to the vCPU Thread's closure.

> **To be written.** Define `finish_vcpu`, failure handling and resource reclamation during the separate review of [Faults](../faults-and-reclamation.md). That chapter retains its earlier lifecycle design; it has not yet been reconciled with the vCPU Thread model.

### Checks required by these paths {#protocol-argument}

An internal switch must keep the same Host Task, so Host scheduling and charging continue to apply to its vCPU.
[Stack acquisition](tasks.md#transfer) must exclude a second vCPU until the first has left the stack and saved its context. The outgoing Rust Task stays alive through post.
A physical pause must keep that exclusive execution use and restore the interrupt snapshot, not an older internal-switch context.
Task exit must forbid further entry; instance stop must finish or cancel Host operations and return normally from the vCPU execution loop before native Thread teardown. Other readers and DMA may keep resources alive longer.
These are requirements on the ordering and lifetime checks above. The [remaining implementation work](tasks.md#obligations) must still establish them in the kernel.

### Which vCPU runs a task? {#task-vcpu}

`Task.schedule_info.cpu` records the internal scheduler's CPU assignment; dequeue clears it on sleep or exit.
Host `active_task` keeps the executing resource alive during a pause. The kernelet current pointer keeps its internal Task alive, but briefly names the next Task before the stack changes; Host state tracks the stack actually in use.
`CpuSlot.vcpu` tells [CPU-local access](tasks.md#cpu-local-address) which vCPU is calling. Bootstrap has no internal current Task. The current `ClassScheduler` selects the recorded `last_cpu` on wake, so the normal path keeps the Task on that vCPU.
Cross-vCPU migration would require an explicit scheduler handoff that makes the old execution unavailable before the new vCPU can claim it; this protocol does not introduce such migration or a wait for it.

## Idle uses the existing halt_cpu call {#objects}

Each vCPU has an internal idle Task running the existing `bsp_idle_loop` or `ap_idle_loop`. The scheduler selects it when other Tasks cannot run:

```rust
// kernel/core/src/init.rs::ap_idle_loop; native loop plus one kernelet-only call.
fn ap_idle_loop() {
    #[cfg(feature = "kernelet")]
    start_virtual_irq_worker();
    loop {
        ostd::task::halt_cpu();
    }
}
```

BSP idle starts the worker in the same way, before spawning `first_kthread`, and calls `halt_cpu` while waiting for the init process.
Both idle loops keep their native structure; `halt_cpu` selects physical CPU halt or virtual idle waiting:

```rust
// ostd/src/task/preempt/mod.rs: common API and sleep-eligibility check.
pub fn halt_cpu() {
    crate::task::atomic_mode::might_sleep();

    #[cfg(not(feature = "kernelet"))]
    {
        // Existing native halt path.
        let irq_guard = crate::irq::disable_local();
        if cpu_local::need_preempt() {
            drop(irq_guard);
        } else {
            core::mem::forget(irq_guard);
            crate::arch::irq::enable_local_and_halt();
        }
        super::scheduler::might_preempt();
    }
    #[cfg(feature = "kernelet")]
    {
        let observed = event_sequence(); // Read before checking work.
        crate::irq::poll_events_at_checkpoint();
        super::scheduler::yield_now(); // Returns when idle runs again.
        wait_for_work(observed); // Park Host only if the recheck still finds no work.
    }
}
```

The first invocation of each kernel idle closure starts that vCPU's interrupt worker, after per-vCPU initialization and the first post handler have finished:

```rust
// Kernelet kernel build: ThreadOptions compiles against vOSTD and creates an
// internal Thread, not another native Host thread. Call once in bsp_idle_loop/ap_idle_loop
// in the kernelet build. Native builds do not create this virtual IRQ worker.
#[cfg(feature = "kernelet")]
fn start_virtual_irq_worker() {
    ThreadOptions::new(|| ostd::irq::worker_main())
        .cpu_affinity(CpuId::current_racy().into())
        .spawn();
}
```

The worker is an ordinary Thread pinned to this vCPU for its lifetime. It may run as soon as it is spawned, using the default fair policy; the kernelet scheduler determines how soon it handles events.
Its [loop](interrupts-and-time.md#worker-delivery) handles pending work, then sleeps on an internal WaitQueue so another Task can run. If no other work remains, idle can [put the vCPU Thread to sleep](tasks.md#waiting).
A Task that never reaches an internal scheduling check can prevent other internal Tasks from running. Host can still pause its vCPU.

## Put an idle vCPU to sleep {#waiting}

An internal Task can sleep while its vCPU runs another Task. Only idle calls `vcpu_wait`, after checking that no Task or pending work needs to run.
[Synchronization](synchronization.md#vcpu-wait) explains how Host waiting and notification avoid missed wakeups.
The [execution-flow guide](tasks.md#execution-flow) distinguishes this Host service continuation from an internal switch or physical IRQ return.

```rust
// vOSTD. observed came from this vCPU's shared VcpuEventSlot.event_seq.
#[cfg(feature = "kernelet")]
fn wait_for_work(observed: u64) {
    // Recheck immediate work and local guards after the idle adapter's drain.
    if has_runnable_or_immediate_work() || has_internal_obligations() {
        return; // The caller rescans; do not park with unfinished local work.
    }
    enter_idle_quiescence();
    let status = (service_table().vcpu_wait)(observed); // SERVICE: sleep vCPU Thread.
    leave_idle_quiescence();
    assert_eq!(status, 0, "invalid idle wait");
    // A successful return means rescan; it does not guarantee runnable work.
}
```

Read `VcpuEventSlot.event_seq` before checking the queues. Host increments it after recording new work, so a change prevents the waiter from sleeping through that work.
Idle must have no active RCU reader before reporting itself idle, and must leave that [RCU idle state](synchronization.md#rcu) before processing a wake. An RCU callback waiting for a future grace period does not by itself prevent sleep.
Host checks the calling vCPU and whether the service is allowed at this point; vOSTD ensures that only idle calls it.
Device and timer results must remain available even if their notifications merge or the notification queue fills.

## CPU accounting: from the vCPU Thread to the internal Thread {#accounting}

The internal scheduler retains the native scheduler's runtime-update semantics: at a scheduling update, read the clock and account for the increase since the previous update.
Host scheduling measures a vCPU Thread; internal scheduling measures the Task that vCPU Thread executes.
The internal clock must exclude time when Host has descheduled the vCPU Thread.
POSIX Thread/Process user and system clocks separately retain their tick-based updates through the [virtual tick handler](interrupts-and-time.md#tick).

### Start with the native scheduler's updates {#tick-accounting}

The [timer callback shown in the preemption sequence](tasks.md#execution-flow) calls `local_rq.update_current(UpdateFlags::Tick)`.

`PerCpuClassRqSet::update_current` first calls `CurrentRuntime::update`, which reads the clock and computes the increase since its previous reading.
It then passes that increase to the scheduling class; for Fair tasks, `FairClassRq::update_current` updates virtual runtime using the Task's weight and checks whether another Task should be selected.
It records any resulting preemption request for `might_preempt`, which selects a Task without calling `update_current` again.

A Task can also call `yield_now` between ticks.
That existing path calls `local_rq.update_current(UpdateFlags::Yield)` before deciding whether to select another Task, reaching the same runtime calculation.
For example, readings of 10 ms at selection, 14 ms at a tick and 16 ms at yield produce increments of 4 ms and 2 ms: 6 ms in total.
If the scheduler then selects another Task, it creates that Task's `CurrentRuntime` with a fresh baseline; a later reading of 18 ms adds 2 ms to the new Task.
This is how yield accounts for execution since the last tick. The low-level context switch only transfers execution; it does not perform this calculation.

The same timer callback list also contains POSIX `update_cpu_time`, registered in `process/process/timer_manager.rs`.
That separate callback adds one jiffy to the interrupted POSIX Thread/Process user or system clock, when applicable.
It is not called by yield. Scheduler runtime uses elapsed clock readings; POSIX CPU clocks retain the native periodic sampling behavior.

In the kernelet build, keep the scheduler callback and yield call sites above.
Host's physical-tick callback updates the vCPU Thread in the Host run queue.
The proposed virtual-tick entry invokes the image's own callback list with the interrupted internal Task current, updating that Task in the internal run queue and its POSIX clocks when applicable.
The Host and vOSTD callbacks use their own schedulers and preemption flags; virtual-tick entry and guarded delivery still require the [tick protocol](interrupts-and-time.md#tick).

### Adapt the runtime clock used by the internal scheduler

Native `CurrentRuntime::new/update` reads `sched_clock()`. Reusing that clock in the kernelet would include time while the Host runs another Thread. Keep the runtime fields and scheduling-class consumers, but read the selected internal Task's execution total:

```rust
// Existing kernel/core/src/sched/sched_class/mod.rs runtime backend.
#[cfg(not(feature = "kernelet"))]
impl CurrentRuntime {
    fn new() -> Self {
        Self { start: sched_clock(), delta: 0, period_delta: 0 }
    }
    fn update(&mut self) {
        let now = sched_clock();
        self.delta = now - core::mem::replace(&mut self.start, now);
        self.period_delta += self.delta;
    }
}

// Kernelet build of kernel/core/src/sched/sched_class/mod.rs.
// start, delta and period_delta retain their native meanings and use nanoseconds.
#[cfg(feature = "kernelet")]
impl CurrentRuntime {
    fn new(task: &Task) -> Self {
        Self { start: task.execution_time_ns(), delta: 0, period_delta: 0 }
    }

    fn update(&mut self, task: &Task) {
        let now = task.execution_time_ns();
        self.delta = now.checked_sub(self.start).expect("execution counter regressed");
        self.start = now;
        self.period_delta = self.period_delta.checked_add(self.delta)
            .expect("execution counter exhausted");
    }
}
```

The proposed `Task::execution_time_ns` reads Host-written `TaskExecTimeSlot`. A closed timer returns `total_ns`; an OPEN timer adds `host_monotonic_ns - open_since_ns` for execution since the last Host update.
Read the clock between two reads of the slot's version. Retry if the version or clock epoch changes, or if the subtraction is invalid. Host closes the timer before descheduling or changing the running Task and writes a new epoch when reopening it. Keep the Task alive throughout the read.

The two call sites in `PerCpuClassRqSet` select matching signatures. The argument is the run queue's selected Task; `Task::current()` still describes the previous processor state during selection:

```rust
// Inside try_pick_next's existing closure, before moving next into self.current.
#[cfg(not(feature = "kernelet"))]
let runtime = CurrentRuntime::new();
#[cfg(feature = "kernelet")]
let runtime = CurrentRuntime::new(&next.0);
if let Some((old, _)) = self.current.replace((next, runtime)) {
    self.enqueue_entity(old, None);
}
```

```rust
// Inside update_current, bind ((task, cur), rt) from self.current.
#[cfg(not(feature = "kernelet"))]
rt.update();
#[cfg(feature = "kernelet")]
rt.update(task);
// Continue the existing class-specific update using cur and rt.
```
Two scheduling cases explain why the clock source and baseline both matter:

1. **Host switches out the vCPU Thread; the internal Task stays selected.**
   Host creates a new native `CurrentRuntime` when it selects the vCPU Thread again.
   vOSTD has made no internal selection, so it retains the internal Task's previous baseline.
   That Task's execution clock must stay unchanged during the Host pause; its next update then counts only actual execution.

2. **vOSTD switches internal Tasks; Host keeps running the same vCPU Thread.**
   For example, the current internal Task calls `yield_now`: the shared yield path first updates its runtime, then the internal scheduler may select the next internal Task.
   Selection creates the next internal Task's `CurrentRuntime` using its own execution total, as in `CurrentRuntime::new(&next.0)` above, even if that Task ran before.
   The subsequent `task_switch` service closes the outgoing execution interval before changing Host's `active_task`; it opens the incoming interval when entering the selected Task.
   Host still schedules the same vCPU Thread throughout this example.
   If the incoming internal Task has already accumulated 40 ms when selected, and later reads 43 ms at a virtual tick, its runtime update adds 3 ms.
   Each baseline belongs to the selected internal Task's own counter; the outgoing and incoming Tasks' totals are never subtracted from each other.

An internal switch does not synthesize a POSIX CPU tick; those clocks are updated by eligible virtual-tick delivery on the interrupted Task.
Worker execution follows the same rules and increases the worker's own execution total.
The Host interval collector and slot writer must implement this read contract in a common monotonic nanosecond domain. Their implementation and integration tests remain **[unverified]**.

### Host publishes the internal Task execution clock

The collector below is proposed Host OSTD support for `Task::execution_time_ns` above.
It records internal execution intervals on entry/return and closes them before Host descheduling, internal Task changes and exit; it also publishes time before virtual tick delivery.
This supplies the time value consumed by the existing scheduler update paths; it does not add another scheduling-class update at the machine switch.
Host charges the vCPU Thread's execution to the instance; reading an internal Task total does not charge it again.

Host needs a start timestamp for the current internal execution interval.
`Vcpu.internal_execution_started_at_ns` is `Some(timestamp)` while that interval is running and `None` while it is stopped.
`internal_execution_total_ns` sums the closed intervals on this vCPU; Host computes it, and it never resets at a Task switch.
`attributed_execution` records how much of that total has already been assigned to Task records.
The difference goes to `active_task.execution_total`; that Task's total is then published in its read-only `TaskExecTimeSlot`.

The following proposed Host OSTD methods supply the counter consumed by the existing `attribute_execution` below.
`now_ns` is a trusted timestamp in the Host execution-clock domain, captured at the relevant entry/return boundary.
The caller holds the same vCPU state protection as `active_task` changes; errors enter trusted stop/recovery rather than allowing execution with inconsistent counters.

```rust
// Proposed Host OSTD additions. No run-queue selection or stack switch here.
impl Vcpu {
    fn begin_internal_execution(&mut self, now_ns: u64) -> Result<(), &'static str> {
        if self.active_task.is_none() {
            return Err("no internal Task selected");
        }
        if self.internal_execution_started_at_ns.is_some() {
            return Err("internal execution interval already open");
        }
        self.internal_execution_started_at_ns = Some(now_ns);
        Ok(())
    }

    fn end_internal_execution(&mut self, now_ns: u64) -> Result<u64, &'static str> {
        let Some(start) = self.internal_execution_started_at_ns else {
            return Ok(self.internal_execution_total_ns); // Already closed: add nothing.
        };
        let elapsed = now_ns.checked_sub(start).ok_or("execution clock regressed")?;
        let total = self.internal_execution_total_ns.checked_add(elapsed)
            .ok_or("vCPU execution counter exhausted")?;
        self.attribute_execution(total)?; // Attribute before active_task changes.
        self.internal_execution_total_ns = total;
        self.internal_execution_started_at_ns = None;
        Ok(total)
    }
}
```

On internal entry, call `begin_internal_execution` and publish an OPEN slot containing the active Task's closed total and this interval's start timestamp.
An internal read can then include `now_ns - open_since_ns` without waiting for a tick.
At IRQ/service entry, call `end_internal_execution` with the captured boundary timestamp and publish the updated CLOSED slot before any Host descheduling or active-Task change.
State mutation and slot publication must share the entry path's physical-IRQ exclusion, so the vCPU cannot be descheduled between closing its private interval and closing the shared slot.
The [versioned-slot contract](../kernelet-api-service.md#cpu-time-publication) defines publication and reads; these arithmetic methods alone do not implement it.

`attribute_execution` gives the increase since its last call to `active_task`:

```rust
// ostd::kernelet::host
// Host OSTD addition. Serialize with active_task changes and execution ownership.
impl Vcpu {
    fn attribute_execution(&mut self, host_execution_total: u64) -> Result<(), &'static str> {
        let delta = host_execution_total.checked_sub(self.attributed_execution)
            .ok_or("Host execution counter regressed")?;
        if let Some(task) = &self.active_task {
            let total = task.execution_total.load(Ordering::Relaxed)
                .checked_add(delta).ok_or("Task execution counter exhausted")?;
            task.execution_total.store(total, Ordering::Release);
        } else if delta != 0 {
            return Err("Task execution without an active Task");
        }
        self.attributed_execution = host_execution_total;
        Ok(())
    }
}
```

The architecture entry gate must capture the boundary timestamp while preserving the interrupted registers; safe Host Rust later uses that timestamp to close the interval.
Passing the later time at which the vCPU loop happens to run would include IRQ/service handling in the internal Task's time.
`settle_vcpu_execution` consumes that closed total before changing `active_task`; its repeated `attribute_execution` call adds zero because the cursor already equals the total.
On a Host pause, leave the interval closed until internal execution resumes, then open it with a new timestamp.
On an internal switch, close the outgoing interval, complete resource selection and transfer preparation, then open an interval for the incoming Task.
Bootstrap has no active internal Task and does not call `begin_internal_execution`; its cost belongs to the framework account.
Framework and physical-IRQ costs follow [I6](../principles.md), including its exception for user-mode IRQs.

For example, ignoring framework overhead, the following calls account for an internal switch and a later Host pause.
The identifiers describe the two sides of that one internal switch; the resource validation and transfer operations are omitted here.

```rust
// Host OSTD arithmetic example. Task totals start at zero; timestamps are ns.
vcpu.active_task = Some(outgoing_task.clone());
vcpu.begin_internal_execution(0)?;
vcpu.end_internal_execution(4_000_000)?; // Outgoing Task: 4 ms.

// Internal task_switch: Host still runs the same vCPU Thread.
vcpu.active_task = Some(incoming_task.clone());
vcpu.begin_internal_execution(4_000_000)?;
vcpu.end_internal_execution(7_000_000)?; // Incoming Task: 3 ms.

// Host switches out the vCPU Thread from 7 ms to 27 ms.
// No interval is open, and active_task remains the incoming Task.
vcpu.begin_internal_execution(27_000_000)?;
vcpu.end_internal_execution(29_000_000)?; // Incoming Task: 3 + 2 = 5 ms.
```

**[unverified]** The interval arithmetic and attribution are specified here; architecture timestamp capture, Host clock conversion, slot publication and every entry/return hook still require integration.
The existing native scheduler does not export this internal execution counter.

### Finishing accounting after Task exit

A Task must finish any required virtual tick processing before its internal exit proceeds, subject to the entry and guard rules still to be specified in [Interrupts and time](interrupts-and-time.md#tick).
Host closes its execution interval before retiring the resource.
Execution ownership, outstanding Host users and stack reclamation still require their separate lifetime checks.


## CPU-local state {#cpu-local-rcu}

CPU-local data belongs to a vCPU, not an internal Task. Two Tasks running in turn on vCPU 0 see the same copy; a Task running on vCPU 1 sees vCPU 1's copy.
The kernel keeps its existing calls. vOSTD changes how those calls locate the copy and how their guards exclude internal switching.

### Keep the kernel call unchanged

This native API is also the kernelet API:

```rust
// Kernel code, unchanged in both builds.
cpu_local! {
    static LOCAL_COUNT: core::cell::Cell<usize> = core::cell::Cell::new(0);
}

fn record_local_event() {
    let irq_guard = ostd::irq::disable_local();
    let count = LOCAL_COUNT.get_with(&irq_guard);
    count.set(count.get() + 1);
} // Release the borrow, then the guard.
```

`get_with` in `ostd/src/cpu/local/mod.rs` ties the borrowed access to a `DisabledLocalIrqGuard`.
In vOSTD, that [virtual IRQ guard](synchronization.md#virtual-guards) prevents this Task from switching vCPUs and excludes this vCPU's virtual interrupt worker until the borrow ends.
Host may pause the vCPU during `count.set`: it resumes the same vCPU identity, and neither Host handlers nor another vCPU accesses this non-Sync copy.

### Replace the native address calculation {#cpu-local-address}

Host replicates the image's `.cpu_local` template once per vCPU before startup, following the [image layout](../builds-and-images.md#window).
Keep `StaticStorage::as_ptr` and its offset calculation in `ostd/src/cpu/local/static_cpu_local.rs`. Select only the base-address calculation:

```rust
// Inside StaticStorage::as_ptr; the offset and returned pointer are common.
let offset = self.get_offset();
#[cfg(not(feature = "kernelet"))]
let local_base = arch::cpu::local::get_base() as usize; // Existing Host GS base.
#[cfg(feature = "kernelet")]
let local_base = {
    // boot/replica_base are from the validated image layout.
    // SAFETY: Host installed its GS base and this vCPU's CpuSlot mapping.
    let vcpu = unsafe { read_current_vcpu(boot.cpu_slot_gs_offset as usize) };
    replica_base + vcpu * (boot.cpu_local_replica_bytes as usize)
};
let local_va = local_base + offset;
```

GS still locates Host CPU-local storage. vOSTD reads this vCPU's number from `CpuSlot`, then selects its copy of the image's CPU-local data.
The accessor returns `local_va as *const T` without a service call. Host restores the slot before resuming the vCPU; the [reader](../kernelet-api-service.md#cpu-slot-access) shows the GS-relative load.
Initialization must validate each copy's bounds, size, alignment and field offsets. `get_ptr_on_target` also needs the new calculation: vCPU 0 uses copy 0, not the template in the image.
`get_on_cpu(target_vcpu)` requires `T: Sync` and always returns the explicitly requested vCPU's copy, even if the caller later moves.

### Keep scalar CpuLocalCell operations guard-free {#cpu-local-cell}

The scalar `CpuLocalCell` API, such as `EVENT_COUNT.add_assign(1)`, returns values without lending references.
Native x86 implements `add_assign` with one `add gs:[offset], value` instruction (`ostd/src/arch/x86/cpu/local.rs`). An interrupt cannot split that instruction. These operations are local, not cross-CPU atomic operations; remote access to the same cell is forbidden.
vOSTD computes the replica address first, then performs the update in one memory instruction.
The address calculation contains no internal scheduling call; a physical pause resumes with the same vCPU identity.
A virtual tick between address calculation and the update therefore cannot split the read/modify/write itself:

```rust
// vOSTD backend for CpuLocalCell<u64>::add_assign; arguments describe a
// validated static cell. All layout lookup/validation happens before entry.
// SAFETY: Only this vCPU accesses this cell; no reference to it is outstanding.
#[cfg(feature = "kernelet")]
unsafe fn cell_add_u64(
    replica_base: usize,
    replica_bytes: usize,
    cpu_slot_gs_offset: usize,
    offset: usize,
    value: u64,
) {
    unsafe {
        let vcpu = read_current_vcpu(cpu_slot_gs_offset);
        let address = replica_base
            .wrapping_add(vcpu.wrapping_mul(replica_bytes))
            .wrapping_add(offset);
        core::arch::asm!(
            "add qword ptr [{cell}], {value}",
            cell = in(reg) address,
            value = in(reg) value,
            options(nostack), // Reads/writes memory and changes flags.
        );
    }
}
```

A Rust `read_volatile` followed by `write_volatile` would permit a tick callback to update the cell between those accesses and lose that update.
The single `add` preserves the native same-CPU interruption semantics.
This accessor must not call a service, allocate, invoke a callback, panic/unwind or check internal scheduling.
Wrapping address arithmetic relies on initialization having validated the complete replica layout.
Do not add a public guard here: guards themselves use `CpuLocalCell`. Borrowed pointers and compound operations still require guards.
**[unverified]** Every supported scalar operation and width needs a corresponding backend and generated-instruction check; this example covers only `u64` addition.
Allocator caches, softirq masks and statistics keep their APIs. [Synchronization](synchronization.md#rcu) covers RCU reports on switch and idle.

## What still needs implementation {#obligations}

- Implement and test the [Host preemption checkpoint contract](tasks.md#host-preemption-checkpoint): run an internal kernel loop with no Host service calls while another native thread is runnable; demonstrate timer-driven Host switching and correct internal resumption. Repeat with virtual guards held, with an interrupted pre/post handler, with no selected replacement, and with a native service guard that defers scheduling. Verify request retention, the explicit deferred checkpoint, nested IRQ snapshot ownership, and unchanged internal Task selection. Measure interrupt-to-checkpoint latency separately from time waiting to be selected again.

Stopping prevents new kernelet execution, interrupts startup and wakes Host waits. Live Host operations must finish or cancel before their saved execution state can be discarded and `enter_vcpu` can return.
The [instance reaper](../faults-and-reclamation.md) waits for all stack, page-table, device and DMA users. Resources still in use remain charged after the vCPUs stop.

The examples specify the intended behavior. These parts still need implementation:

- Register saving and restoration: connect transfer/IRQ entry to the vCPU execution loop, implement ordinary service entry/return including wait/yield, and validate interrupted, selected, first-entry and stop continuations. Test Host descheduling both on vCPU Threads' original stacks and inside services without overwriting a pending interrupt frame.
- Internal switching: implement the services and first-entry function, and connect worker startup and event checks.
- Accounting: connect Host timers, scheduling and cleanup rules, shared counters and final updates.
- Synchronization: discuss the wait/notify and RCU reader/idle contracts before implementing their adaptations.

**[unverified]** These service and machine paths are not implemented or boot-tested. Native excerpts refer to Asterinas commit `430ab496fe74fd7d081ae926be46886ab024c4c7`.
Validation must cover first entry, bidirectional switches, exit, wake races, pauses inside pre/post, nested interrupts, user return and concurrent kill. Rust compilation alone cannot verify register restoration or stack lifetimes.

## What a tenant sees {#tenant-view}

- `nproc` and `/proc/cpuinfo` describe the virtual CPU set. `nice`, affinity and supported `sched_setscheduler` policies control the active internal scheduler. Internal real-time policy cannot override Host allocation, and more runnable internal threads do not add Host scheduling weight.
- `/proc/loadavg`, `/proc/stat` run queues, `sysinfo(2)` load and `sched_getscheduler` describe internal state. CPU-time statistics require the [accounting changes](tasks.md#accounting) that apply each sample to the Task that ran.
- Host can pause a spinning kernelet despite virtual guards. Internal preemption and virtual event delivery still require checkpoints. Host scheduling determines when the vCPU Thread runs again. The machine pause path and maximum preemption delay remain **[unverified]**.
- Process, thread, wait and signal APIs stay the same. Idle blocks its vCPU Thread until notified. Wall time advances during Host pause; Task CPU time does not. Event and scheduling latency depend on both Host scheduling and internal checkpoints.

## Costs {#costs}

- **Per internal Task:** one internal Task/closure/data allocation plus a Host-managed internal Task stack, execution record, registry and accounting/lifetime state. The initial stack is 512 KiB, **measured on the source tree**, charged to the instance; ordinary task creation is bounded by `max_tasks`. `build` crosses through `task_create`; `run` enqueues internally and requests rescheduling through the native call chain. Record size and guard/mapping overhead need measurement.
- **Per internal switch, park and wake:** switch services save/restore context and update accounting, with local pre/post handlers. Parking normally switches, unless a wake is already recorded. Waking may enqueue; a remote rescheduling request makes one targeted notification call, while a local request makes none. There is no fixed one-crossing park/wake cost.
- **Per CPU-local access or spin-lock operation:** vOSTD locates the current vCPU's copy and uses virtual guards where the native implementation uses physical CPU state. Added instructions and cache traffic require measurement of the actual accessors; scalar cell operations follow the checkpoint-free sequence above.
- **Per Host pause/resume and physical interrupt:** save/restore the interrupted registers and instruction pointer, including FS/user-GS/FPU state and the active page table. Snapshot, root/TLB and ordinary-Host fast-path costs all require measurement.
- **Per vCPU:** one vCPU Thread with its native Task and stack, one internal idle Task, one ordinary internal interrupt worker, CPU-local state, and startup/interrupt storage. Idle and worker are already counted among internal Task allocations; startup/interrupt storage is additional; the vCPU execution loop reuses the native Host Task stack. Pending reclamation remains charged until freed.
- **Per internal checkpoint:** check for rescheduling and pending events, and whether virtual guards permit handling them. Host schedules the vCPU Thread independently. Writing and reading accounting counters and freeing resources add their own work.

## What this page decides {#decisions}

This proposal revises the following decisions from the earlier [design register](../../../notes/design-register.md):

- **D15/D31:** the kernelet scheduler selects internal Tasks and interprets their nice, affinity and scheduling class; Host never invokes this policy under its run-queue locks.
- **D61/D62:** endovisor creates one vCPU Thread per vCPU; internal Task creation adds execution resources, not Host scheduling weight. Host scheduling controls each vCPU Thread's CPU time.
- **D16/D17:** Host may pause kernelet execution despite virtual guards and must preserve the actual interrupted state, including partial pre/post operations. Host operations must complete or cancel before stop discards their frames.
- **D32:** RCU adaptation remains pending the [Synchronization discussion](synchronization.md#rcu).
- **D67:** each vCPU has an internal idle Task to run after other Tasks sleep/exit, process pending work and park its vCPU Thread.
