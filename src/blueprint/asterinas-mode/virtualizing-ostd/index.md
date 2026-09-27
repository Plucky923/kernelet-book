# Virtualizing OSTD

*Answers question 2: how is the API of vOSTD virtualized, item by item?*

This page is the taxonomy. Every public item of OSTD that the kernel proper uses, as enumerated in the [OSTD API inventory](../../../notes/ostd-api-inventory.md), is one of three things in vOSTD, and the tables below say which, how a virtualized item is implemented, which service-half function or shared page it relies on, what it costs, and what a tenant can observe. The six pages that follow hold the mechanisms: [Memory](memory.md), [Tasks, scheduling, and CPUs](tasks.md), [Interrupts and time](interrupts-and-time.md), [User mode](user-mode.md), [Devices](devices.md), and [Boot, power, panic, and the rest](the-rest.md).

## The three kinds

- **Identical.** The item's source has no `cfg(feature = "kernelet")` in it. It compiles into the kernelet image unchanged and has the same semantics, because its effect is local to the caller: it takes no host lock, allocates nothing the host keeps, stores no pointer to its argument that outlives the call, and queues no closure.
- **Virtualized.** The item keeps its public name and signature and gets a second body under the feature. The body serves the request from the kernelet's own state in its window, from a shared page, or through a service-table call. Some virtualized items never cross into the host (a `cpu_local!` read, `Jiffies::elapsed`); some always do (`Task::spawn`, an `IoMem` register write); the table says which.
- **Absent.** The item is `cfg(not(feature = "kernelet"))`. A use of it fails to compile in vOSTD with an unresolved name. Every absent item is either a machine operation a tenant must never have, or something whose only users are the components that stay in the host kernel.

The classification is executable, not a list to be trusted. For every item marked identical, a test in `ostd`'s kernelet-build test suite is to call it under instrumentation that counts host lock acquisitions, records every physical address written, and scans host structures for window addresses afterward; an identical item must leave all three unchanged. The test is what keeps the table true as OSTD changes (invariant I8).

## Counting

Over the inventory's items, grouped as the tables below group them and counted per row: 26 rows identical, 38 virtualized (of which 22 never cross into the host), 7 absent, and one internal to OSTD. The absent rows have their users in host-only components, except three items (`IoPort`, its access types and `ACPI_INFO`) used by the kernel proper's own `arch/x86/power.rs`; the places in the kernel proper's own code that need a `cfg` line are listed at the end of the page, and the files that name host-only components are counted there too. Counted over this page's tables; the inventory's import counts say how widely each item is depended on.

## `mm`: memory

