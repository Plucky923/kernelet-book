# Memory

*Part of question 2. Virtualizes `mm`: frames and their metadata, the heap, `paddr_to_vaddr`, address spaces and page tables, TLB shootdown, and the DMA objects. Discharges invariants I2 (ownership) and I3 (privacy) on the kernelet side.*

A kernelet's memory is a set of **runs**: physically contiguous, 2 MiB-aligned sequences of **grains** the host has granted it, and nothing else. Everything the kernel proper allocates, every frame of every process, every page-table node, every slab, every DMA buffer, comes from a run. vOSTD addresses a frame exactly as OSTD addresses one, by adding a base to its physical address. That base is **the host's own linear map**, whose address the host writes into `BootArgs` at creation and which the instance holds in its own data ([Builds and images](../builds-and-images.md#window)); granted frames are not mapped anywhere else, and the host has no per-grain page-table work to do. Frame metadata is different: each kernelet keeps its own **metadata window** `KW_META`, sparse, holding slots only for frames it has been granted, at a base the host chooses per instance and writes into `BootArgs`. The kernelet never writes a page-table entry of that window, and the host's own frame metadata is never shared with a kernelet, which is what makes reclamation a release of whole runs with no per-frame obligation.

## What is identical, and why

On the tree, `paddr_to_vaddr` is `pa + LINEAR_MAPPING_BASE_VADDR` (checked: `ostd/src/mm/kspace/mod.rs`), and the `mapping` module places frame `pa`'s 64-byte `MetaSlot` at `FRAME_METADATA_RANGE.start + (pa / PAGE_SIZE) × 64` (checked: `ostd/src/mm/frame/meta.rs`, `META_SLOT_SIZE`). vOSTD changes the two base constants and nothing else:

```rust
// vOSTD: the same functions as the host build, over two bases the host supplies
// at creation and the instance holds in its own writable data.
static LINEAR_BASE: Vaddr = /* BootArgs::linear_map_base, the host's own */;
static KW_META: Vaddr     = /* BootArgs::meta_base, chosen per instance */;
pub fn paddr_to_vaddr(pa: Paddr) -> Vaddr { LINEAR_BASE + pa }
pub(crate) fn frame_to_meta(pa: Paddr) -> Vaddr { KW_META + (pa / PAGE_SIZE) * META_SLOT_SIZE }
pub(crate) fn meta_to_frame(va: Vaddr) -> Paddr { (va - KW_META) / META_SLOT_SIZE * PAGE_SIZE }
```

Everything built on them is the tree's code: `Frame`, `UniqueFrame`, `Segment`, `FrameRef`, the intrusive `LinkedList` over metadata, `HeapSlot::paddr` and `as_ptr`, `alloc_large`, `DynCpuLocalChunk`, and the page-table code's access to its own nodes (checked: `ostd/src/mm/page_table/node`, `cursor/locking.rs` call `mm::paddr_to_vaddr`). `get_slot` keeps its alignment and bounds checks, with `max_paddr()` the end of the highest granted run, monotone and published in the info page before the run's frames are usable; a physical address below it that was never granted meets an unmapped metadata page, which is a kernel-mode fault in kernelet code and ends the kernelet ([User mode](user-mode.md), register D22). Only vOSTD's own `unsafe` code can produce such an address; the kernel proper's safe code cannot.

## Runs and the grant table

The grant table records every run and is **written by the host, read by the kernelet**: it lives in `KW_SHARED`, appended by `create`, `grant` and `grains_request`, with its length published in the info page.

```rust
// ostd::kernelet::abi, host-written, kernelet-read. In `KW_SHARED`.
#[repr(C)] pub struct RunDesc { pub paddr: u64 /* 2 MiB-aligned */, pub grains: u32, pub _pad: u32 }
// `InfoPage::runs` is the published length of the `RunDesc` array at `BootArgs::grant_table`;
// `InfoPage::max_paddr` is the end of the highest run.
pub const GRAIN_SIZE: usize = 2 << 20;
```

## How memory arrives

For every run, at creation or later, the host does the same six things, through its linear map, before it publishes the run:

1. Allocates the run with `alloc_segment_aligned` ([control half](../kernelet-api-control.md)) and zeroes it (register D55).
2. Nothing. The grain is already addressable, through the host's linear map, which maps all of physical memory once and is shared into every address space. This step is where the earlier design mapped the grain into a per-kernelet physical window; it no longer exists.
3. Allocates and zeroes eight frames per grain for its metadata, host frames charged to the kernelet, and maps them at `KW_META + paddr / 64`, allocating the level-2 and level-1 tables the range needs for the first time (one level-1 table per 128 MiB of physical address, one level-2 per 64 GiB).
4. Writes the owner array.
5. Appends the `RunDesc` and raises `max_paddr` if the run is the highest.
6. Publishes the new length with a release store.

Nothing the kernelet sees is partial: a run it can read in the table is mapped, its metadata is mapped and zero, and its frames are zero. vOSTD then hands the run's frames to the kernel proper's frame allocator with the identical `GlobalFrameAllocator::add_free_memory` hook, one call per run, under one kernelet-side spin lock that also holds the count of runs already added, so that the two paths that learn of new runs, a `JOB_GRANT` job on a worker after the endovisor's `grant`, and the returning `grains_request` on the requesting task, never add a run twice. From then on the kernel's own allocator, the buddy allocator of `osdk/deps/frame-allocator`, manages the frames with its code unchanged, initializing each frame's metadata slot with `Frame::from_unused` as on the tree; its per-CPU pools and caches use `cpu_local!` and `LocalIrqDisabled` locks, which are virtualized per virtual CPU and to preemption guards ([Tasks](tasks.md), [Interrupts and time](interrupts-and-time.md)), so its correctness rests on the host honoring the preemption count across the allocator's slow path, which the host does.

Everything the kernelet writes here is in its own grant or in the metadata frames dedicated to it: the metadata slots, the allocator's free lists inside them, and the page-table nodes of its own address spaces, which are grant frames reached through the host's linear map. Everything the host writes is host memory, or the grant at creation through the linear map. That is invariant I2 as this page discharges it, and it needs no exception for page-table nodes and no page-table entry written by the kernelet outside its own user page tables.

The cost is 32 KiB of host memory per grain, 1.56 percent, plus one level-1 frame per 128 MiB of physical address the kernelet's grains touch, for the metadata window alone; the granted frames themselves cost no page-table work at all. A kernelet whose grains are scattered pays a few more page-table frames than one whose grains are adjacent, and the host allocator's preference for adjacent grains keeps the count small. The metadata window reserves address space too, and **how much depends on how a frame's record is found**, which is decision D86. Scaling the raw physical address, as the framework does today, makes the reservation proportional to the physical span the kernelet may be granted — one byte for every sixty-four — and on four-level paging that reservation, not memory, is then what bounds the number of kernelets a machine can hold. Finding the record through a two-level index over a coarse physical section instead, which is how Linux finds its own per-page records when it will not pay for a flat array, makes the region's size a matter of policy rather than of the machine: the bound moves from about two thousand kernelets per TiB of span to about sixty thousand (*estimated*, from the region sizes), and stops growing with the machine. The price is one extra load on `Frame::clone` and `Frame::drop`, which sit on every page fault, and it should be cache-resident. Both functions are internal to the framework, so no public interface moves. The reasoning, and the bound the flat index imposes, are in [Memory](../../linux-mode/virtualizing-ostd/memory.md). What it buys: the host's metadata array is untouched by kernelets, `Frame::clone` and `drop`, which sit on every page fault and every `mmap`, cost a shift and an add as on the tree, and destroy has nothing per frame to reset.

## Allocation, contiguity, and exhaustion

`FrameAllocOptions::alloc_frame` and `alloc_segment` are the identical call into the kernel proper's global frame allocator, which is the kernelet's own buddy allocator over its runs. When the allocator returns `None`, vOSTD asks the host before giving up:

```rust
// vOSTD, the body behind `FrameAllocOptions::alloc_*` under the feature
fn alloc_with_refill(layout: Layout) -> Option<Paddr> {
    if let Some(pa) = global_frame_allocator().alloc(layout) { return Some(pa); }
    let grains = grains_for(layout);                                   // enough contiguous grains for `layout`, or REFILL_GRAINS (2, chosen)
    if services().grains_request(grains, /* contiguous */ 1) > 0 {
        add_new_runs();                                                 // read the grant table past the count last added; add_free_memory
        return global_frame_allocator().alloc(layout);
    }
    None
}
```

`grains_request(count, contiguous)` asks for `count` grains as one run when `contiguous` is set; the host grants at once from its free memory up to `max_grains`, and at that limit consults the endovisor if the policy allows ([control half](../kernelet-api-control.md)). The call may arrive with the caller's preemption count nonzero, since a spin-lock holder may allocate, and it does not sleep; but it zeroes and maps 2 MiB per grain on the caller's task, *estimated* at 50 to 100 µs per grain, which is why `REFILL_GRAINS` is small and why that time counts against `policy.preempt_off_ticks` like any other preemption-off work. **Contiguity is what a run gives and no more**: the largest physically contiguous allocation a kernelet can make is the largest run the host can find for it, which fragmentation of the host's allocator bounds. The kernel proper does make large contiguous requests, a copy-up in the overlay file system and the block-group descriptors of `ext2` among them (checked on the tree: `kernel/core/src/fs`), so this is a tenant-visible limit: such a request fails with `ENOMEM` when the host cannot supply a run, as it would on a fragmented machine, and a heap object over the largest run the kernelet holds ends the kernelet through the allocation-error path below.

A refused request becomes `Error::NoMemory` from `FrameAllocOptions`, the error the host kernel sees when its own allocator is empty, and the kernel proper's own handling applies: `ENOMEM` to the process that asked, or the kernelet's OOM killer. The host never kills a kernelet for memory. A kernelet whose kernel cannot absorb the failure, because an infallible path, a slab refill for a `Box`, met it, reaches `#[alloc_error_handler]`, which in vOSTD calls the `stop` service with "out of memory"; that ends the kernelet as `Panicked`, and only the kernelet ([Faults, termination, and reclamation](../faults-and-reclamation.md)).

`dealloc` is identical: frames return to the kernelet's buddy allocator and never to the host. Memory only grows (register A1) and is returned in whole runs when the kernelet is destroyed. The buddy allocator's per-virtual-CPU pools can hold frames a request on another virtual CPU cannot see (the tree's own comment in `pools/alloc` admits it), so a small kernelet with many virtual CPUs may report `NoMemory` with free frames in a neighbor's pool; the floor estimate below counts it.

## The heap

The kernel proper's slab allocator, `osdk/deps/heap-allocator`, is bound through the identical `GlobalHeapAllocator` hook, and the `core::alloc` global allocator of the kernelet image is vOSTD's, which calls that hook. `HeapSlot`, `alloc_large`, and `DynCpuLocalChunk` with `DynamicCpuLocal`, which the heap allocator's per-CPU allocator uses (checked on the tree: `osdk/deps/heap-allocator/src/cpu_local_allocator.rs`), are identical: they translate through `paddr_to_vaddr`, and `get_on_cpu` indexes by `CpuId`, whose namespace is the kernelet's virtual CPUs ([Tasks](tasks.md)). The slab's per-CPU caches use `cpu_local!`, virtualized per virtual CPU. Every heap object in a kernelet therefore lives in a granted frame, reached through the host's linear map, and a `Box` in a kernelet holds a pointer that resolves in any address space on the machine, which is why nothing but the type system keeps one kernelet out of another's, and invariant I3 no longer guarantees anything against a stray pointer but the frame-metadata region.

## Address spaces and page tables

`VmSpace::new` is virtualized without a crossing: it allocates a root frame from the grant and copies the 256 kernel-half entries from `BootArgs::kernel_half_entries`, which the host filled from its own kernel page table. Those entries are the host's, shared by every address space on the machine, so every address space the kernelet creates sees the kernelet's image and its metadata window along with everything else in the kernel half. On the tree the copy is made from the host's `KERNEL_PAGE_TABLE` (checked: `ostd/src/mm/page_table/mod.rs`, `create_user_page_table`); in vOSTD the source is the array, and the code is otherwise the same.

`VmSpace::activate` is virtualized: on a space's first activation vOSTD calls `pt_root_register(root)`, and on every activation `pt_activate(root)`, which writes CR3 and records the root as the task's address space so that the host's scheduler restores it on every switch to the task. At registration the host checks that the root frame is in the grant and, as a cheap backstop against a bug in vOSTD, that its 256 kernel-half entries equal the ones it published: 256 loads, once per process. The tree's early return, which skips the CR3 write when the space is already active, is keyed by CPU on the tree (`ACTIVATED_VM_SPACE`, a per-CPU cell; checked) and **by task** in vOSTD, since CR3 is restored per task: the kernelet-side task entry holds the `Arc<VmSpace>` it last activated, as the tree's per-CPU cell holds an `Arc` (checked: `ostd/src/mm/vm_space.rs`), and `activate` compares against it. That `Arc` is also what makes dropping a `VmSpace` simple: a space reaches `Drop` only when no task holds it as its active space, so `pt_root_unregister(root)` in `Drop` always succeeds, and the host invalidates the root's translations on every CPU it was active on before returning. A task's exit path, in `run_task` before `task_exit`, calls `pt_activate(0)`, which the host reads as "no user space" and answers by installing its own kernel page table, and then drops its entry's `Arc`; an exiting thread's process page table is therefore freed as soon as its last thread has exited, as on the tree.

The cursors, `map`, `unmap`, `protect_next`, `query` and `find_next`, are the identical page-table walk: node frames from the grant, addressed through the window, leaf entries for `UFrame`s from the grant. Their `PageProperty`, `PageFlags` and `CachePolicy` are identical. `map_iomem` and `find_iomem_by_paddr` are unreachable in a kernelet: their only producer is the framebuffer device, a host-only component (checked on the tree: `kernel/core/src/device/fb.rs`), so vOSTD makes the first a no-op and the second return `None`; a virtual device has a register file, not memory ([Devices](devices.md)).

**TLB shootdown.** A local invalidation is the identical `invlpg`, a privileged instruction a kernelet may execute since it runs in kernel mode. A remote one cannot be an inter-processor interrupt, since `smp` is absent, so `TlbFlusher::dispatch_tlb_flush` is virtualized: the tree batches up to 32 page or range operations per dispatch and sends one interrupt batch for all of them (checked on the tree, `ostd/src/mm/tlb.rs`); vOSTD makes at most one crossing per dispatch, `tlb_shootdown(root, start, len)` for a batch of one operation and a flush of everything non-Global under the root, `len == u64::MAX`, for a batch of more than four (chosen), so that a fragmented `munmap` costs one crossing and one interrupt per target rather than one per operation. The host, which knows on which CPUs the root has been active since `pt_activate` recorded them, sends the interrupts and waits; the call arrives with preemption disabled, as the tree's flusher holds a preemption guard (checked: `ostd/src/mm/vm_space.rs`, `cursor_mut`), and its wait is a spin on the host's own pending-interrupt set, bounded by the kernelet's CPU set. Because the call is synchronous at dispatch, the frames the flusher keeps alive until the flush completes can be released at dispatch, which preserves the tree's `sync_tlb_flush` guarantee. Mapping a new grain needs no shootdown: it turns an absent entry present, and x86 does not cache absent translations; nothing in this design unmaps or downgrades a window mapping while a kernelet lives (register A1).

## DMA objects

`DmaStream`, `DmaCoherent` and their direction types are virtualized without a crossing: the frames come from the grant, `daddr()` returns the physical address, `sync_to_device` and `sync_from_device` do nothing, and the `is_cache_coherent` argument is ignored, since the "device" is a device model in the host that reads and writes the kernelet's frames through `Kernelet::guest_memory`, coherently, after checking that every frame of a descriptor is the kernelet's ([Devices](devices.md)). On the tree the non-coherent paths map through `KVirtArea` and `prepare_dma` consults the IOMMU (checked: `ostd/src/mm/dma`); in vOSTD those paths are not taken and `prepare_dma` and `unprepare_dma` are no-ops. A tenant that hands a device a frame it does not own gets the request failed, not a fault. There is no IOMMU on this path and no passthrough of physical devices in this design; passthrough would need an IOMMU domain per kernelet and is left for the Limitations chapter.

## What the host must undo

The memory the host holds for a kernelet, and what destroy does with each ([Faults, termination, and reclamation](../faults-and-reclamation.md)):

| what | where the host keeps it | at destroy |
|---|---|---|
| the runs | the grant table and the owner array; each run is a host `Segment` | each `Segment` dropped, which returns its frames to the host allocator one frame at a time as `Segment::drop` does on the tree; owner-array entries cleared; the host's own `MetaSlot`s for the run's frames are intact, since no kernelet ever wrote them |
| the metadata frames | host frames, eight per grain, charged | freed |
| registered roots | the root set | forgotten; the frames are in the grant |
| the metadata window's page-table frames | `meta_pt` | freed: the level-2 and level-1 tables of `KW_META`, and the entries that mapped the image |
| the data template copy, the replicas, the shared pages | host frames | freed |
| the kind's shared text frames | the image | reference count decremented; freed when no kernelet of the kind remains and the image is unregistered |

Nothing per frame is reset, because the host's frame metadata was never the kernelet's to write.

## Costs

- Per grain: 32 KiB of metadata in host memory (1.56 percent), one 2 MiB page entry, eight 4 KiB metadata entries, and the 2 MiB zeroing at grant. Per GiB of physical address touched: one level-2 frame; per 128 MiB: one level-1 frame for the metadata. Per run: one grant-table append, and, after boot, one `JOB_GRANT` wakeup or the requesting task's own `add_free_memory`.
- Per metadata access and per `paddr_to_vaddr`: a shift and an add, as on the tree.
- Per first activation of an address space: one crossing and 256 loads; per activation: one crossing and the CR3 write, which also drops the window's translations (register A2, **[unverified]** cost); `KW_TEXT` and `KW_DATA` refill from a handful of 2 MiB entries.
- Per remote TLB flush batch: one crossing and one interrupt per target CPU, bounded by the kernelet's CPU set.
- The memory floor of a kernelet is `initial_grains × 2 MiB` plus the fixed host-side items of the [control half](../kernelet-api-control.md). *Estimated* at 8 grains plus 4 per virtual CPU, from the initial slabs and the buddy allocator's per-virtual-CPU pools; to be measured early, since it decides what a small sandbox costs before it runs a process, and the pools' hoarding tuned if it dominates.
- Fragmentation of the host's allocator by aligned, contiguous requests; if a run cannot be had, the grant fails rather than falling back.

## What a tenant sees

`/proc/meminfo`'s total is the initial grant, since the kernel proper computes it once from `boot_info().memory_regions` at boot (checked on the tree: `kernel/core/src/vm/mod.rs`, `mem_total`); later grants raise the free memory the allocator reports but not the total, as a balloon does on Linux. `MemFree` sums the buddy allocator's per-CPU counters over `all_cpus()`, which is virtualized to the kernelet's virtual CPUs, so it is right only because that row of the [taxonomy](index.md) holds. `ENOMEM` and the OOM killer behave as in the host kernel, at the grant limit instead of the machine's. Physically contiguous allocations larger than the largest run the host can supply fail with `ENOMEM`; on the tree that affects overlay file-system copy-up of large files and `ext2` mount of large volumes. Everything else in the memory system is the host kernel's behavior.

## What this page decides

- **A frame's metadata record is found through a two-level index over a coarse physical section, not by scaling its physical address** (register D86). That is what keeps the region's size a matter of policy rather than of the machine, on both hosts, for one extra load on the two operations that sit on every page fault.
- **Frames are addressed through the host's linear map and metadata through a per-instance window** (register D58, revised; it supersedes D13 and D10, and was itself revised by [Design for Linux](../../linux-mode/virtualizing-ostd/memory.md)). The earlier scheme, a physical window at a fixed address inside the kernelet's own top-level entries, is superseded; D58 also supersedes D13 and the kernelet-mapped half of D10. The alternative, a dense slot space with the frame metadata at the head of each run, made `paddr_to_vaddr` and every metadata access a radix lookup on the page-fault path, made the kernelet write page-table entries the host had installed the parents of, and needed a grant table, a radix and reserved level-2 frames that this layout does not. What it gives up is a dense window: the metadata region must span the physical range the kernelet's grains may touch, which reserves address space in proportion to that span and is what bounds the number of kernelets on a machine, and scattered grains cost a few more page-table frames. Assumption A13, which bounded the old physical window instead, is withdrawn; the live bound is A22.
- **The grant table is a host-written shared page** (register D29): the kernelet learns its runs by reading, never by being told through a call it must answer.
- **Memory is granted in contiguous runs** (register D30). Single grains would cap a kernelet's largest contiguous allocation at under 2 MiB, which the kernel proper's own file systems exceed; runs move the limit to the host's fragmentation, which is where it is for any kernel.
- **Each task holds the `Arc` of the space it last activated** (register D59), which is the tree's own invariant made per task; a `VmSpace` is dropped only when no task holds it, so unregistering never fails and no pending list is needed.
- **A shootdown batch above four operations is one flush of the root** (register D60): one crossing and one interrupt per target, against up to 32 of each; the price is over-invalidation of a root's other translations on a large batch, which the tree itself accepts at 32.
