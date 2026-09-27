# The kernelet API: control half

*Answers question 1: what API does OSTD provide so that the endovisor can manage the lifecycle of kernelets and customize their behavior?*

The control half is the module `ostd::kernelet::control`, present only in the host build. It is the whole of what the endovisor sees of a kernelet: it registers images, creates kernelets, gives them memory, CPUs and devices, starts, observes, kills and destroys them, and it supplies the hooks OSTD calls back when a kernelet's request needs the host kernel's Linux functionality. Everything on this page is host-side Rust; no kernelet ever holds a reference to any of it. Invariants enforced here: I3 (privacy), I4 (no retained reference), I6 (charged work), and the host side of I7 (termination).

## Division of labor over the window

The control half and the service half share the [window](builds-and-images.md#window) with vOSTD, and each region has exactly one mapper:

| region | mapped by | frames from | when |
|---|---|---|---|
| the metadata window's page-table frames | control half | host memory, charged to the kernelet's host-overhead account | `create`, and `grant` or `grains_request` when a grain lands in a range not yet covered |
| `KW_TEXT` | control half | the kind's shared frames, 2 MiB-aligned so that they map as 2 MiB pages | `create` |
| `KW_DATA` (template and replicas) | control half | host memory, charged to the kernelet, one 2 MiB page | `create` |
| `KW_SHARED` | control half | host memory, charged to the kernelet | `create` |
| `KW_META`, eight entries per grain | control half | host memory, charged to the kernelet | `create`, `grant`, `grains_request` |

So the host maps the image's regions at creation and the metadata of a grain when it grants it, through its linear map; granted frames themselves need no mapping, since the linear map already holds them; the grant table the kernelet reads is a host-written page in `KW_SHARED` ([Memory](virtualizing-ostd/memory.md)). The kernelet writes no page-table entry of the window and no page-table entry in host memory; the only page tables it writes are its own user page tables, in frames of its grant.

## Images

An image is registered once per kind, at boot, from bytes embedded in the host image ([Builds and images](builds-and-images.md)).

```rust
pub struct ImageId(u16);

/// Parses a kernelet image, checks it, and installs it as a kind. Copies the
/// read-only segments into 2 MiB-aligned shared frames, keeps the writable segment
/// as the data template, and reads the entry table at `KW_TEXT + 4 KiB`.
pub fn register_image(name: &'static str, elf: &'static [u8]) -> Result<ImageId, ImageError>;

pub enum ImageError {
    NotElf, WrongArch, HasRelocations, HasUndefinedSymbols, SegmentOutsideWindow,
    WritableAndExecutable, EntryOutsideText, EntryTableMisplaced,
    BadTableBounds, SourceHashMismatch, NoMemory,
}

pub struct ImageInfo {
    pub name: &'static str,
    pub text_bytes: usize,        // shared, read-only
    pub template_bytes: usize,    // `.data` + `.bss`, copied per kernelet
    pub cpu_local_bytes: usize,   // replicated per virtual CPU
}
pub fn image_info(id: ImageId) -> Option<&'static ImageInfo>;
```

Registration repeats, in trusted code and on the bytes actually booted, every check of the [audit](builds-and-images.md#audit) that can be made on the ELF alone: every relocation of the one permitted type and inside the per-instance region, none in the shared regions, no undefined symbols, every segment inside the `KW_TEXT` or `KW_DATA` offset range and none both writable and executable, `e_entry` inside `KW_TEXT`, the entry table at its fixed offset with the expected size, the exception-table and `.cpu_local` bounds inside their segments, and the source hash equal to the host build's. The bounds check is what invariant I5 needs before the host ever jumps to an exception-table fixup. A failure is a boot-time error for that kind, not for the host.

## Identity

```rust
/// Names a kernelet for its whole life and never repeats: a slot index into a
/// fixed table and a generation, starting at 1, that the slot's reuse increments.
/// Every host table that refers to a kernelet stores this and nothing else
/// (invariant I4). `Option<KerneletId>` is 8 bytes because the generation is non-zero.
#[derive(Copy, Clone, PartialEq, Eq, Hash, Debug)]
pub struct KerneletId { pub slot: u16, pub generation: NonZeroU32 }

/// Build-time bound on live kernelets; sizes the slot table. Chosen, not measured.
pub const MAX_KERNELETS: usize = 4096;
/// A task's name inside its kernelet: an index into the kernelet's task table.
pub type TaskName = u32;
```

## Configuration

What a kernelet is given is decided before it runs and, except for memory and the CPU budget, cannot change afterward.

```rust
pub struct KerneletConfig {
    pub image: ImageId,
    /// Host CPUs this kernelet's tasks may run on. Its virtual CPU `i` is the
    /// `i`-th CPU of the set in ascending order; `num_cpus()` inside the kernelet
    /// is the set's size. Fixed for the kernelet's life. At most `MAX_VCPUS` (64), so
    /// that a virtual-CPU set is one `u64` on the wire.
    pub cpus: CpuSet,
    /// The most live tasks the kernelet may have. Each is a host kernel thread with a
    /// 512 KiB stack in host memory, charged to the kernelet's host-overhead account,
    /// so this bound is what keeps a kernelet's host footprint bounded (register D64).
    pub max_tasks: u32,
    /// Memory, in 2 MiB grains: granted at creation, and the most the kernelet may
    /// take by itself. `grant` raises both.
    pub initial_grains: u32,
    pub max_grains: u32,
    pub budget: CpuBudget,
    /// The kernel command line the kernelet's kernel sees as `boot_info().kernel_cmdline`.
    pub cmdline: String,
    /// Virtual devices, in the order the kernelet's MMIO bus enumerates them.
    pub devices: Vec<DeviceDesc>,
    pub policy: KerneletPolicy,
}

pub struct CpuBudget {
    /// Applied as the `nice` value of every thread of the kernelet. The host's scheduler has
    /// no groups and no bandwidth control: its fair class knows per-thread `nice` weights and
    /// per-thread affinity only (checked on the tree: `kernel/core/src/sched/sched_class/fair.rs`),
    /// so this is a per-thread weight, not a share for the sandbox as a whole, and a sandbox
    /// with more runnable threads gets more of the machine, as on Linux without cgroups.
    pub nice: i8,
    /// Hard cap: at most `quota_us` of CPU time per `period_us`, over all its tasks. `None` is
    /// uncapped. Enforced by OSTD's own throttle, not the scheduler: the host tick charges the
    /// running task's kernelet, and once a period's usage exceeds the quota every task of the
    /// kernelet is parked at its next quiescent point until the period rolls (register D62).
    pub quota: Option<(u32 /* quota_us */, u32 /* period_us */)>,
}

/// One virtual MMIO device. The register file is served by the endovisor's
/// device model through `KerneletHooks::mmio_read` and `mmio_write`; OSTD only
/// knows the device's size and interrupt line. The kernelet's virtio MMIO
/// transport finds it in `BootArgs` ([Devices](virtualizing-ostd/devices.md)).
pub struct DeviceDesc {
    pub id: DeviceId,          // chosen by the endovisor; unique within the kernelet
    pub kind: DeviceKind,      // `VirtioMmio { device_type: u32 }` for now
    pub reg_bytes: u32,        // size of the register window, a multiple of 4 KiB
    pub irq: Virq,             // the virtual interrupt line the device raises
    pub vcpu: u16,             // the virtual CPU whose worker delivers it; 0 if unbound
    pub mmio_base: u64,        // the pseudo-physical address the kernelet's bus probe finds it at
}
#[derive(Copy, Clone, PartialEq, Eq, Hash, Debug)] pub struct DeviceId(pub u16);
#[derive(Copy, Clone, PartialEq, Eq, Hash, Debug)] pub struct Virq(pub u8);   // 32..=255; 0 is the tick, 1..=31 reserved

pub struct KerneletPolicy {
    /// Oopses (caught panics, reported through the `oops` service call) allowed before the kernelet is killed.
    pub oops_budget: u32,
    /// Consecutive ticks a task may hold preemption off before the kernelet is killed.
    pub preempt_off_ticks: u32,
    /// Log bytes per second delivered to the hook; beyond it, records are dropped and counted.
    pub log_bytes_per_sec: u32,
    /// When the kernelet has taken `max_grains` and asks for more: consult
    /// `on_grant_exhausted` (true) or refuse at once (false).
    pub ask_before_oom: bool,
    /// Ticks per second posted to an idle kernelet's virtual CPU 0, so that its kernel's
    /// timers still fire; 0 means none ([Interrupts and time](virtualizing-ostd/interrupts-and-time.md)).
    pub idle_tick_hz: u32,
}
```

`create` validates the configuration and fails with one of:

```rust
pub enum CreateError {
    UnknownImage, NoCpus, TooManyCpus, CpuNotOnline(CpuId), ZeroMaxTasks,
    InitialExceedsMax, ZeroMaxGrains, PaddrAboveWindow, DuplicateDevice(DeviceId), ReservedVirq(Virq), BadVcpu(DeviceId), MmioBaseInvalid(DeviceId),
    RegBytesNotPageMultiple(DeviceId), TooManyDevices, CmdlineTooLong,
    SlotsExhausted, NoMemory,
}
```

## Hooks: how OSTD calls the endovisor

The endovisor customizes a kernelet's behavior by implementing one trait per kernelet and passing it at creation. The four *request* hooks (`mmio_read`, `mmio_write`, `log`, `on_grant_exhausted`) and `on_oops` run on the kernelet's own task, inside a service call, with the kernelet's page table active, and their time is charged to the kernelet. They must not sleep: a driver holds a spin lock across a register write as it would for real hardware, so a hook is a bounded amount of state manipulation, and anything that needs host I/O is handed to a device thread ([Devices](virtualizing-ostd/devices.md)). The service wrapper runs each of them on the CPU's **hook stack**, a per-CPU host stack it switches to with the host's preemption disabled, so that a hook's depth, which is the depth of the endovisor's device models and of the Linux functionality beneath them, never counts against the kernelet task's own stack reserve, and under `catch_unwind`, so that a panic in a hook unwinds to the wrapper and ends the kernelet with `KillReason::HostHookPanicked` rather than the machine (register D63). `spawn_task` runs on the kernelet's task too, on its own stack, and may allocate. `on_dying` and `on_exited` run on the host's **reaper task**, never on a kernelet's task or in the context that detected the death, which may be an interrupt handler; `on_exited` is therefore the one hook that may sleep, and it runs after the last kernelet task's stack has been freed ([Faults, termination, and reclamation](faults-and-reclamation.md)). The hooks are the only path from a kernelet's requests into the host kernel's Linux functionality.

```rust
pub trait KerneletHooks: Send + Sync + 'static {
    /// Creates a host kernel thread for one task of the kernelet. The host's scheduler runs
    /// only tasks that are the kernel proper's threads (its class scheduler begins with
    /// `task.as_thread()?`; checked on the tree: `kernel/core/src/sched/sched_class/mod.rs`),
    /// and OSTD cannot make a `Thread`, so the endovisor makes it: `ThreadOptions::new(move || body.run())`
    /// with the hints, not yet running. `body` is opaque to the endovisor; `run` is the
    /// trampoline that loads the kernelet's page table and enters `run_task` (register D61).
    fn spawn_task(&self, k: &Kernelet, body: KerneletTaskBody, hints: SpawnHints) -> Result<Arc<Task>, SpawnError>;

    /// A read of `width` bytes (1, 2, 4 or 8) at `offset` in device `dev`'s register file.
    fn mmio_read(&self, k: &Kernelet, dev: DeviceId, offset: u32, width: u8) -> u64;
    /// The matching write. A write to a virtio queue's notify register is where a
    /// device model walks the queue; it does so through `k.guest_memory()`.
    fn mmio_write(&self, k: &Kernelet, dev: DeviceId, offset: u32, width: u8, value: u64);

    /// A formatted log record from the kernelet's `log` macros, or, at `LogLevel::Console`
    /// with an empty module, bytes from its early console (`early_print!`); already rate-limited.
    fn log(&self, k: &Kernelet, level: LogLevel, module: &str, text: &str);

    /// The kernelet has taken `max_grains` and asks for more, and `policy.ask_before_oom`
    /// is set. Return how many grains to add to both the grant and `max_grains`;
    /// `0` refuses, and the kernelet's allocator sees an empty grant.
    fn on_grant_exhausted(&self, k: &Kernelet) -> u32 { 0 }

    /// A task of the kernelet took an oops (a caught panic); the budget has been charged.
    fn on_oops(&self, k: &Kernelet, task: TaskName, message: &str) {}
    /// The kernelet has entered `Dying`. Called once, on the reaper task; must not block.
    /// The host kernel has no I/O cancellation (a file or socket operation on the tree runs
    /// to completion on the caller's task), so what the endovisor does here is shut down or
    /// close the host objects its device models hold and wake their threads, which then
    /// return with an error and disown; until they do, `destroy` returns `Zombie`.
    fn on_dying(&self, k: &Kernelet, reason: &ExitReason) {}
    /// Every task has stopped and the kernelet is `Exited`; `destroy` may now be called.
    fn on_exited(&self, k: &Kernelet, status: &ExitStatus) {}
}

#[derive(Copy, Clone, PartialEq, Eq, Debug)]
pub enum LogLevel { Emerg, Alert, Crit, Error, Warn, Notice, Info, Debug, Console }

/// The body of a kernelet task, opaque to the endovisor: the kernelet, the entry index and
/// the argument. `run` never returns: it activates the kernelet's kernel page table, enters
/// `EntryTable::run_task(entry, arg)`, and the task ends with `task_exit`.
pub struct KerneletTaskBody { /* OSTD-private */ }
impl KerneletTaskBody { pub fn run(self) -> !; }
pub struct SpawnHints { pub vcpus: CpuSet, pub nice: i8, pub suspended: bool }
pub enum SpawnError { NoMemory, TooManyThreads }
```

**What a hook may call.** Inside a hook, these methods of `Kernelet` are safe: `id`, `state`, `config`, `stats`, `guest_memory`, `raise_irq`, `charge_host_bytes`, `uncharge_host_bytes`, `adopt_current_task` and `disown_current_task`. A device that completes at once, an entropy source, may raise its interrupt from inside `mmio_write`; every other completion is the device thread's ([Devices](virtualizing-ostd/devices.md)). These are not: `grant`, `kill`, `wait_exited` and `destroy`, which either take the state word the hook's caller holds or block on the task the hook is running on.

**A hook must return in bounded time, and may not sleep.** Neither is enforced; a hook runs with the host's preemption disabled on the CPU's hook stack, so a hook that sleeps is a host bug the tree's own `might_sleep` assertion catches. A task inside a hook is at service-call depth one, so `kill` cannot terminate it, `wait_exited` times out, and `destroy` is never legal; a kernelet with a task stuck in a hook holds its grant until the hook returns, as a process stuck in uninterruptible sleep holds its memory. A hook may take host spin locks, which raise the host's own per-CPU preemption count, not the kernelet's; that is why the hook path is at depth one and not terminable. Because hooks cannot sleep, the host I/O behind a device is done by a **device thread** the endovisor runs per device, woken by the notify hook and handing completions back with `raise_irq`. `on_dying` is where the endovisor shuts down those threads' host objects so that pins drain.

**Host memory a device model allocates** inside a hook, a `Vec` for a request or frames for a bounce buffer, is charged to no one by the host's allocators, which do not consult the CPU slot. The endovisor accounts for it explicitly through `charge_host_bytes`, and bounds it by the queue depths its device models accept ([Devices](virtualizing-ostd/devices.md)).

**Device threads have no process.** The host kernel's file and socket paths that need a current process (credentials, a root directory, a file-descriptor table) cannot be entered from a kernel thread. The endovisor therefore opens every host resource a device needs, a backing file, a tap device, a socket, at attach time on the kernelet runtime's process, through the [endovisor ABI](endovisor.md), and holds the resulting kernel objects; a device thread drives those objects. **[unverified]** (register A5): that the host kernel's file and socket objects can be driven by a kernel thread without a process context. If some cannot, the device thread carries a borrowed context from the runtime's process. A device thread's CPU time is charged to the kernelet it serves by adoption, which makes the host tick charge it as it charges the kernelet's own tasks:

```rust
impl Kernelet {
    /// Charges the calling host task's CPU time to this kernelet until `disown_current_task`.
    /// Used by the endovisor's device threads. Hook-safe. Fails once the kernelet is `Dying`,
    /// so that nothing adopts into a kernelet that is being drained.
    pub fn adopt_current_task(&self) -> Result<(), StateError>;
    pub fn disown_current_task(&self);
}
```

Device models need to read and write the kernelet's memory: a virtio queue lives in the kernelet's frames. The control half gives them a checked, scoped accessor rather than a mapping:

```rust
impl Kernelet {
    /// Runs `f` with a reader and writer over `len` bytes at physical address `paddr`
    /// of this kernelet's memory. Fails unless every frame of the range is in the
    /// kernelet's grant. Every grain of the range is pinned for the duration of `f`;
    /// a destroy that finds a pin does not wait but returns `Zombie`, and is retried
    /// once the pin is gone. The reader and writer go through the host's linear map
    /// and are valid only inside `f`.
    pub fn guest_memory<R>(
        &self, paddr: Paddr, len: usize,
        f: impl FnOnce(&mut VmReader<'_, Infallible>, &mut VmWriter<'_, Infallible>) -> R,
    ) -> Result<R, MemoryAccessError>;

    /// Explicit accounting of host memory a device model holds for this kernelet.
    pub fn charge_host_bytes(&self, bytes: usize) -> Result<(), LimitError>;
    pub fn uncharge_host_bytes(&self, bytes: usize);
}
pub enum MemoryAccessError { NotOwned, Destroying }
```

This is the one place outside a service call's own pointer arguments where host code touches a kernelet's memory, it is bounded by a closure, and the pin is what lets [Faults, termination, and reclamation](faults-and-reclamation.md) release frames without a use-after-release: no accessor outlives its pin, and destroy releases nothing while a pin is held.

## Lifecycle

```rust
pub struct Kernelet { /* host-side state; see below */ }

impl Kernelet {
    /// Builds a kernelet: its slot and identifier; its kernel page table (the host's
    /// mapped at a base of the host's choosing); the image's
    /// host-mapped regions (`KW_TEXT` from the kind's shared frames, `KW_DATA` from the
    /// template with the per-virtual-CPU replicas, `KW_SHARED` with `BootArgs`, the info
    /// page and the task records); the initial grant of `initial_grains` grains, zeroed,
    /// addressable through the host's linear map, with their metadata frames mapped into `KW_META`, recorded in
    /// the grant table and the owner array ([Memory](virtualizing-ostd/memory.md)); the
    /// device table; and, through the `spawn_task` hook, one worker thread per virtual CPU
    /// (`run_task(1, vcpu)`, suspended) and the boot thread (`_kernelet_entry`), all
    /// created but not runnable. Fails, with everything undone, on any error.
    pub fn create(config: KerneletConfig, hooks: Arc<dyn KerneletHooks>) -> Result<Arc<Kernelet>, CreateError>;

    pub fn id(&self) -> KerneletId;
    pub fn state(&self) -> KerneletState;
    pub fn config(&self) -> &KerneletConfig;

    /// `Created → Running`: the boot task becomes runnable and enters `_kernelet_entry`
    /// on the kernelet's kernel page table; it unparks the workers once its tables exist,
    /// so a `raise_irq` before then waits as a pending bit ([The rest](virtualizing-ostd/the-rest.md)).
    pub fn start(&self) -> Result<(), StateError>;

    /// Adds `grains` to the grant and to `max_grains` as one run if it can and as several
    /// otherwise, maps them and their metadata into the window, appends them to the grant
    /// table, and posts `JOB_GRANT` so that the kernelet adds them to its allocator. Legal
    /// in `Created` and `Running`.
    /// Memory only grows; a kernelet returns memory by exiting. Every grain is zeroed
    /// before it is published (register D55), so no tenant's data reaches another.
    pub fn grant(&self, grains: u32) -> Result<u32 /* granted */, GrantError>;

    pub fn set_budget(&self, budget: CpuBudget) -> Result<(), StateError>;

    /// Injects a virtual interrupt: sets the line's pending bit and wakes the worker
    /// of the virtual CPU the line is bound to. Idempotent while the line is pending.
    /// Legal in `Running`; hook-safe.
    pub fn raise_irq(&self, virq: Virq) -> Result<(), StateError>;

    /// Asks the kernelet to die. `Created → Exited` at once, since nothing has run.
    /// `Running → Dying`: marks the kernelet dying (register D56); the reaper task then
    /// cancels every parked task and lets each task reach its next quiescent point, where
    /// it is terminated. Idempotent in `Dying`. Returns at once.
    pub fn kill(&self, reason: KillReason) -> Result<(), StateError>;

    /// Blocks the calling host task until the kernelet is `Exited`, or until `timeout`.
    pub fn wait_exited(&self, timeout: Option<Duration>) -> Result<ExitStatus, Timeout>;

    /// Reclaims everything. `Exited → Destroying` by compare-and-swap, then waits for
    /// in-progress control operations to drain (they are short), then drains every host
    /// table that names this kernelet, unmaps the window, drops the host `Segment` that
    /// holds each run, frees the host objects, and retires the slot
    /// (`Destroyed`). If any grain is pinned by a `guest_memory` in progress, returns
    /// `Zombie` without releasing anything; the kernelet stays charged and `destroy`
    /// is retried once the pin is gone.
    pub fn destroy(&self) -> Result<ReclaimReport, DestroyError>;

    pub fn stats(&self) -> KerneletStats;
}

#[derive(Copy, Clone, PartialEq, Eq, Debug)]
pub enum KerneletState { Created, Running, Dying, Exited, Destroying, Destroyed }

pub enum KillReason {
    Requested, OopsBudget, PreemptOffTooLong, StackReserve, StackOverflow,
    /// A hook of the endovisor panicked on this kernelet's task; caught by the service wrapper.
    HostHookPanicked,
    /// A page fault in kernelet code with no exception-table entry ([User mode](virtualizing-ostd/user-mode.md)).
    KernelFault { addr: u64, ip: u64 },
    HostPolicy(u32),
}

pub enum ExitReason {
    /// The kernelet's kernel called `power::poweroff`, `restart` or `exit_with_code`; the
    /// code it passed, with `EXIT_RESTART` (bit 31) set by `restart`. `0` maps to OSTD's
    /// `ExitCode::Success`, anything else to `Failure`.
    Exited(u32),
    /// A panic that could not be caught inside the kernelet, with its message. An
    /// allocation failure the kernelet's kernel could not absorb ends here too.
    Panicked(String),
    Killed(KillReason),
}
pub struct ExitStatus { pub reason: ExitReason, pub uptime: Duration, pub cpu_time: Duration }

pub struct StateError { pub state: KerneletState }
pub enum GrantError { State(KerneletState), Limit, NoMemory }
pub enum DestroyError { NotExited(KerneletState), Zombie { pins: u32, adopted: u32 } }
pub struct Timeout;
pub struct LimitError;

pub struct ReclaimReport {
    pub grains_released: u32,
    pub tasks_reaped: u32,
    pub roots_forgotten: u32,      // registered page-table roots
    pub host_bytes_uncharged: usize,
}

pub struct KerneletStats {
    pub grains_granted: u32,
    pub max_grains: u32,
    pub host_bytes_charged: usize,
    pub tasks: u32,
    pub cpu_time: Duration,
    pub service_calls: u64,
    pub mmio_accesses: u64,
    pub irqs_raised: u64,
    pub log_bytes: u64,
    pub log_records_dropped: u64,
    pub oopses: u32,
    /// Host memory held for the kernelet outside its grant: stacks, metadata frames, page tables, templates.
    pub host_overhead_bytes: usize,
    /// Time the kernelet's tasks spent parked by the quota throttle.
    pub throttled: Duration,
}
```

**The state machine.** `Created → Running → Dying → Exited → Destroying → Destroyed`, with two shortcuts: `Created → Exited` by `kill`, and `Exited → Destroying → Exited` when `destroy` returns `Zombie`. Every transition is one compare-and-swap on the state word. `start` is legal in `Created`; `grant` in `Created` and `Running`; `set_budget` and `raise_irq` in `Running`; `kill` in `Created`, `Running` and `Dying`; `destroy` in `Exited`. A service call that finds `dying` set on entry terminates its caller instead of proceeding ([service half](kernelet-api-service.md)).

**Concurrency.** Two host tasks may operate on one kernelet at once, and a host task may operate on it while its tasks run. Every `&self` operation except `destroy` increments an operation count on entry and decrements it on exit, and refuses with `StateError` if the state is `Destroying` or `Destroyed`; `destroy` moves to `Destroying` first and then waits for the count to reach zero before touching any field, so no operation runs against a kernelet being torn down. The grant table is append-only: a chunked array whose count is published with a release store after the chunk is written, so a reader that indexes below the count never sees a partial entry, and no reallocation happens under a reader. The owner array is written by `create`, `grant` and `destroy` only, under the grant table's append order. The pending-interrupt bitmap and the accounts are atomics.

**Memory growth.** A kernelet takes grains by itself through the service half's `grains_request` until it holds `max_grains`; no host decision is involved below that line, and the grains come from the host's free memory when the request is made. At `max_grains`, the request consults `on_grant_exhausted` if `ask_before_oom` is set, and otherwise returns nothing. A refused request reaches the kernel proper as `Error::NoMemory` from `FrameAllocOptions`, and from there the kernelet's own out-of-memory handling applies, `ENOMEM` to the process or the kernelet's OOM killer ([Memory](virtualizing-ostd/memory.md)). The host never kills a kernelet for memory; a kernelet whose kernel cannot absorb an allocation failure panics, and that is `Panicked` ([Faults, termination, and reclamation](faults-and-reclamation.md)).

## What the host keeps per kernelet

The fields of `Kernelet`, listed so that the destroy sequence can be checked against them and so that invariant I4 can be reviewed: every field is host memory, and the only window addresses among them are the mapping records that destroy unmaps.

| field | what it holds | who writes it |
|---|---|---|
| `id`, `state`, `ops_in_progress` | identity; the state word; the operation count | control half |
| `image`, `config`, `hooks` | the kind, the configuration, the endovisor's hooks | creation |
| `image_base: Vaddr`, `meta_base: Vaddr` | where this instance's image and metadata window were placed, and the page-table frames beneath the metadata window, which the host allocates as grains land in new ranges | creation, `grant`, `grains_request` |
| `grant` | the grant table: each run's physical base and length, mirrored read-only into `KW_SHARED`, plus each run's pin count, its host `Segment`, and the metadata frames dedicated to it; append-only, chunked | `create`, `grant`, `grains_request`; pins by `guest_memory` |
| `roots` | the user page-table roots the kernelet has registered, each with its active set of CPUs | the service half |
| `budget`, `throttle` | the `nice` applied to the kernelet's threads; the quota's period accounting and the `throttled` flag every task's quiescent points read | `create`, `set_budget`, the host tick |
| `timer_deadlines: [AtomicU64; MAX_VCPUS]`, tick-list membership | the per-virtual-CPU `timer_arm` deadlines, and whether the host tick posts to this kernelet | `timer_arm`; `start` and `mark_dying` |
| `tasks` | the kernel threads that are this kernelet's tasks, by name, each an `Arc<Task>` the `spawn_task` hook returned, with its stack charged to the host-overhead account; the boot task and the workers among them, with each worker's virtual CPU; at most `max_tasks` live | the service half's spawn and exit |
| `devices`, `virq_pending: [AtomicU64; 4]` | the device table and the pending-interrupt bitmap | creation; `raise_irq`; `job_wait` clears a bit when it hands the job over |
| `shared` | the frames mapped read-only (`BootArgs`, the info page) and read-write (the task records and the per-virtual-CPU records) into `KW_SHARED` | creation; the scheduler writes task records |
| `accounts` | the counters behind `stats()`, the host-bytes charge, and the host-overhead account | the service half, the host tick, the hooks |
| `exit: Once<ExitStatus>`, `exit_waiters: WaitQueue` | the outcome, and who is waiting for it | the reaper; `wait_exited` |

Two host-wide tables complete the picture. The **slot table** maps `KerneletId::slot` to the `Arc<Kernelet>` and the current generation, and is how the service half finds the caller's kernelet from the CPU slot in constant time; it drops its `Arc` at `Destroyed`. The **owner array** has one `Option<KerneletId>`, 8 bytes, per 2 MiB grain of physical memory, written when a grain is granted, before the run that holds it is published in the grant table, and cleared when it is released, after the run is retired; it is what `guest_memory` and the service half's ownership checks read, and its size is physical memory divided by 2 MiB times 8 bytes: 4 MiB for a 1 TiB machine.

The second version of devices widens each owner-array entry into an owner-and-lend word, the `KerneletId` plus a 16-bit count of the grain's frames lent to a device ([Zero-copy I/O](zero-copy-io.md)).

**Grains are 2 MiB-aligned.** The host allocates a grain with a new host-build entry point, `alloc_segment_aligned`, since `FrameAllocOptions::alloc_segment_with` asks its allocator for page alignment only (measured on the tree, `ostd/src/mm/frame/allocator.rs`). Alignment lets the kernelet map a grain as one 2 MiB page and lets the owner array be indexed by `paddr >> 21`. Its cost is fragmentation in the host's frame allocator, which the Evaluation chapter will measure; where an aligned grain cannot be had, `grant` fails with `NoMemory` rather than falling back to unaligned grains.

## Costs

Per kernelet, `create` costs, in host memory charged to the kernelet's host-overhead account: the page-table frames that map the image; one level-2 table for `KW_TEXT`, whose 2 MiB pages need no level-1 tables (about seven entries for the tree's 14 MiB debug image, measured with `size -A`); the level-2 and level-1 tables of `KW_META` that the initial grant touches; one level-2 and one or two level-1 tables for `KW_SHARED`; the data template copy padded to 2 MiB, of which under 128 KiB is used (*estimated*, [Builds and images](builds-and-images.md)); the per-virtual-CPU replicas at about 2 KiB each, measured on the tree; the shared pages, including the grant table; eight metadata frames per grain; and one kernel thread per virtual CPU plus the boot thread, each with a 512 KiB kernel stack and four guard pages of vmalloc (measured on the tree: `DEFAULT_STACK_SIZE_IN_PAGES = 128`, `ostd/src/task/kernel_stack.rs`), which is the largest fixed item and the reason `max_tasks` exists. The host-side `Kernelet` object is a few kilobytes (*estimated*). Per run: one `alloc_segment_aligned`, the zeroing, the window and metadata mappings, one grant-table append, the owner-array writes, and, after boot, one `JOB_GRANT` wakeup. `raise_irq` is one atomic or and one wakeup. `kill` is a compare-and-swap, one store and a signal; the per-task work is the reaper's. `destroy` is linear in grains, tasks, roots and devices; its cost is the drain list of [Faults, termination, and reclamation](faults-and-reclamation.md).

Per hook call, the endovisor pays whatever its device model does; the control half adds, in `guest_memory`, one owner-array read and one pin increment and decrement per grain touched.

## What this page decides

- **Hooks are a trait object per kernelet, called synchronously on the kernelet's task** (register D5). The alternative, a queue of requests served by an endovisor thread, would keep host Linux code off the kernelet's task and its stack, but it turns every device register access into a cross-task round trip with a wakeup; the booted prototype measured a cross-domain round trip at 9.4 µs where a call into its virtualization layer cost 35 cycles against 34 for a plain call (see the Paper's Evaluation section). Synchronous hooks keep a register access at function-call cost; the price is that the hook runs with the host's preemption disabled on the CPU's hook stack, cannot sleep, and cannot be terminated while it runs. What needs host I/O goes to a device thread at the cost of one wakeup per queue notification, which is what a real device's DMA engine costs a driver too (register D12).
- **Memory only grows** (register A1). Reclaiming memory from a running kernelet needs its cooperation, a balloon, and a story for pages the kernelet has mapped to user space; none of that is needed to give every agent a sandbox that exits when it is done.
- **Device models live in the endovisor, not in OSTD** (register D6). OSTD knows a device only as a register window and an interrupt line, because a device model over host files and sockets is Linux functionality, written in the safe-Rust kernel proper, not in OSTD's `unsafe`-bearing framework.
- **The host maps every grain and its metadata into the window when it grants the grain** (register D58, on the [Memory](virtualizing-ostd/memory.md) page). The kernelet writes no page-table entry of the window.
- **A kernelet's tasks are the host kernel proper's threads, created through the `spawn_task` hook** (register D61). The alternative, OSTD creating bare host `Task`s, produces tasks the host's class scheduler never enqueues, since it dispatches on `as_thread()`; teaching the scheduler a second kind of task is more code in the kernel proper for less. The cost is one `Thread` object per kernelet task and one hook call per spawn, which is not a hot path.
- **The CPU quota is a throttle in OSTD; the weight is a per-thread `nice`** (register D62). The host has no group scheduler; building one is a scheduler project, not a sandbox one. The throttle costs one compare at every quiescent point and gives up proportional sharing between sandboxes.
- **Hooks run on a per-CPU host stack under `catch_unwind`** (register D63). The alternative, running them on the kernelet task's stack, makes the depth of the endovisor's Linux code a tenant-reachable machine halt whenever the stack reserve is wrong, and makes a panic in the endovisor's fresh code a machine halt; the stack switch costs about ten cycles and the landing pad nothing.
- **`max_tasks` bounds a kernelet's threads, and their stacks are charged** (register D64). Without it a sandbox could take 512 KiB of host memory per thread without limit.