| item | kind | how, and what it relies on | cost | what a tenant sees |
|---|---|---|---|---|
| `VmIo`, `VmIoOnce`, `VmIoFill`, `HasVmReaderWriter`, `VmReaderWriterResult` | identical | traits over readers and writers | none | nothing |
| `VmReader`, `VmWriter` (`Infallible`) | identical | kernel-space cursors; the pointers they hold are window or stack addresses | none | nothing |
| `VmReader`, `VmWriter` (`Fallible`) | virtualized | on a host that leaves the processor's supervisor-access check disabled, as Asterinas does, the copy routines are identical, with their exception-table entries, and the *fault* is handled by the host and completed by a retry loop in vOSTD that calls the kernel's injected page-fault handler ([User mode](user-mode.md)). On a host that enables the check, as Linux does, the tenant's own address may not be dereferenced at all and vOSTD copies through its alias of the frame instead (register D82) | one extra round trip per first-touch fault; on such a host, an address-to-frame lookup per copy | nothing |
| `Fallible`, `Infallible`, `FallibleVmRead`, `FallibleVmWrite`, `PodOnce`, `PodAtomic` | identical | markers and traits | none | nothing |
| `PAGE_SIZE`, `Vaddr`, `Paddr`, `Daddr`, `PagingLevel`, `MAX_USERSPACE_VADDR`, `KERNEL_VADDR_RANGE` | identical | constants | none | nothing |
| `HasPaddr`, `HasSize`, `HasDaddr`, `HasPaddrRange`, `Split` | identical | traits | none | nothing |
| `FrameAllocOptions` | virtualized | the identical call into the kernel proper's own global frame allocator, which manages the kernelet's runs; when that allocator is empty the body calls `grains_request` before returning `NoMemory` | none per call; one crossing per refill | `ENOMEM` when the grant is exhausted ([Memory](memory.md)) |
| `GlobalFrameAllocator` (hook) | identical | the kernel's allocator is fed runs by `add_free_memory` at boot and on each `JOB_GRANT` | none | physically contiguous allocations are bounded by the largest run the host can supply ([Memory](memory.md)) |
| `Frame<M>`, `UFrame`, `Segment<M>`, `USegment`, `UniqueFrame<M>`, `FrameRef` | identical | the metadata slot is found by the tree's `frame_to_meta` over the metadata window's base constant; the host maps a grain's slots when it grants the grain ([Memory](memory.md)) | none | nothing |
| `AnyFrameMeta`, `AnyUFrameMeta`, `impl_frame_meta_for!`, `impl_untyped_frame_meta_for!`, `GetFrameError`, `FRAME_METADATA_MAX_SIZE` | identical | the metadata protocol, over the kernelet's own slots | none | nothing |
| `frame::linked_list::{LinkedList, Link, CursorMut}` | identical | intrusive lists over the kernelet's own metadata | none | nothing |
| `heap::GlobalHeapAllocator` (hook), `HeapSlot`, `SlotInfo`, `Slab`, `SlabMeta`, `SlabSlotList` | identical | the tree's code over the host's linear map, through `paddr_to_vaddr` | none | nothing |
| `kspace::paddr_to_vaddr` | identical | the host's linear-map base plus `pa`, the tree's function over a base the host supplies at creation, never the linear map ([Memory](memory.md)) | none | nothing |
| `kspace::{KVirtArea, kernel_loaded_offset, LINEAR_MAPPING_*, VMALLOC_*}` | absent | kernel-half virtual memory is the host's; no user in the kernel proper | | |
| `VmSpace::new` | virtualized, no crossing | copies the host's 256 kernel-half entries, which `BootArgs` carries, into a root frame from the grant | none beyond today's | nothing |
| `VmSpace::activate` | virtualized | `pt_root_register` on first activation, `pt_activate` on each; each task holds the `Arc` of the space it last activated, so `Drop` unregisters without a pending list ([Memory](memory.md)) | one crossing per activation; the CR3 write | nothing |
| `VmSpace::{cursor, cursor_mut, reader, writer}`, `Cursor`, `CursorMut::{query, find_next, jump, map, unmap, protect_next}`, `VmQueriedItem`, `PageProperty`, `PageFlags`, `CachePolicy` | virtualized, no crossing | the page-table walk is identical; leaf and table frames come from the grant; a node frame's metadata is the kernelet's | none beyond today's | nothing |
| `CursorMut::flusher`, `tlb::{TlbFlusher, TlbFlushOp}` | virtualized | remote invalidation through `tlb_shootdown`, at most one call per flush batch: the operation itself for a batch of one, a flush of everything non-Global under the root above four, since a kernelet cannot send interrupts ([Memory](memory.md)) | one crossing and one IPI per target CPU per batch | nothing |
| `CursorMut::map_iomem`, `find_iomem_by_paddr` | virtualized, no crossing | unreachable in a kernelet: their only producer is the framebuffer device, a host-only component (checked on the tree: `device/fb.rs`); vOSTD makes `map_iomem` a no-op and `find_iomem_by_paddr` return `None` | none | nothing |
| `dma::{DmaStream, DmaCoherent, DmaDirection, ToDevice, FromDevice, FromAndToDevice}` | virtualized, no crossing | frames from the grant; `daddr` is `paddr`; `sync_*` are no-ops; ownership of a descriptor's frames is checked by the device model, not here ([Devices](devices.md)) | none | nothing |
| `fault::inject_user_page_fault_handler` | identical | the hook is stored and called kernelet-side by the retry loop above | none | nothing |
| `page_table::*` | internal | not public to the kernel proper | | |

## `sync`: synchronization

