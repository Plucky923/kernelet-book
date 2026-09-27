# The kernelet API: service half

*Answers question 3: what API does OSTD expose to vOSTD, and how does a call cross from the kernelet image into the host?*

The service half is what a kernelet gets from the host at run time. Its wire form, the **image ABI**, is two tables of `extern "C"` function pointers and a few shared pages; its implementation is the module `ostd::kernelet::service` in the host build. Every virtualized item on the [taxonomy](virtualizing-ostd/index.md) bottoms out in one of the functions on this page, and every function here is specified with its signature, what it checks, and what it costs. Invariants enforced here: I1 (reach), the service side of I2 (ownership), I4 (no retained reference), I5 (no closure crosses), I6 (charged work) and the counter behind I7 (termination).

The tables and the shared-page layouts are defined once, in a module compiled into both builds, `ostd::kernelet::abi`, with no `cfg` inside it, so the two sides cannot disagree on a field. Every error code and constant below is a named value in that module.

## How the host knows who is calling

A service function takes no kernelet or task argument. The host learns both from its own per-CPU area, which the scheduler writes on every switch to or from a kernelet task (a 16-byte store per switch):

```rust
// ostd::kernelet::abi — read by both builds; written only by the host scheduler.
#[repr(C)]
pub struct CpuSlot {
    /// The kernelet whose task is running on this CPU, or `NONE`.
    pub kernelet: u16,        // slot index
    pub vcpu: u16,            // this CPU's index in the kernelet's CPU set
    pub task: u32,            // the running task's name, `TaskName`
    pub generation: u32,      // the kernelet's generation, so a stale slot is detected
    pub _reserved: u32,
}
```

The slot lives in the host's per-CPU storage at an offset published in `BootArgs::cpu_slot_gs_offset`; vOSTD reads it with one GS-relative load through a small inline-assembly helper, `mov rax, gs:[reg]` with the offset in a register, since the tree's `cpu_local_cell!` macro emits an immediate offset it cannot know here (checked on the tree: `ostd/src/arch/x86/cpu/local.rs`). The kernelet never writes the slot; it is host memory. Reading `task` needs no preemption guard: a task that migrates reads its own name on the new CPU. Reading `vcpu` requires preemption disabled, as every per-CPU read does, and the host's kernel-mode preemption honors the kernelet's preemption count ([Tasks](virtualizing-ostd/tasks.md)).

A **task name** is a `u32` of two halves, `index:16 | generation:16`. The index selects the task's record; the generation distinguishes a reuse of the index, so that a late `task_unpark` with a stale name fails with `-INVALID` instead of waking a stranger. A kernelet may have at most `config.max_tasks` live tasks, and never more than 65,536; retired indices are reused with the next generation. Names, not pointers, are what cross (invariant I4).

## Host-private per-task state

The state on which termination depends is host memory a kernelet cannot address. Each host task that belongs to a kernelet carries:

```rust
// Inside the host's `Task`, only for kernelet tasks. Host memory.
pub(crate) struct KerneletTaskState {
    pub kernelet: KerneletId,
    pub name: TaskName,
    /// 0 while in kernelet code, 1 while inside a service call. Written by the service
    /// prologue and epilogue with `Release`; read by the kill and tick paths with `Acquire`.
    /// This is invariant I7's counter, and it is host-private so that no kernelet can
    /// hold it at 1.
    pub service_depth: AtomicU32,
    /// Host-owned flags: NEED_RESCHED, DYING, CANCEL_PARK, PARK_TOKEN.
    pub flags: AtomicU32,
    /// The user page-table root the task last activated; the scheduler restores it.
    pub root: AtomicU64,
    /// User FS and GS bases and an XSAVE area, saved on an involuntary switch away from
    /// kernelet code and restored on the way back ([Tasks](virtualizing-ostd/tasks.md)).
    pub tls_fpu: SavedUserState,
    /// The trampoline's stack pointer, to which termination resets the stack.
    pub entry_sp: u64,
}
```

## The shared pages

The regions of `KW_SHARED` are host frames mapped into the kernelet's window ([Builds and images](builds-and-images.md#window)); the host writes them through its linear-map alias, never through a window address (invariant I4).

**The boot arguments**, read-only, at `KW_SHARED + 0`, written once at creation:

```rust
#[repr(C)]
pub struct BootArgs {
    pub size: u32,
    /// The host build's source hash; `_kernelet_entry` stops if it is not the image's own.
    pub source_hash: [u8; 32],
    pub kernelet: u16, pub generation: u32,
    pub num_vcpus: u16,
    /// Host CPU of each virtual CPU, in order. MAX_VCPUS = 64, so that a virtual-CPU set is one `u64`.
    pub vcpu_host_cpu: [u16; MAX_VCPUS],
    pub cpu_slot_gs_offset: u32,
    pub cpu_local_replica_bytes: u32,
    /// Addressing: the base of the host's own linear map, which `paddr_to_vaddr` adds to
    /// a physical address, and the base of this instance's metadata window. Both are
    /// chosen by the host and differ between instances ([Memory](virtualizing-ostd/memory.md)).
    pub linear_map_base: u64, pub meta_base: u64,
    /// The 256 kernel-half top-level entries that every user page table the kernelet
    /// creates must copy.
    pub kernel_half_entries: [u64; 256],
    /// Memory: the most grains the kernelet may take, and the offset, from `KW_SHARED`, of the
    /// host-written grant table (an array of `RunDesc`, its length in the info page) ([Memory](virtualizing-ostd/memory.md)).
    pub max_grains: u32, pub grant_table: u32,
    /// Devices: `num_devices` entries of `DeviceEntry` follow the struct.
    pub num_devices: u16,
    /// Offsets, from `KW_SHARED`, of the clock page, the info page, the task-record array
    /// and the per-virtual-CPU record array.
    pub clock_page: u32, pub info_page: u32, pub task_records: u32, pub max_tasks: u32, pub vcpu_records: u32,
    pub tsc_freq_hz: u64,
    /// The command line follows the devices: `cmdline_len` bytes.
    pub cmdline_len: u32,
}
#[repr(C)] pub struct RunDesc { pub paddr: u64, pub grains: u32, pub _pad: u32 }
#[repr(C)] pub struct DeviceEntry { pub id: u16, pub kind: u16, pub irq: u8, pub _pad: u8, pub vcpu: u16, pub reg_bytes: u32, pub device_type: u32, pub mmio_base: u64 }
```

**The clock page**, read-only, one page, shared by every kernelet on the machine: the host tick writes it once, not once per kernelet.

```rust
#[repr(C)]
pub struct ClockPage {
    /// The host's tick counter; `Jiffies::elapsed()` in the kernelet reads this.
    pub jiffies: AtomicU64,
    /// Monotonic nanoseconds since host boot.
    pub monotonic_ns: AtomicU64,
}
```

**The info page**, read-only to the kernelet, one page per kernelet:

```rust
#[repr(C)]
pub struct InfoPage {
    /// Set once by `kill` or `stop`; every service call after it terminates its
    /// caller at the prologue, except the three that end tasks.
    pub dying: AtomicU32,
    /// The published length of the grant table: runs the kernelet may read and add to its allocator.
    pub runs: AtomicU32,
    /// The end of the highest granted run; `max_paddr()` in vOSTD.
    pub max_paddr: AtomicU64,
    /// Set while the kernelet's CPU quota for the current period is exhausted; every task
    /// parks at its next quiescent point until it clears ([Tasks](virtualizing-ostd/tasks.md)).
    pub throttled: AtomicU32,
}
```

**The task records**, read-write, `max_tasks` records of 64 bytes at `KW_SHARED + task_records`, indexed by the task name's index half. The task records and the per-virtual-CPU records below are the only shared pages a kernelet writes, and nothing on them can harm another kernelet or block a kill: a corrupted `preempt_count` delays only that kernelet's own preemption until the tick budget kills it; a corrupted mirror only blinds the kernelet to its own state.

```rust
#[repr(C, align(64))]
pub struct TaskRecord {
    /// Written by the kernelet: its preemption-disable count for this task. Read by the
    /// host's kernel-mode preemption point to decide whether the task may be switched out.
    pub preempt_count: AtomicU32,
    /// A read-only mirror, for the kernelet, of the host-private flags NEED_RESCHED,
    /// DYING and CANCEL_PARK; the host writes it whenever it writes the private copy.
    pub flags_mirror: AtomicU32,
    /// Written by the host's page-fault handler before it jumps to an exception-table
    /// recovery address: the faulting address and error code, for the kernelet's retry
    /// loop ([User mode](virtualizing-ostd/user-mode.md)).
    pub fault_addr: AtomicU64, pub fault_code: AtomicU32,
    pub _reserved: [u32; 11],
}
```

