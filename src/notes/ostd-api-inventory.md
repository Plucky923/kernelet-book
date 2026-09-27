# OSTD API inventory

The public API of OSTD as read from the Asterinas tree at commit `ab9a4cfdc726263b3f41ccea0337633e24b443fc` (the tree's `HEAD` when this note was written, dated 2026-08-31), and how the kernel above OSTD uses it. This is the raw material for the design chapters's [taxonomy](../blueprint/asterinas-mode/virtualizing-ostd/index.md), which decides what each item becomes in vOSTD. Nothing here is a design decision.

**Sizes, measured on the tree** (lines of `.rs` files, `wc -l`):

| crate or directory | lines | note |
|---|---|---|
| `ostd/src` | 40,945 | 222 files; all of OSTD's `unsafe` |
| `ostd/libs` | 4,653 | `ostd-macros`, `ostd-pod`, `ostd-test`, `id-alloc`, `align_ext`, `int-to-c-enum`, `linux-bzimage`, `padding-struct` |
| `kernel/core/src` | 126,228 | the kernel proper: `#![deny(unsafe_code)]`, one `unsafe` token in a comment |
| `kernel/core/comps` | 39,341 | sixteen components: drivers and machine-wide subsystems (table below) |
| `kernel/libs` | 17,279 | kernel-side libraries |
| `osdk/deps/{frame,heap}-allocator` | 2,085 | the allocators the kernel binds into OSTD's hooks |

OSTD with its libraries is 45,598 lines; the kernel above it is 182,848. Both figures are the ones the Overview rounds to 45,600 and 183,000.

**How usage was measured.** Every `use ostd::…` statement in `kernel/` was expanded into its leaf paths (nested braces resolved, `as` renames dropped), and the leaves counted: 173 distinct items, 1,182 imports, in 424 files. Inline paths such as `ostd::mm::VmIo` in expressions were counted separately (533 occurrences) and agree in ranking. The column "kernel imports" below is the leaf count; it measures how many files import an item, not how many call sites use it, so it is a proxy for how widely the item is depended on. Items with no import line are used through the prelude, through a macro, or not at all.

## Entry, hooks, and macros

These are the points where OSTD calls the kernel rather than the reverse. Each is an attribute macro from `ostd-macros` or an `inject_*` function, and each is bound exactly once in the kernel.

| item | bound at | what OSTD does with it |
|---|---|---|
| `#[ostd::main]` | `kernel/src/lib.rs` | emits `__ostd_main`, which calls the kernel's `main`, then `Task::yield_now()`, asserts no current task, and calls `power::poweroff(Success)` |
| `#[ostd::global_frame_allocator]` | `kernel/core/src/vm/mod.rs` | the `GlobalFrameAllocator` OSTD's `FrameAllocOptions` calls: `alloc(Layout) -> Option<Paddr>`, `dealloc(Paddr, usize)`, `add_free_memory(Paddr, usize)` |
| `#[ostd::global_heap_allocator]` | same | the `GlobalHeapAllocator` behind `alloc`: `alloc(Layout) -> Result<HeapSlot, AllocError>`, `dealloc(HeapSlot)` |
| `#[ostd::global_heap_allocator_slot_map]` | same | `const fn(Layout) -> Option<SlotInfo>`, the slab-size map |
| `#[ostd::panic_handler]` | `kernel/core/src/thread/oops.rs` | the function `__ostd_panic_handler` calls |
| `#[ostd::early_cmdline_parser]` | `kernel/core/comps/cmdline` | parses the early command line before logging is up |
| `task::scheduler::inject_scheduler` | `kernel/core/src/sched/sched_class` | the `&'static dyn Scheduler<Task>` OSTD dispatches through |
| `task::inject_pre_schedule_handler`, `inject_post_schedule_handler` | `kernel/core/src/thread/mod.rs` | `fn(&DisabledLocalIrqGuard)` before and `fn()` after every context switch; the kernel activates the incoming task's `VmSpace` in the post handler |
| `mm::fault::inject_user_page_fault_handler` | `kernel/core/src/vm` | `fn(&CpuException) -> Result<(), ()>` for page faults taken in kernel mode on user addresses |
| `log::inject_logger` | `kernel/core/comps/logger` | the `&'static dyn Log` sink |
| `power::inject_restart_handler`, `inject_poweroff_handler` | `kernel/core/src/arch/x86/power.rs` | `fn(ExitCode)` |
| `boot::smp::register_ap_entry` | `kernel/core/src/init.rs` | `fn()` run on each application processor after boot |
| `timer::register_callback_on_cpu` | `kernel/core/src/time` | a `Fn()` run on every tick, on every CPU |
| `irq::register_bottom_half_handler_l1`, `_l2` | `kernel/core/comps/softirq` | handlers run on the interrupt return path |

Macros the kernel uses, by name: the log macros `emerg`, `alert`, `crit`, `error`, `warn`, `notice`, `info`, `debug`, `log_enabled`, `log` (all expand to `__write_log_record` calls and read a per-crate `__log_prefix!`); `early_print`, `early_println`; `cpu_local!`, `cpu_local_cell!` (expand to `unsafe` constructors of `StaticCpuLocal` and `CpuLocalCell`); `impl_frame_meta_for!`, `impl_untyped_frame_meta_for!` (expand to `unsafe impl AnyFrameMeta`); `const_assert!`, `ptr_null_of!`; `ktest`. The two `cpu_local` macros are used in five kernel files: `time/clocks/system_wide.rs`, `fs/fs_impls/procfs/cpuinfo.rs`, and three files of the `softirq` component.

## `prelude`

Re-exports `Result<T, E = Error>`, `ktest`, the log macros, `early_print as print`, `early_println as println`, `panic::abort`, and `mm::{HasPaddr, HasSize, Paddr, Vaddr}`. Imported by wildcard in 7 files and as `prelude::ktest` in 23. `Error` (an enum of exactly `InvalidArgs`, `NoMemory`, `PageFault`, `AccessDenied`, `IoError`, `NotEnoughResources` and `Overflow`) has 11 imports and `Result` 11.

## `mm`

| item | kernel imports | shape |
|---|---|---|
| `VmIo`, `VmIoOnce`, `VmIoFill` | 100, 12, 2 | traits: typed reads and writes on anything that has a reader and writer |
| `VmReader`, `VmWriter` | 49, 46 | cursors over memory, `Fallible` (user space, faults through the exception table) or `Infallible` (kernel space); constructors `from_kernel_space`, `from_user_space` are `unsafe` |
| `Infallible`, `Fallible`, `FallibleVmRead`, `FallibleVmWrite`, `PodOnce`, `PodAtomic` | 33, 3, 5, 4, 4, 1 | markers and traits for the above |
| `io::util::HasVmReaderWriter`, `VmReaderWriterResult`, `VmReaderWriterIdentity` | 27, 3, 0 | the trait a type implements to get `VmIo` for free |
| `PAGE_SIZE`, `Vaddr`, `Paddr`, `Daddr`, `PagingLevel` | 18, 5, 1, 3, 0 | constants and type aliases |
| `HasPaddr`, `HasSize`, `HasDaddr`, `HasPaddrRange`, `Split` | 8, 12, 13, 0, 1 | traits over memory objects |
| `FrameAllocOptions` | 12 | `alloc_frame`, `alloc_frame_with(meta)`, `alloc_segment`, `alloc_segment_with`; goes through the global frame allocator hook |
| `Frame<M>`, `UFrame`, `Segment<M>`, `USegment`, `UniqueFrame<M>` | 3, 3, 7, 4, 0 | reference-counted frames and contiguous runs, typed by metadata; `from_unused(paddr, meta)`, `from_in_use(paddr)`, `meta()`, `reference_count()`, `reset_as_unused()` |
| `frame::meta::AnyFrameMeta`, `AnyUFrameMeta`, `GetFrameError`, `FRAME_METADATA_MAX_SIZE` | 0 (via macros) | the per-frame metadata protocol; one `MetaSlot` per physical frame in a global array |
| `frame::linked_list::LinkedList<M>`, `Link<M>`, `CursorMut` | 0 (used by the OSDK frame allocator) | intrusive lists of `UniqueFrame` |
| `frame::allocator::GlobalFrameAllocator` | 0 (hook) | see hooks |
| `heap::GlobalHeapAllocator`, `HeapSlot`, `SlotInfo`, `slab::{Slab, SlabMeta}`, `slot_list::SlabSlotList` | hook, 0, 1, 0, 0 | the heap protocol: OSTD's `alloc` calls the hook with a `Layout`; `HeapSlot` addresses slots through the linear map (`paddr()`, `as_ptr()`); `HeapSlot::alloc_large` backs allocations larger than a slab slot with a `Segment` |
| `VmSpace` | 4 | a user address space: `new`, `cursor`, `cursor_mut`, `activate`, `reader`, `writer`; `cursor_mut().map(UFrame, PageProperty)`, `map_iomem`, `unmap`, `protect_next`, `flusher()` |
| `vm_space::{VmQueriedItem, CursorMut}` | 4, 1 | the cursor's item type and the mutable cursor |
| `PageProperty`, `PageFlags`, `CachePolicy` | 2, 5, 3 | mapping attributes |
| `tlb::TlbFlushOp`, `TlbFlusher` | 3, 0 | shootdown requests; `TlbFlusher` issues inter-processor calls |
| `MAX_USERSPACE_VADDR`, `KERNEL_VADDR_RANGE` | 2, 1 | address-space bounds |
| `dma::{DmaStream, DmaCoherent, DmaDirection, ToDevice, FromDevice, FromAndToDevice}` | 11, 10, 4, 7, 8, 1 | DMA-mapped memory: `alloc`, `map(USegment)`, `sync_from_device`, `sync_to_device`; `daddr()` |
| `fault::{UserPageFaultHandler, inject_user_page_fault_handler}` | hook | see hooks |
| `kspace::{KVirtArea, paddr_to_vaddr, kernel_loaded_offset, LINEAR_MAPPING_*, VMALLOC_*}` | 0 | kernel-half virtual memory; not imported by the kernel |
| `page_table::{PageTable, Cursor, PageTableError}` | 0 | the raw page table; the kernel uses it only through `VmSpace` |

## `sync`

| item | kernel imports | shape |
|---|---|---|
| `SpinLock<T, G>`, `SpinLockGuard` | 52, 13 | spin lock with a guardian `G`: `PreemptDisabled` (default), `LocalIrqDisabled` via `.disable_irq()` |
| `RwLock<T, G>` and its three guards | 9, 3 read, 4 write, 0 upgradeable | reader-writer spin lock, same guardians |
| `SpinGuardian`, `PreemptDisabled`, `LocalIrqDisabled`, `WriteIrqDisabled`, `GuardTransfer` | 3, 8, 25, 2, 1 | the guardian protocol |
| `Mutex`, `MutexGuard` | 14, 4 | sleeping lock over a `WaitQueue` |
| `RwMutex` and its guards | 5, 6 read, 6 write, 1 upgradeable | sleeping reader-writer lock |
| `WaitQueue`, `Waiter`, `Waker` | 22, 19, 9 | `wait_until(cond)`, `wake_one`, `wake_all`; `Waiter::new_pair()`, `wait()`, `wait_until_or_cancelled`; `Waiter::task() -> &Arc<Task>`; a waker holds the sleeper's `Arc<Task>` |
| `Rcu`, `RcuOption`, `RcuReadGuard`, `RcuOptionReadGuard`, `RcuDrop`, `non_null::{NonNullPtr, ArcRef, BoxRef}` | 2, 4, 0, 1, 0, 5, 1, 0 | read-copy-update: `read()`, `update()`, `compare_exchange()`; `RcuDrop<T>` defers the drop of an old value to the end of a grace period, tracked in a per-CPU list |
| `RwArc`, `RoArc` | 5, 1 | an `Arc` with an internal `RwLock` |

## `task`

| item | kernel imports | shape |
|---|---|---|
| `Task` | 38 | `current() -> Option<CurrentTask>`, `need_yield`, `yield_now`, `run(self: &Arc<Self>)`, `data()`, `schedule_info()`; owns a 512 KiB kernel stack, a saved register context, a `data: Box<dyn Any>` and a `local_data`, and a `TaskScheduleInfo` |
| `TaskOptions` | 3 | builder: `new(func)`, `data`, `local_data`, `build`, `spawn -> Arc<Task>` |
| `CurrentTask` | 3 | a non-`Send` handle to the running task; `local_data()`, `cloned()` |
| `disable_preempt`, `DisabledPreemptGuard` | 14, 4 | per-CPU preemption count |
| `halt_cpu` | 0 | the idle instruction |
| `scheduler::{Scheduler, LocalRunQueue, EnqueueFlags, UpdateFlags, inject_scheduler, enable_preemption_on_cpu, info::{CommonSchedInfo, TaskScheduleInfo}}` | 1, 1, 5, 5, 1, 1, 1, 0 | the scheduler the kernel injects: `enqueue(Arc<Task>, flags) -> Option<CpuId>`, `local_rq_with`, `mut_local_rq_with`; a run queue's `current`, `update_current`, `pick_next`, `dequeue_current` |
| `AtomicCpuId` | 1 | the CPU a task is on |
| `atomic_mode::{InAtomicMode, AsAtomicModeGuard, might_sleep}` | 4, 4, 0 | the guard protocol for code that must not sleep |
| `inject_pre_schedule_handler`, `inject_post_schedule_handler` | hooks | see hooks |

## `irq`

| item | kernel imports | shape |
|---|---|---|
| `IrqLine` | 10 | `alloc()`, `alloc_specific(u8)`, `num()`, `on_active(callback)`, `is_empty()`, `remapping_index()`; the callback type is `IrqCallbackFunction = dyn Fn(&TrapFrame) + Sync + Send` |
| `IrqCallbackFunction` | 5 | see above |
| `disable_local`, `DisabledLocalIrqGuard` | 3, 5 | clears the interrupt flag for the guard's lifetime |
| `InterruptLevel` | 4 | `current()`, `is_task_context()`, `is_interrupt_context()` |
| `register_bottom_half_handler_l1`, `_l2` | hooks | see hooks |

## `cpu`

| item | kernel imports | shape |
|---|---|---|
| `CpuId`, `CpuSet`, `AtomicCpuSet`, `num_cpus`, `all_cpus`, `PinCurrentCpu` | 18, 10, 1, 5, 3, 8 | the CPU namespace; `PinCurrentCpu` is an `unsafe` trait implemented by the preemption and interrupt guards |
| `cpu_local!`, `cpu_local_cell!`, `local::{StaticCpuLocal, CpuLocalCell, DynamicCpuLocal, DynCpuLocalChunk}` | 4, 2, 1, 0, 0, 0 | per-CPU statics in a replicated `.cpu_local` section, addressed through the GS segment base |
| `PrivilegeLevel` | 2 | kernel mode or 3 |
| `UserContext` (re-exported from `arch`) | see `arch` | |

## `user`

| item | kernel imports | shape |
|---|---|---|
| `UserMode` | 1 | `new(UserContext)`, `execute<T: UserModeHooks>(&mut self, &T) -> ReturnReason`, `context()`, `context_mut()`: the ring-3 round trip |
| `UserContextApi` | 12 | the trait over `UserContext`: get and set the instruction pointer and the stack pointer; syscall arguments are read from `GeneralRegs` |
| `UserModeHooks` | 1 | `has_kernel_event()`, `pre_user_run(&DisabledLocalIrqGuard)` |
| `ReturnReason` | 1 | `UserSyscall`, `UserException`, `KernelEvent` |

## `timer`

| item | kernel imports | shape |
|---|---|---|
| `Jiffies` | 9 | `elapsed()`, `as_u64()`, `as_duration()`; a global tick counter |
| `TIMER_FREQ` | 3 | 1000 Hz |
| `register_callback_on_cpu` | hook | see hooks |

## `boot`, `smp`, `power`, `console`, `log`, `bus`, `panic`, `util`

| item | kernel imports | shape |
|---|---|---|
| `boot::boot_info`, `BootInfo`, `EarlyCmdline`, `memory_region::{MemoryRegion, MemoryRegionType}` | 4, 0, 1, 1 | bootloader name, kernel command line, initramfs, ACPI and framebuffer arguments, memory regions |
| `boot::smp::register_ap_entry` | hook | see hooks |
| `smp::inter_processor_call`, `PendingIpis` | 0 (used by OSTD's own `TlbFlusher`) | run a `fn()` on a set of CPUs |
| `power::{restart, poweroff, ExitCode, inject_*_handler}` | 1, 1, 3, hooks | machine reset and power-off |
| `console::{early_print, early_println, uart_ns16650a::*}` | macros, 2, 2, 1 | the early console and a 16550 UART driver |
| `log::{Log, Record, Level, LevelFilter, inject_logger, set_max_level, max_level}` | 0, 1, 1, 1, hook, 0, 0 | the logging sink protocol |
| `bus::BusProbeError` | 8 | the error type bus drivers return |
| `panic::{abort, catch_unwind, begin_panic, print_stack_trace}` | via prelude, 0, 0, 0 | `abort` halts the machine; `catch_unwind` is re-exported from the `unwinding` crate |
| `util::{Either, id_set::{Id, IdSet, AtomicIdSet}, range_alloc::RangeAllocator, ops::range_difference}` | 3, 5, 0, 0, 0, 0 | utilities without machine effect |

## `arch` (x86-64)

The `arch` module is public and the kernel reaches into it directly; on x86-64 the items used are:

| item | kernel imports | shape |
|---|---|---|
| `cpu::context::{UserContext, GeneralRegs, FsBase, GsBase, FpuContext, CpuException, CpuExceptionInfo, RawPageFaultInfo, PageFaultErrorCode}` | 24, 3, 7, 7, 5, 5, 1, 1, 2 | the saved user register file and the exception it took; `FpuContext::{save, load, as_bytes}`; `FsBase`/`GsBase::{save, load}` touch MSRs |
| `trap::{TrapFrame, USER_CS_VALUE, USER_SS_VALUE}` | 10, 1, 1 | the interrupt frame and the user segment selectors |
| `irq::{IRQ_CHIP, IrqChip, MappedIrqLine, InterruptSourceInFdt}` | 6, 0, 6, 2 | the interrupt controller and its lines; used by the `pci`, `uart`, `i8042` and `virtio` (MMIO bus) components |
| `device::io_port::{ReadWriteAccess, WriteOnlyAccess, PortRead, PortWrite}` | 3, 3, 2, 2 | port I/O access types; used by the `uart`, `i8042`, `time` (CMOS) components |
| `kernel::ACPI_INFO` | 4 | ACPI tables; used by the `pci` and `time` components |
| `boot::DEVICE_TREE` | 8 | the device tree (RISC-V and LoongArch); used by the MMIO bus and `uart` |
| `serial::SERIAL_PORT` | 2 | the early serial port |
| `read_tsc`, `tsc_freq`, `read_random`, `cpu::cpuid::cpuid` | 4, 3, 2, 1 | timestamp counter, its frequency, `rdrand`, `cpuid` |

## `io`

| item | kernel imports | shape |
|---|---|---|
| `IoMem` | 24 | `acquire(Range<Paddr>)`, `slice`, `read_fallible`, `write_fallible`, `cache_policy`; implements `VmIo` through `HasVmReaderWriter`; a mapped device-memory range |
| `IoPort<T, A>` | 4 | `acquire(port)`, `read`, `write`; port I/O |

## Where the machine-level items are used

The kernel imports that touch the machine directly (`IoPort`, `IRQ_CHIP`, `MappedIrqLine`, `ACPI_INFO`, `SERIAL_PORT`, `DEVICE_TREE`, `register_ap_entry`, `inter_processor_call`, `KVirtArea`, `paddr_to_vaddr`, `halt_cpu`) are confined to 21 files: the `pci`, `uart`, `i8042`, `time`, `logger` and `virtio` (MMIO transport's bus layer) components, and in `kernel/core/src` the files `init.rs`, `thread/mod.rs`, `arch/x86/power.rs`, `time/softirq.rs`, `time/cpu_time_stats.rs`, `sched/stats/scheduler_stats.rs` and `process/process/timer_manager.rs`. The sixteen components and their sizes:

| component | lines | machine-facing |
|---|---|---|
| `mlsdisk` | 13,617 | no |
| `virtio` | 8,339 | the MMIO and PCI transports; the device drivers are not |
| `nvme` | 2,500 | yes |
| `pci` | 2,063 | yes |
| `block` | 2,023 | no |
| `systree` | 1,727 | no |
| `framebuffer` | 1,608 | yes |
| `i8042` | 1,541 | yes |
| `cmdline` | 1,531 | no |
| `input` | 1,256 | no |
| `time` | 909 | yes (TSC, CMOS, ACPI) |
| `network` | 829 | no |
| `softirq` | 820 | uses `cpu_local!` and the bottom-half hooks |
| `uart` | 342 | yes |
| `logger` | 162 | *not host-only*: writes to the console devices and falls back to `early_print`; stays in the kernelet ([The rest](../blueprint/asterinas-mode/virtualizing-ostd/the-rest.md)) |
| `console` | 74 | no |