| item | kind | how, and what it relies on | cost | what a tenant sees |
|---|---|---|---|---|
| `SpinLock`, `SpinLockGuard`, `RwLock` and its guards, `SpinGuardian`, `PreemptDisabled`, `GuardTransfer` | identical | spin locks over kernelet state; the preemption count is the task record's | none | nothing |
| `LocalIrqDisabled`, `WriteIrqDisabled`, `SpinLock::disable_irq` | virtualized, no crossing | the guardian disables preemption instead of interrupts; sound because no kernelet code runs in interrupt context ([Interrupts and time](interrupts-and-time.md)) | none | nothing |
| `Mutex`, `MutexGuard`, `RwMutex` and its guards, `WaitQueue`, `Waiter`, `Waker` | virtualized | identical logic over `task_park` and `task_unpark` by name; a waker keeps the sleeper's `Arc<Task>` and derives the name at wake ([Tasks](tasks.md)) | one crossing per park and per wake | nothing |
| `Rcu`, `RcuOption`, their read guards, `non_null::*` | identical | the read side is preemption-disabled reads | none | nothing |
| `RcuDrop`, the write side | virtualized, no crossing | grace periods tracked per virtual CPU, advanced at the kernelet's own switch points (park, yield, return from `user_run`, a tick point, the worker's loop top), with an extended quiescent state the host sets when it parks a virtual CPU's worker with nothing runnable; callbacks run at the switch or tick point that completes the period ([Tasks](tasks.md)) | callbacks are deferred by up to one tick | nothing |
| `RwArc`, `RoArc` | identical | | none | nothing |

## `task`: tasks and scheduling

| item | kind | how, and what it relies on | cost | what a tenant sees |
|---|---|---|---|---|
| `Task`, `TaskOptions`, `CurrentTask` | virtualized | a kernelet-side `Task` is a name plus the kernel's `data` and `local_data`; `run` stores the body under an entry index, calls `task_spawn` suspended, registers the task and unparks it; the host task is a kernel thread the endovisor creates through the `spawn_task` hook; `current` reads the CPU slot; `yield_now` is `task_yield`; `TaskOptions` gains `cpu_affinity` and `nice` builders in vOSTD ([Tasks](tasks.md)) | two crossings and one hook per spawn, one crossing per yield | load figures and scheduler policy queries report the inert class scheduler's values |
| `disable_preempt`, `DisabledPreemptGuard` | virtualized, no crossing | increments the task record's `preempt_count`, which the host's kernel-mode preemption honors; the guard's drop reads `NEED_RESCHED` from the mirror and yields if asked | one store and one load; a `task_yield` crossing when the host has asked | nothing |
| `halt_cpu` | virtualized | `task_park`; unreachable, since vOSTD spawns no idle threads ([Tasks](tasks.md)) | none | nothing |
| `scheduler::inject_scheduler`, `Scheduler`, `LocalRunQueue`, `EnqueueFlags`, `UpdateFlags`, `info::*`, `AtomicCpuId`, `enable_preemption_on_cpu` | virtualized, no crossing | accepted and not consulted: the host's scheduler runs the kernelet's threads by their own `nice` and affinity, under the kernelet's quota; the kernel proper's scheduler classes are inert inside a kernelet | none | `nice`, `sched_setscheduler` and real-time policies reach the host only as the `nice` the `cfg` line forwards through `task_set_nice`; `sched_setaffinity` within the virtual-CPU set works through `task_set_vcpus`; load averages and run-queue statistics report the inert scheduler's zeros ([Tasks](tasks.md)) |
| `atomic_mode::{InAtomicMode, AsAtomicModeGuard, might_sleep}` | identical | | none | nothing |
| `inject_pre_schedule_handler`, `inject_post_schedule_handler` | virtualized, no crossing | called by vOSTD around its own voluntary switch points; involuntary switches are the host's, which saves and restores the user FS and GS bases and the FPU state itself ([Tasks](tasks.md)) | XSAVE and XRSTOR per involuntary switch | nothing |

## `irq`, `timer`: interrupts and time

| item | kind | how, and what it relies on | cost | what a tenant sees |
|---|---|---|---|---|
| `IrqLine::{alloc, alloc_specific, num, on_active, is_empty}`, `IrqCallbackFunction` | virtualized | virtual lines; a callback runs on the worker of the virtual CPU the line is bound to, in task context, when the host posts `JOB_VIRQ` ([Interrupts and time](interrupts-and-time.md)) | a wakeup per interrupt | nothing |
| `IrqLine::remapping_index` | absent | interrupt remapping is the host's | | |
| `disable_local`, `DisabledLocalIrqGuard` | virtualized, no crossing | disables preemption | one store | nothing |
| `InterruptLevel` | virtualized, no crossing | reports the level of the job a worker is delivering or the tick a task is consuming, recorded in the replica; the bottom-half dispatch, the CPU-time statistics, the process CPU clocks and the softirq guard all dispatch on it ([Interrupts and time](interrupts-and-time.md)) | none | nothing |
| `register_bottom_half_handler_l1`, `_l2`, and `bottom_half::process` | virtualized, no crossing | the hooks are identical; the dispatch runs on the worker after each virtual interrupt's top half and after each tick's callbacks, as on the tree; its `process_l1` drops its guard instead of forgetting it and enables no interrupts ([Interrupts and time](interrupts-and-time.md)) | none | nothing |
| `Jiffies`, `TIMER_FREQ` | virtualized, no crossing | `elapsed` reads the host-wide clock page's `jiffies`; the tick callback's increment of the counter is `cfg`'d out | one load | time is the host's |
| `register_callback_on_cpu` | virtualized | per-virtual-CPU callbacks run on that CPU's tasks when they consume a pending tick at a tick point, once per host tick; an idle kernelet's ticks arrive on its virtual CPU 0's worker at `idle_tick_hz` ([Interrupts and time](interrupts-and-time.md)) | one load per tick point; no wakeup while busy | timer latency up to `1 / idle_tick_hz` while the kernelet is idle |