**The per-virtual-CPU records**, read-write, `num_vcpus` records of 64 bytes at `KW_SHARED + vcpu_records`. The host tick writes, the kernelet consumes:

```rust
#[repr(C, align(64))]
pub struct VcpuRecord {
    /// Ticks the host has taken on this virtual CPU's host CPU while a task of the kernelet
    /// was running, not yet consumed by the kernelet: a count in the low 31 bits, and bit 31
    /// set if the most recent one found the task in user mode. The host adds; the kernelet's next
    /// task on this virtual CPU swaps it to zero at a tick point and runs the tick
    /// callbacks that many times ([Interrupts and time](virtualizing-ostd/interrupts-and-time.md)).
    pub tick_pending: AtomicU32,
    /// Set by the host when it parks the virtual CPU's worker with no runnable task of the
    /// kernelet on that CPU, cleared by the kernelet at its next switch point there: the RCU
    /// extended quiescent state ([Tasks](virtualizing-ostd/tasks.md)).
    pub quiescent: AtomicU32,
    pub _reserved: [u32; 14],
}
```

## The entry table

Read by the host from `KW_TEXT + 4 KiB` of the kernelet image at registration ([Builds and images](builds-and-images.md#entry)). The host executes kernelet code in exactly two ways: the boot task's trampoline calls `_kernelet_entry`, the ELF entry point, once; every other task's trampoline calls `run_task`. Both at service-call depth zero, on a fresh task.

```rust
#[repr(C)]
pub struct EntryTable {
    pub size: u32,
    /// The body of a spawned task. Called by the host's task trampoline on a fresh task,
    /// on the kernelet's kernel page table, with `entry` and `arg` from the spawn.
    /// `entry == 1` is reserved: it is the worker body, and `arg` is its virtual CPU;
    /// the host spawns one at creation per virtual CPU. Never returns: it ends with `task_exit`.
    pub run_task: extern "C" fn(entry: u32, arg: u64) -> !,
    /// Bounds of the image's exception table, for kernel-mode faults in user copies
    /// ([User mode](virtualizing-ostd/user-mode.md)). Both inside `KW_TEXT`.
    pub ex_table_start: u64, pub ex_table_end: u64,
    /// Bounds of the image's `.cpu_local` section, replicated per virtual CPU. Both inside `KW_DATA`.
    pub cpu_local_start: u64, pub cpu_local_end: u64,
    /// Hash of the OSTD source tree and toolchain; must equal the host build's.
    pub source_hash: [u8; 32],
}
```

Every entry index above 1 names a closure the kernelet stored in its own heap before calling `task_spawn` ([Tasks](virtualizing-ostd/tasks.md)); the host never sees the closure, only the index (invariant I5).

## The service table

```rust
#[repr(C)]
pub struct ServiceTable {
    pub size: u32,

    // Memory
    pub grains_request: extern "C" fn(count: u32, contiguous: u32) -> i32,
    pub pt_root_register: extern "C" fn(root_paddr: u64) -> i32,
    pub pt_root_unregister: extern "C" fn(root_paddr: u64) -> i32,
    pub pt_activate: extern "C" fn(root_paddr: u64) -> i32,
    pub tlb_shootdown: extern "C" fn(root_paddr: u64, start: u64, len: u64) -> i32,

    // Tasks
    pub task_spawn: extern "C" fn(entry: u32, arg: u64, vcpu_mask: u64, nice: i32, flags: u32) -> i64,
    pub task_exit: extern "C" fn() -> !,
    pub task_destroy: extern "C" fn(name: u32) -> i32,
    pub task_yield: extern "C" fn(),
    pub task_park: extern "C" fn() -> i32,
    pub task_unpark: extern "C" fn(name: u32) -> i32,
    pub task_set_nice: extern "C" fn(name: u32, nice: i32) -> i32,
    pub task_set_vcpus: extern "C" fn(name: u32, vcpu_mask: u64) -> i32,

    // Jobs and time
    pub job_wait: extern "C" fn() -> i64,
    pub timer_arm: extern "C" fn(vcpu: u32, deadline_ns: u64) -> i32,

    // User mode
    pub user_run: extern "C" fn(ctx: *mut RawUserContext) -> i32,

    // Devices
    pub mmio_read: extern "C" fn(dev: u16, offset: u32, width: u8) -> MmioResult,
    pub mmio_write: extern "C" fn(dev: u16, offset: u32, width: u8, value: u64) -> i32,

    // Output, faults, and end of life
    pub log_write: extern "C" fn(level: u8, module: *const u8, module_len: u32, text: *const u8, text_len: u32) -> i32,
    pub oops: extern "C" fn(msg: *const u8, len: u32) -> i32,
    pub stop: extern "C" fn(kind: u32, code: u32, msg: *const u8, len: u32) -> !,
}

/// Returned in two registers under the C ABI; no pointer crosses.
#[repr(C)] pub struct MmioResult { pub status: i64, pub value: u64 }

// `job_wait` returns a job packed in an `i64`: the kind in bits 0..8 and the argument above.
// JOB_VIRQ: the line. JOB_GRANT: no argument. JOB_TICK: an idle tick, with the count of ticks
// coalesced; a busy virtual CPU's ticks never come here but through `VcpuRecord::tick_pending`
// ([Interrupts and time](virtualizing-ostd/interrupts-and-time.md)).
pub const JOB_VIRQ: i64 = 1; pub const JOB_TICK: i64 = 2; pub const JOB_GRANT: i64 = 3;

// Error codes: negative return values.
pub const NOT_OWNED: i32 = 2; pub const INVALID: i32 = 3;   // 1 is unused: a dying kernelet's call never returns
pub const LIMIT: i32 = 4; pub const STATE: i32 = 5; pub const CANCEL: i32 = 6;

// `task_spawn` flags, `log_write` levels beyond the log crate's, and `stop` kinds.
pub const SPAWN_SUSPENDED: u32 = 1;   // created but not runnable until `task_unpark`
pub const LEVEL_CONSOLE: u8 = 8;      // bytes from the early console, no module
pub const STOP_EXIT: u32 = 0; pub const STOP_PANIC: u32 = 1; pub const EXIT_RESTART: u32 = 1 << 31;
```

Twenty-one functions. Return values are `0` or a positive count on success and `-code` on failure: `-NOT_OWNED` (a physical address outside the grant), `-INVALID` (a bad name, index, width or pointer), `-LIMIT` (a quota or `max_grains` reached), `-STATE` (the call is not legal now, including a sleeping call with preemption disabled), `-CANCEL` (a park cut short by a kill).

**Pointer arguments.** Four functions take pointers, and no function returns a value through one: results come back in registers (`MmioResult`, the packed job of `job_wait`, the name of `task_spawn`), because a host store through a kernelet-chosen pointer is a store the kernelet can make fault by unmapping the page beneath it, and a range check cannot prevent that. `ctx` is the one pointer the host writes through, and it must lie on the calling task's own kernel stack, host memory the kernelet cannot unmap, above the stack pointer at entry plus the host's own frame headroom and below the stack's top, so that it overlaps no host frame (the kernel proper keeps its `UserMode` as a local of the task's entry closure; checked on the tree: `kernel/core/src/thread/task.rs`). The three read-only pointers (`module`, `text`, `msg`) may lie in the image's regions or in a granted frame reached through the host's linear map, and the host reads them only with its fallible copy routines, whose exception-table entries the host's fault handler consults first for a kernelet task at any address ([User mode](virtualizing-ostd/user-mode.md)), so an unmapped page beneath them is `-INVALID`, not a host fault. Every pointer is used only during the call; the host copies what it needs and keeps no pointer afterward (invariant I4). This is the one class of host access to a kernelet's memory besides `guest_memory` on the control half, and both are bounded by a call.

### The prologue and epilogue every function shares

```rust
// ostd::kernelet::service, host build; the wrapper each table entry points at.
fn enter(may_sleep: bool, exempt_from_dying: bool) -> Result<Entered, i32> {
    let slot = cpu_slot();                                        // one GS-relative load
    let k = slot_table().get(slot.kernelet, slot.generation).ok_or(-INVALID)?;
    let st = current_task().kernelet_state();                     // host-private
    if !exempt_from_dying && k.info.dying.load(Acquire) != 0 { drop(k); terminate_current_task(st); }   // a quiescent point; nothing returns into `KW_TEXT`
    if may_sleep && k.task_record(st.name).preempt_count.load(Relaxed) != 0 { return Err(-STATE); }
    if stack_below_reserve() { k.mark_dying(KillReason::StackReserve); drop(k); terminate_current_task(st); }   // the reaper does the rest
    st.service_depth.store(1, Release);                           // invariant I7: now in host code
    k.accounts.service_calls.fetch_add(1, Relaxed);               // invariant I6
    Ok(Entered { k, st })
}
fn leave(e: Entered) {
    let Entered { k, st } = e;
    st.service_depth.store(0, Release);
    let dying = k.info.dying.load(Acquire) != 0;
    let throttled = k.info.throttled.load(Relaxed) != 0 && k.task_record(st.name).preempt_count.load(Relaxed) == 0;
    drop(k);                                                      // `terminate_current_task` never returns; nothing may be live on this stack
    if dying { terminate_current_task(st); }                      // a quiescent point
    if throttled { throttle_park(st); }                           // the CPU quota: parked until the period rolls
}

/// The wrapper of a hook-bearing call (`mmio_read`, `mmio_write`, `log_write`, `oops`, and the
/// hook branch of `grains_request`): the same prologue, then the hook on the CPU's hook stack
/// under `catch_unwind`, with the host's preemption disabled so that no other task can take the stack.
fn call_hook<R>(e: &Entered, f: impl FnOnce(&dyn KerneletHooks) -> R) -> R {
    let _host_preempt = disable_preempt();                        // the host's count, not the kernelet's
    match on_hook_stack(|| catch_unwind(|| f(&*e.k.hooks))) {    // `on_hook_stack` switches `rsp` in assembly and back
        Ok(r) => r,
        Err(_) => { e.k.mark_dying(KillReason::HostHookPanicked); /* the epilogue terminates the task */ Default::default() }
    }
}
```

The depth is stored only after every check has passed, so no error return leaves it at one; the stores are `Release` and the kill path's loads `Acquire`, so a remote observer that sees depth zero also sees every host lock the task released before it. A rule every service function follows: no host guard or owning object is live across the epilogue, since `terminate_current_task` resets the stack and never returns. `stop`, `task_exit` and `task_park` are exempt from the dying check, since a dying kernelet must still be able to end its tasks and a park must return `-CANCEL`; `task_park` and `job_wait` are the calls that sleep, and they are refused with `-STATE` under a nonzero preemption count, which mirrors OSTD's own `might_sleep` assertion. The stack check compares the current stack pointer with the task's stack base plus `STACK_RESERVE`, 64 KiB *estimated* (register A3); a task found below the reserve kills its kernelet, not just itself, because its kernelet-side locks would otherwise stay held. The reserve covers only OSTD's own service paths, which are shallow and measurable: the endovisor's hooks, whose depth is that of file systems and sockets, run on the CPU's **hook stack**, a 64 KiB (chosen) per-CPU host stack the wrapper switches to (about ten cycles, *estimated*) with the host's preemption disabled, so that no task can be switched out while on it and no other task can take it. A panic inside a hook is caught there: the host's panic entry (`__handler_entry`, [The rest](virtualizing-ostd/the-rest.md)) unwinds instead of aborting when the current task is inside a hook, the `catch_unwind` in the wrapper ends the kernelet with `HostHookPanicked`, and the machine continues. That turns the endovisor's device models from a machine-halt surface into a sandbox-halt surface, which is what fresh code should be. *Estimated cost of prologue and epilogue:* one GS load, one slot-table index, two atomic stores, four loads and three compares, about 25 cycles, on top of the indirect call; a bare crossing was measured on the booted prototype at 35 cycles against 34 for a plain call, without any prologue.

**A gap this does not close.** A stack overflow in *kernelet* code, between service calls, hits the guard page; on the tree the double-fault handler exists but has no interrupt-stack-table stack of its own (checked on the tree, `ostd/src/arch/x86/trap`), so the fault has no stack to be taken on and the machine resets. OSTD gives the double-fault vector an IST stack and a handler that, if the faulting instruction lies in `KW_TEXT` and the current task is at depth zero, kills that kernelet with `KillReason::StackOverflow` and switches away; if the fault is anywhere else, it halts the machine as today. **[unverified]** (register A6): that a kernelet-side overflow is always caught at depth zero with a recoverable state.

### Memory

- `grains_request(count, contiguous)`: asks for more memory now, `count` grains, as one physically contiguous run if `contiguous` is set. Grants at once, from the host's free memory, up to what `max_grains` allows; at `max_grains`, consults `KerneletHooks::on_grant_exhausted` if the policy allows, which may raise the limit. Zeroes the run and maps its metadata frames into `KW_META`, appends it to the host-written grant table, and publishes the new length in the info page before returning the number granted, possibly `0`; the kernelet then adds the run to its allocator ([Memory](virtualizing-ostd/memory.md)). *Cost:* one aligned segment allocation, the zeroing (*estimated* 50 to 100 µs per grain), the mappings and the table write, or the hook.
- `pt_root_register(root)`, `pt_root_unregister(root)`: the kernelet built a user page table whose root frame is `root`; the host records it so that `pt_activate` and the scheduler accept it, and so that destroy can find every root. Unregistering a root that any task has recorded as its address space, or that is active on any CPU, fails with `-STATE`, which vOSTD's per-task `Arc` makes impossible in practice ([Memory](virtualizing-ostd/memory.md)); a successful unregister invalidates the root's translations on every CPU it was active on before returning. *Checks:* `root` in the grant and not already registered; the root's 256 kernel-half entries equal the published ones, a backstop against a bug in vOSTD. *Cost:* one owner-array read, 256 loads, one insertion in the kernelet's root set; on unregister, a shootdown.
- `pt_activate(root)`: writes CR3 with `root` for the current task and records `root` in the task's host-private state, so that the host's `switch_to_task` restores it on every switch to this task and records the CPU in the root's active set; when the host switches from a kernelet task to a task of another kernelet or to a host task it loads the host kernel's root into CR3, since the tree's `switch_to_task` never writes CR3 and a freed grant frame must never remain a CPU's live root ([Faults, termination, and reclamation](faults-and-reclamation.md)), and clears its own `ACTIVATED_VM_SPACE` cache, so that the host thread's post-schedule handler re-activates its space rather than trusting a stale pointer (checked on the tree: `VmSpace::activate` early-returns when the cache matches). *Checks:* `root` registered, or the kernelet's own kernel page-table root. *Cost:* the CR3 write and, since window mappings are not Global, the loss of the window's translations.
- `tlb_shootdown(root, start, len)`: invalidates `[start, start+len)` on every host CPU in `root`'s active set, by inter-processor call, and waits; `len == u64::MAX` means everything non-Global under the root. A kernelet cannot send interrupts (absent `smp`), so this is how its `TlbFlusher` completes, at most once per flush batch ([Memory](virtualizing-ostd/memory.md)). The call arrives with the caller's preemption disabled, as the tree's flusher holds a guard, and its wait is a spin on the host's pending-interrupt set. *Checks:* `root` registered; the range inside the user half. *Cost:* one IPI per target CPU plus the wait; the target set is bounded by the kernelet's CPU set.

Every page-table entry of the window is written by the host when it grants a grain; nothing here maps anything ([Memory](virtualizing-ostd/memory.md)).

### Tasks

- `task_spawn(entry, arg, vcpu_mask, nice, flags) -> name`: asks the endovisor, through `KerneletHooks::spawn_task`, for a host kernel thread whose body is the trampoline that loads the kernelet's kernel page table and calls `EntryTable::run_task(entry, arg)`, with a fresh 512 KiB kernel stack (measured on the tree) charged to the kernelet's host-overhead account, with affinity the host CPUs of the virtual CPUs in `vcpu_mask` and the given `nice`, and makes it runnable unless `SPAWN_SUSPENDED`; the insertion into the task table re-checks `dying` under the table's lock, and the trampoline checks `DYING` before entering `run_task`, so a task spawned during a kill never runs kernelet code. *Checks:* `entry > 1`; fewer than `max_tasks` live tasks, else `-LIMIT`; `vcpu_mask` a nonempty subset of the CPU set. *Cost:* the hook, a kernel stack, a `Task` and a `Thread`, as `ThreadOptions::spawn` today, plus one record initialization.
- `task_exit() -> !`: the current task ends. The host removes it from the task table before switching away, and the reaper task frees the task object and its stack afterward ([Faults, termination, and reclamation](faults-and-reclamation.md)); the index is retired and its generation advanced. No kernelet destructor runs on the host's behalf.
- `task_destroy(name)`: ends a task that was spawned `SPAWN_SUSPENDED` and never unparked, freeing its stack; `-STATE` if it has ever run. *Cost:* the reap.
- `task_yield()`: the current task yields, as `Task::yield_now` does on the host.
- `task_park() -> 0 | -CANCEL`: parks the current task until `task_unpark(name)` or a cancellation. A park token is remembered: an unpark that arrives while the task is running sets `PARK_TOKEN`, and the next park consumes it and returns at once, so no wakeup is lost between a wait queue's enqueue and its park, which is the rule OSTD's own `park_current(has_unparked)` enforces today. A park that finds `CANCEL_PARK` set, or is interrupted by `kill`, returns `-CANCEL`; the epilogue then terminates the task.
- `task_unpark(name)`: makes the named task runnable if parked, or sets its token if running. *Checks:* the name's index and generation are this kernelet's and live. *Cost:* the scheduler's enqueue.
- `task_set_nice(name, nice)`, `task_set_vcpus(name, vcpu_mask)`: the kernelet's say over its own threads, applied through the kernel proper's own per-thread scheduling attributes; the host scheduler decides across sandboxes by the same attributes ([Tasks](virtualizing-ostd/tasks.md)).

### Jobs and time

- `job_wait() -> job`: parks the calling task, which must be a worker, until a job for its virtual CPU is posted, then returns it packed in the result. Jobs are: a virtual interrupt (`JOB_VIRQ`, with the line number), an idle tick (`JOB_TICK`, with the count of ticks coalesced, posted only while the kernelet is idle or a `timer_arm` deadline passes), or a new grant to read from the grant table (`JOB_GRANT`). Delivery is edge-triggered: the host clears a line's pending bit when it hands the job over, so a `raise_irq` that arrives while the handler runs becomes the next job rather than being lost; a line raised twice before delivery is one job. When the host parks the worker and no task of the kernelet is runnable on that CPU, it sets the virtual CPU's `quiescent` bit. A kill does not return from `job_wait`: the parked worker is terminated in the epilogue like any parked task. *Checks:* the caller is a worker. *Cost:* a park and an unpark per job; the delivery latency of a virtual interrupt is therefore a wakeup, which the Evaluation chapter will measure.
- `timer_arm(vcpu, deadline_ns)`: a one-shot: post `JOB_TICK` to the virtual CPU's worker at the first host tick at or after `deadline_ns` on the host's monotonic clock, or at once if past; a second call replaces the virtual CPU's deadline; `u64::MAX` cancels it. The busy tick needs no call: the host tick that finds a task of the kernelet running on a CPU adds to that virtual CPU's `tick_pending`, and the kernelet consumes it in task context at its next tick point ([Interrupts and time](virtualizing-ostd/interrupts-and-time.md)).

### User mode

- `user_run(ctx) -> 0 | 1 | 2`: performs one round trip into user mode with the register file in `ctx` and returns why it came back: `0` a system call, `1` an exception, `2` an interrupt that the host has already handled. `ctx` is a `RawUserContext` **on the calling task's own kernel stack**, `#[repr(C)]` and defined in `ostd::kernelet::abi` with the general registers, the instruction and stack pointers, the flags, and the trap number, error code and fault address the transition fills in, the trap module's own type under the feature; the host reads it before the transition and writes it after, and keeps nothing. The stack is required, not merely permitted, because the host's entry stubs use the context as a stack with interrupts disabled and no fixup; a context on a page the kernelet could unmap would double-fault the machine ([User mode](virtualizing-ostd/user-mode.md)). On the way back the host classifies what happened: a system call, a fault or trap of the tenant's, or an interrupt, NMI or machine check of the machine's, which it serves itself on this task before returning `2`; interrupts are re-enabled before every return. The kernelet's `execute` loop runs with its preemption count raised from the pre-run hook through this call, so that the FS and GS bases and the FPU state the hook loaded into the CPU are still there when the host switches to user mode under its own interrupt guard ([User mode](virtualizing-ostd/user-mode.md)). Before returning `2` for an interrupt, `user_run` copies the host's own need-preempt decision, which the host tick made inside the interrupt (checked on the tree: `ostd/src/task/scheduler/mod.rs`, `set_need_preempt`), into the task's `NEED_RESCHED` flag and its mirror, so that the kernelet's `might_preempt` at the loop top yields; without that copy a CPU-bound thread would be time-sliced only by `preempt_off_ticks`. *Checks:* `ctx` within the calling task's kernel stack, above the entry stack pointer plus the host's frame headroom, and aligned; the task has an activated address space; `dying` is re-checked under the interrupt guard immediately before the transition. *Cost:* the ring transition, as today, plus the host's interrupt work when one lands in user mode.

### Devices

- `mmio_read(dev, offset, width) -> MmioResult`, `mmio_write(dev, offset, width, value)`: one register access on virtual device `dev`, forwarded to `KerneletHooks::mmio_read` or `mmio_write` on the hook stack; the read's value comes back in a register. The hooks must not sleep, because a driver holds a spin lock across a register write as it would for real hardware; a notify write hands the queue's work to the endovisor's device thread ([Devices](virtualizing-ostd/devices.md)). *Checks:* `dev` in the device table, `offset + width ≤ reg_bytes`, `width ∈ {1, 2, 4, 8}`. *Cost:* the hook; for a notify, one wakeup of the device thread.

### Output, faults, and end of life

- `log_write(level, module, text)`: copies the text, with the fallible routines, into the host's rate limiter and, if under the kernelet's limit, delivers it to `KerneletHooks::log`; otherwise drops it and counts. `LEVEL_CONSOLE` with an empty module is the early console's bytes. Formatting happened in the kernelet. *Checks:* `text_len ≤ 1024`; a buffer that faults is `-INVALID`. *Cost:* the copy and the hook.
- `oops(msg, len) -> 0`: a task of the kernelet caught a panic and continues. The host charges the oops budget, calls `KerneletHooks::on_oops`, and, at the budget, kills the kernelet with `KillReason::OopsBudget`, which the epilogue then enforces.
- `stop(kind, code, msg, len) -> !`: the kernelet ends. `STOP_EXIT` with `code` is `power::poweroff`, `restart` or `exit_with_code` in the kernel proper (`restart` sets `EXIT_RESTART` in the code), recorded as `Exited(code)`; `STOP_PANIC` with `msg` is a panic the kernelet's own handler could not turn into an oops, or an allocation failure the kernel could not absorb, recorded as `Panicked(msg)`. Either way the call marks the kernelet dying, which signals the reaper task to run `on_dying`, and terminates the calling task; the other tasks are canceled and terminated at their next quiescent point ([Faults, termination, and reclamation](faults-and-reclamation.md)). Nothing unwinds across the boundary.

## What the service half does not offer

There is no function to read or write host memory, to map anything outside the window, to allocate a frame by physical address, to disable interrupts, to send an interrupt, to change the CPU set, to load a page table that was not registered, or to call anything by address. Each of those is an *absent* item in the taxonomy, and its absence from this table is what makes the absence real at the ELF boundary (invariant I1).

## What this page decides

- **A flat function table, with the caller identified from the host's per-CPU slot** (register D7).
- **Task records shared read-write carry only the preemption count and a read-only mirror of the flags** (register D8, revised). The termination counter and the host's flags are host-private, so a kernelet cannot make itself unkillable; what it can corrupt harms only itself.
- **One worker per virtual CPU** (register D11), fetching virtual interrupts, grants and idle ticks by `job_wait`; the host never enters the entry table except at task start, which is what lets invariant I7's counter be a single bit per task. Per-virtual-CPU workers are what let the kernel proper's per-CPU interrupt and bottom-half code run on the virtual CPU it believes it is on; the busy tick is consumed by the interrupted virtual CPU's own tasks (register D66).
- **No service call returns a value through a pointer** (register D65). Results come back in registers, because a host store through a pointer the kernelet chose is a store the kernelet can make fault; the three read-only pointers go through fallible copies. The price is a `MAX_VCPUS` of 64, so that a virtual-CPU set fits a register.
- **Hooks do not sleep** (register D12). Device register accesses happen under drivers' spin locks; the endovisor's device models therefore do their host I/O on device threads that a notify wakes, and a hook is a bounded amount of state manipulation.