## `cpu`, `user`: CPUs and user mode

| item | kind | how, and what it relies on | cost | what a tenant sees |
|---|---|---|---|---|
| `CpuId`, `CpuSet`, `AtomicCpuSet`, `num_cpus`, `all_cpus`, `CpuId::current_racy` | virtualized, no crossing | the virtual CPU namespace from `BootArgs`; `current` reads the CPU slot's `vcpu` | one load | `nproc` is the kernelet's CPU count |
| `PinCurrentCpu`, `PrivilegeLevel` | identical | | none | nothing |
| `cpu_local!`, `cpu_local_cell!`, `StaticCpuLocal`, `CpuLocalCell` | virtualized, no crossing | the replica for the current virtual CPU, addressed as `replica_base + vcpu × replica_bytes + offset`; single-instruction operations become load-and-store under a preemption guard, since the race is in forming the replica's address, not in the access | two loads per access instead of a GS-relative one; two stores per cell operation | nothing |
| `DynamicCpuLocal`, `DynCpuLocalChunk` | identical | used by the heap allocator's per-CPU allocator (checked on the tree); a chunk is a grant `Segment` addressed through the physical window and `get_on_cpu` indexes by `CpuId`, which is the virtual CPU ([Memory](memory.md)) | none | nothing |
| `UserMode::{new, context, context_mut}`, `UserContextApi`, `UserModeHooks`, `ReturnReason`, `UserContext`, `GeneralRegs`, `CpuException`, `CpuExceptionInfo`, `RawPageFaultInfo`, `PageFaultErrorCode`, `FpuContext` | identical | value types and the loop's hooks; `CpuException::from_raw`, the tree's `new` with the fault address as an argument, is the one addition ([User mode](user-mode.md)) | none | nothing |
| `UserMode::execute` | virtualized | the loop keeps its shape; the ring transition, the classification of what came back and the interrupt dispatch move into `user_run` ([User mode](user-mode.md)) | one crossing per round trip, on top of the transition | an interrupt landing in user mode is served on, and charged to, the tenant's task |
| `FsBase` | identical | a base-register write under a preemption guard, which the host honors as it honors interrupt disabling today | none | nothing |
| `GsBase` | virtualized, no crossing | on the tree `load` and `save` bracket a base-register access with two `swapgs` (checked: `ostd/src/arch/x86/cpu/context/mod.rs`), which is safe only with interrupts really disabled; a host interrupt between the two would run on the tenant's GS base. vOSTD reads and writes the `IA32_KERNEL_GS_BASE` MSR directly, which needs no `swapgs` | an MSR access instead of a base-register instruction, *estimated* at a hundred cycles | nothing |

## `io`, `bus`: devices

| item | kind | how, and what it relies on | cost | what a tenant sees |
|---|---|---|---|---|
| `IoMem::{acquire, acquire_with_cache_policy, slice, cache_policy}`, its `HasPaddr`, `HasSize` and `Drop` | virtualized, no crossing | an `IoMem` is a `(device, offset, len)` triple over a virtual device whose `mmio_base` in `BootArgs` covers the range, not a mapping; `paddr()` is the pseudo-physical address; `Drop` unmaps nothing | none | nothing |
| `IoMem::{read_fallible, write_fallible, paddr}`, its `VmIoOnce`, `VmIo` and `VmIoFill` | virtualized | every access is `mmio_read` or `mmio_write` to the endovisor's device model; a bulk access is a loop of the widest aligned words, and no driver on the tree makes one; `paddr` is the pseudo-physical base plus the offset ([Devices](devices.md)) | one crossing and one hook call per register access, per word for bulk access | nothing |
| `IoPort` and `arch::device::io_port::*` | absent | port I/O is the host's; used by host-only components and by the virtio crate's PCI transport, which is compiled out ([Devices](devices.md)) | | |
| `bus::BusProbeError` | identical | | none | nothing |

The second version of devices adds two service calls, `dev_ring_set` and `dev_notify`, and no new vOSTD type: a lent frame is owned by the tree's own `DmaStream` until its request completes; both are on [Zero-copy I/O](../zero-copy-io.md).

## `boot`, `smp`, `power`, `console`, `log`, `panic`, `util`, `prelude`

| item | kind | how, and what it relies on | cost | what a tenant sees |
|---|---|---|---|---|
| `boot::boot_info`, `BootInfo`, `EarlyCmdline`, `MemoryRegion`, `MemoryRegionType` | virtualized, no crossing | synthesized from `BootArgs`: the command line from the configuration, one `Usable` region per initial run, no ACPI, no framebuffer, no initramfs unless the endovisor attaches one as a device ([The rest](the-rest.md)) | none | `/proc/cmdline`, `/proc/meminfo` reflect the configuration |
| `boot::smp::register_ap_entry` | virtualized | spawns at once one task per virtual CPU above 0, pinned there, that runs the entry and exits | one transient task and stack per virtual CPU | nothing |
| `smp::{inter_processor_call, PendingIpis}` | absent | a kernelet cannot interrupt a CPU; no user in the kernel proper | | |
| `power::{poweroff, restart}`, `ExitCode`, and `power::exit_with_code` (new in both builds) | virtualized | `stop(STOP_EXIT, 0)` for `Success` and `stop(STOP_EXIT, 1)` for `Failure`; `exit_with_code(u32)` carries an arbitrary code, and OSTD's body is `poweroff`; the injected handlers are accepted and not called | one crossing, final | `reboot(2)` ends the kernelet with code 0 (the syscall passes `Success`; checked on the tree), with `EXIT_RESTART` set for a restart; init exiting ends it with init's status through `exit_with_code` ([The rest](the-rest.md)) |
| `power::inject_restart_handler`, `inject_poweroff_handler` | virtualized, no crossing | stored, unused | none | nothing |
| `console::{early_print, early_println}` | virtualized | `log_write` at `LEVEL_CONSOLE`; always on, where the host needs `earlycon` | one crossing per call | output goes to the endovisor's log hook |
| `console::uart_ns16650a::*` | absent | used only by the host-only `uart` component | | |
| `log` macros, `Log`, `Record`, `Level`, `LevelFilter`, `inject_logger`, `set_max_level`, `max_level` | virtualized | formatting and filtering are identical; the default sink, when no logger is injected, is `log_write`; every record that passes the filter goes to `log_write` and then to the injected `Log`; the `logger` component writes to the tenant's console devices, not the serial port, and stays | one crossing per record under the rate limit | records reach the endovisor's log hook |
| `panic::{catch_unwind, begin_panic, print_stack_trace}` | identical | the unwinder and its tables are in the image | none | nothing |
| `panic::abort` | virtualized | the `stop` service call with `STOP_PANIC` and the message the handler's entry stashed, or `"abort"` if none: ends the kernelet, never the machine | one crossing, final | a kernel panic ends the sandbox |
| `#[ostd::panic_handler]` and `__ostd_panic_handler` | virtualized, no crossing of its own | the kernel's oops handling runs in the kernelet unchanged; the expansion in both builds enters through `__handler_entry`, which in vOSTD stashes the message; `catch_unwind` reports a caught panic with `oops(msg)`, and `abort` ends with `stop(STOP_PANIC, msg)` ([The rest](the-rest.md)) | one crossing per oops | a kernel oops kills the thread, up to a budget the runtime sets |
| `#[ostd::main]` | virtualized, no crossing | the expansion in both builds ends in `__main_epilogue`, which in vOSTD ends the boot task with `task_exit` after `main` returns instead of asserting there is no current task and powering off; the boot task is pinned to virtual CPU 0, since `main` initializes the boot CPU's per-CPU state and pins its idle loop there (checked on the tree: `init.rs`) | none; the boot task's stack is returned | nothing |
| `#[ostd::global_frame_allocator]`, `#[ostd::global_heap_allocator]`, `#[ostd::global_heap_allocator_slot_map]`, `#[ostd::early_cmdline_parser]` | identical | the hooks are bound inside the kernelet image | none | nothing |
| `util::*`, `prelude::*`, `Error`, `Result` | identical | | none | nothing |

## `arch` (x86-64)

| item | kind | how, and what it relies on | cost | what a tenant sees |
|---|---|---|---|---|
| `cpu::context::*` | identical, listed under `user` above | | | |
| `trap::{TrapFrame, USER_CS_VALUE, USER_SS_VALUE}` | identical | value type and constants | none | nothing |
| `read_tsc`, `read_random`, `cpu::cpuid::cpuid` | identical | unprivileged or ring-0 instructions with local effect | none | nothing |
| `tsc_freq` | virtualized, no crossing | `BootArgs::tsc_freq_hz` | none | nothing |
| `irq::{IRQ_CHIP, IrqChip, MappedIrqLine, InterruptSourceInFdt}` | absent | the interrupt controller is the host's; the virtio MMIO bus layer that uses it gets a `cfg` line ([Devices](devices.md)) | | |
| `kernel::ACPI_INFO`, `boot::DEVICE_TREE`, `serial::SERIAL_PORT` | absent | used only by host-only components | | |

## The kernel proper's own `cfg` lines

The kernel proper's own code needs `cfg` lines in the following places. Items 1 to 4 and 6 were found by the inventory's scan of `use ostd::` imports, item 5 by the resource pages, and item 7 by a scan for the names of host-only component crates (16 files in `kernel/core/src`, counted on the tree):

1. `init.rs`: `register_ap_entry` stays (virtualized); the boot CPU's idle loop becomes a thread that waits on the init process and the per-CPU idle loops are not spawned, two lines ([Tasks](tasks.md)); the `panic!` that ends the host when init exits becomes `power::exit_with_code(status)` under the feature, so that a kernelet whose init exits ends with `Exited(status)` rather than `Panicked`.
2. `arch/x86/mod.rs`: the call to `power::init()`, which installs the ACPI-then-i8042 restart handler and needs `ACPI_INFO`, `IoPort` and `aster_i8042`, is `cfg(not(feature = "kernelet"))`, and `arch/x86/power.rs` is compiled out with it. `thread/oops.rs`: `PANIC_ON_OOPS` is initialized `false` under the feature, so that the oops path the host keeps switched off is on in a kernelet ([The rest](the-rest.md)).
3. `time/softirq.rs`, `time/cpu_time_stats.rs`, `sched/stats/scheduler_stats.rs`, `process/process/timer_manager.rs`: these use `cpu_local!` and the tick, both virtualized. The tick callbacks charge time to the *interrupted* thread through `Thread::current()` and decide user against system time through `InterruptLevel::current()` (checked on the tree); since a pending tick is consumed on the interrupted task itself, at a tick point, both are right and no line changes ([Interrupts and time](interrupts-and-time.md), register D66).
4. `thread/mod.rs`: the schedule handlers stay (virtualized); the context-switch counter's `add_on_cpu` stays over the virtualized `cpu_local!`.
5. `thread/kernel_thread.rs` (`ThreadOptions::build`), `syscall/sched_affinity.rs` and the `nice` path: one line each forwards a thread's CPU affinity and `nice` to the `TaskOptions` builders and setters vOSTD adds, so that per-CPU daemons pin correctly and a tenant's `nice` reaches the host ([Tasks](tasks.md)).
6. `comps/virtio`: in `transport/mmio/bus/arch/x86.rs` the microVM-constants probe and the `IRQ_CHIP` lookup are compiled out; in `bus/mod.rs` `try_register_mmio_device` calls `IrqLine::alloc_specific(irq)` where the host configuration maps a fresh line through `IRQ_CHIP`, and `bus/common_device.rs` stores an `IrqLine` under the alias `MappedIrqLine`; in `transport/mod.rs` the PCI transport, its `BarAccess` field, the `PortRead`/`PortWrite` bounds and the `aster-pci` dependency are compiled out (checked on the tree) ([Devices](devices.md)).
7. The files that name host-only components (`aster_framebuffer` in `device/fb.rs` and the virtual terminal, `aster_nvme` in the block registry, `aster_uart` in `tty/serial.rs`, `aster_i8042` in `driver/mod.rs`, `aster_time` in the vDSO, `system_time`, `sysinfo` and `uptime`): each reference is behind `cfg(feature = "host")`, and where the kernel proper needs a value the component supplied, the TSC frequency for the vDSO and the uptime, vOSTD reads it from `BootArgs` through `tsc_freq` and the clock page. The `logger`'s `print` and `println` stay, over `early_print`.

Every other difference between the kernel proper as host and as kernelet is a component present in one build and not the other ([Builds and images](../builds-and-images.md#two-builds)).
