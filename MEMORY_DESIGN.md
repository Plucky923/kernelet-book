# Tenant memory on a Linux host, without a model

*A working design document, outside the book. It proposes replacing the Linux chapter's model-and-cache design for tenant memory with a `VmSpace` implemented directly over Linux's own page tables, states the contract that makes that sound, lists what must be verified, and records each review and prototype iteration. When it is done its content moves into the book and this file is retired. Written 2026-10-08 on branch `linux-native-vmspace`, against `main` at `070311f`.*

## Status

| step | state |
|---|---|
| 1. Draft the design (this file) | **done**, iteration 1 |
| 2. Verify the host properties (§4.1) in Linux v6.12 | **done**, folded in at iteration 2; one finding against the current prototype (§8) |
| 3. Audit the kernel proper's use of `VmSpace` (§4.2) | **done**, folded in at iteration 2 |
| 4. Review the design (reviewer: Linux maintainer, implementer, security skeptic) | running, on iteration 2 |
| 5. Fix and re-review until no blocking issue remains | not started |
| 6. Prototype (§7) in `kernelet-in-linux-poc` | running, from iteration 2; review findings are forwarded to it |
| 7. Fold the prototype's findings back; final review | not started |
| 8. Update the book (§6), `make check` and `make build`, render the Memory page | not started |

---

## 0. The decision in one paragraph

On the Linux host, vOSTD's `VmSpace` is implemented over the tenant process's **Linux address space**: Linux's page table is the only page table, written through Linux's own functions by the endovisor, in service calls, and read by vOSTD through the direct map. There is no model, no cache, no fill-on-fault and no coherence protocol. What makes this sound is a contract the Linux chapter has so far left implicit: **a kernelet's atomic mode governs the kernelet's own scheduler and its virtual interrupts, and nothing else**; Linux may pause the carrier anywhere, so a service may sleep in Linux inside kernelet atomic mode, provided the caller holds no kernelet spin lock. The design keeps the kernel proper unmodified, keeps one Linux address space per tenant process, keeps the adoption at `user_run`, and changes the service table, the endovisor's memory code, vOSTD's `mm` module, and one line of the patch.

## 1. Why

The model-and-cache design ([Memory](src/blueprint/linux-mode/virtualizing-ostd/memory.md#cache), register D95) was chosen so that vOSTD could keep OSTD's page-table code verbatim. Its page-table cost is paid in faults rather than in writes, and that is the wrong currency for a workload that forks, maps and exits constantly:

- **Every page the kernel proper maps without a tenant fault** costs one Linux fault at first touch, estimated at about a microsecond, **[unverified]**, that a container does not pay. The kernel proper maps ahead more than it seems: its page-fault handler maps up to sixteen pages around the faulting one for ELF segments (*fault-around*, `vm_mapping.rs:641-700` at `ab9a4cfdc`), `fork` maps every page of the parent into the child, and `mremap` remaps page by page. Under the cache each of those pages faults again in Linux before the tenant can use it, so fault-around, which exists to save faults, saves none here. (`MAP_POPULATE` and `mlock` are not the cases: the kernel proper ignores the first and lacks the second.)
- **A write-protect is an eviction.** `protect` becomes a `tlb_shootdown`, which removes the cached translations with `unmap_mapping_range()`, so after a `fork` or an `mprotect` even *reads* fault again to refill. Native Linux narrows the entry in place and only writes fault. For fork-heavy work, shells and build systems and the agents this book is for, this is the amplification that matters.
- **A demand-paging fault takes a detour** through Linux's fault path, the resume hook, the kernel proper's handler and a refill.
- Two tables are kept coherent by a per-model reader-writer semaphore and a rule that vOSTD frees nothing until the flush returns (D111), with a prototype that never raced them.

Steady state, once a page is cached, is already native: a one-level page table, no second-level walk, no exit. What the model costs is first touch and every protection change. The owner's requirement is that kernelets be as efficient as containers and far more efficient than virtual machines; a design that pays a fault per mapped page and a refault per protected page is neither.

The objection recorded against writing Linux's page tables, that a vOSTD bug could map any frame, does not stand: vOSTD is in the trusted computing base on both hosts, and the endovisor still checks every frame against the grant in the map service, so the Linux chapter's stronger form of invariant I2 survives unchanged.

## 2. The contract: kernelet atomic mode is not Linux atomic mode

A kernelet cannot mask hardware interrupts, only virtual ones; its tasks run on virtual CPUs that Linux schedules; the time it reads is the host's. So its atomic mode (a `disable_preempt()` guard, a spin lock, an RCU read guard, all of which are `AsAtomicModeGuard` on the tree) can promise only this: **on this virtual CPU, no kernelet task switch happens and no virtual interrupt is delivered until the guard drops.** It never promised that the carrier keeps the processor: Linux takes it at any instruction, by an interrupt the kernelet cannot see, for a whole timeslice, and the kernelet is already correct under that, as a guest is correct when its hypervisor deschedules a vCPU inside a critical section.

A service that sleeps in Linux while the kernelet is in atomic mode is therefore the same event as that preemption, taken voluntarily. On the Linux host kernelet code always runs in Linux *task* context (the upcall stub runs at the trap return, on the task's stack), so the only thing that stops a service from sleeping is the design's own choice to mirror the kernelet's guard count into Linux's preemption count (D119). The sleeping services already lift the mirror in their prologue and restore it in their epilogue ([Who is calling](src/blueprint/linux-mode/kernelet-api-service.md#depth)). This design makes that the rule rather than the exception:

- **R1.** Any service may sleep in Linux. Its prologue lifts the mirror; its epilogue restores it.
- **R2.** A service that may sleep must not be called while the caller holds a kernelet **spin lock** (or an interrupts-off guard, which on the Linux host is the same thing: `irq_off` plus a guard). Under a plain preempt guard, an RCU read guard, or the range lock of §3.4, whose waiters sleep in Linux rather than spin, it may. The reason is lock-holder preemption, not correctness: a spin lock held across a Linux sleep makes every other virtual CPU that wants it spin for the sleep's length, which under memory pressure is milliseconds. vOSTD asserts R2 in debug builds: it keeps a per-virtual-CPU count of spin-lock guards separately from the guard count that the shared record publishes, and a sleeping service's stub panics the kernelet if it is nonzero. The audit of §4.2 found no kernel-proper spin lock live across any `VmSpace` operation, so R2 holds for this design's services by inspection and the assertion guards the future.
- **R3.** What a sleep costs the kernelet is stated, not hidden: this virtual CPU's pending virtual interrupts and the kernelet's RCU grace periods wait for the carrier to return, and its clock jumps by the sleep, as a descheduled vCPU's does. The pending tick count already absorbs the backlog.
- **R4.** The mirror stays, as a latency optimization for the non-sleeping path: it keeps Linux from preempting a carrier inside a short critical section, at the measured bound of about 2 ms. It is no longer load-bearing for correctness anywhere, and the prototype's "mirror with and without" comparison is what should decide whether it is kept.

The rule the Linux chapter states today, that an upcall handler may call only non-sleeping services, becomes R2: an upcall handler runs with the interrupted task's guards in place, and if those include a spin lock, it may not call a sleeping service. The service page's `-STATE` refusal of a sleeping call "under a nonzero preemption count" becomes a refusal under a nonzero *spin-lock* count.

## 3. The design

### 3.1 One Linux address space per `VmSpace`

Unchanged from today in shape, new in meaning: when the kernel proper calls `VmSpace::new()`, vOSTD calls the service `vm_create()`, and the endovisor creates an empty Linux address space with `mm_alloc()`, a file object for it, and the one PFN-mapped area over the whole user range, as it does for a model today. The area's flags are `VM_PFNMAP | VM_SHARED | VM_IO | VM_DONTEXPAND | VM_DONTDUMP | VM_DONTCOPY`, its operations supply `fault` and `pfn_mkwrite`, and deliberately neither `map_pages` nor `access` (the second is the fallback through which `ptrace` and `/proc/pid/mem` could otherwise reach the area once `get_user_pages` has refused it). The file behind the area has an **inode of its own**, from `anon_inode_create_getfile()`: the plain `anon_inode_getfile()` the prototype uses today hands every caller the one shared anonymous inode, so every space's area would sit in one `i_mmap` tree and a range operation on that file would reach all of them (§8, finding). The difference from today is that nothing else exists: the address space's page table **is** the tenant process's page table. The endovisor returns a space identifier and publishes, in the per-space record, the physical address of the address space's root (`mm->pgd`) so that vOSTD can read the table.

The endovisor keeps the user reference it got from `mm_alloc()` until `vm_destroy`. That reference is what pins the page tables for the lockless reads of §3.4: Linux frees a space's page tables only in `exit_mmap()`, which runs when the last user reference goes, and a carrier is an ordinary task that the OOM killer or a root `SIGKILL` can end at any time, dropping only its own reference. With the endovisor's reference held, `exit_mmap()` cannot run before `vm_destroy`, whoever dies.

The area is created at `vm_create`, not at first adoption, so that `vm_map` has somewhere to insert before any carrier runs the process. Creating it needs the address space's lock and may sleep, which R1 allows; `VmSpace::new()` is called from task context in the kernel proper under no spin lock (§4.2 confirms the sites).

A carrier adopts a space's Linux address space at `user_run` with `kernelet_switch_mm()`, as today ([Adopting an address space](src/blueprint/linux-mode/virtualizing-ostd/user-mode.md#adopt)); `vm_activate(space)` records the virtual CPU's current space, as `pt_activate` does today. One Linux address space per tenant process, shared by whichever virtual CPUs run its threads (D120), is kept.

### 3.2 Each operation of `VmSpace`

The semantics on the right of each row are the tree's, as the audit of §4.2 states them; the Linux-native implementation must keep every one.

| the kernel proper calls | vOSTD does | the endovisor does, in Linux | sleeps? |
|---|---|---|---|
| `VmSpace::new()` | `vm_create()` | `mm_alloc()`, the file, `vm_mmap()` of the area over the user range; records the space under the kernelet | yes |
| `cursor(..)` / `cursor_mut(..)` | takes the space's **range lock** (§3.4) for the cursor's lifetime, shared for a read cursor and exclusive for a mutable one; uncontended, a compare-and-swap in kernelet memory | nothing, unless the lock is contended: then `vm_wait` sleeps the carrier until `vm_wake` | only when contended |
| `cursor_mut(..).map(frame, prop)` | checks the range; `Frame::into_raw`; `vm_map(space, va, paddr, prot)`, or one `vm_map_batch` for a run; the cursor moves on | checks `paddr` against the grant (owner array) and `va` against the area; builds the protection from `prot`'s three bits as the fault handler does today, never from the kernelet's bits; `vmf_insert_pfn_prot()` | yes, when a page-table page must be allocated (`GFP_PGTABLE_USER` includes `GFP_KERNEL`, checked v6.12) |
| `cursor_mut(..).unmap(len) -> usize` | reads the leaves of the range through the direct map (lockless, §3.4), remembers their physical addresses; `vm_unmap(space, va, len)`; queues the remembered frames on the flusher, exactly as the tree does, so that they are released only after the flush; returns the count | `apply_to_existing_page_range()` with a callback that clears each present entry under the PTE lock; returns the count cleared; **does not flush** | no (the PTE lock only) |
| `cursor_mut(..).protect_next(len, op) -> Option<Range>` | finds the next mapped run by the lockless walk, applies `op` to its flags (the kernel proper never changes the cache policy, audit), `vm_protect(space, va, len, prot)` for the run, returns the range; does not flush, as the tree does not | the same walk, rewriting each present entry in place with the new protection; **does not flush** | no |
| `flusher().issue_tlb_flush(op)` then `dispatch_tlb_flush()` | the tree's code: queues the ops and the frames; dispatch calls `vm_flush(space, start, len)` once per op (or once with `len = UINT64_MAX` for `for_all`), then releases the queued frames, as the tree does after its own shootdown | `flush_tlb_mm_range()` over the range, which reaches every processor in the address space's CPU mask | no |
| `cursor(..).query() -> VmQueriedItem` | reads the one entry at the cursor by the lockless walk; `MappedRam { frame, prop }` with `frame` a `FrameRef` borrowed from the frame metadata for the cursor's lifetime, as on the tree, and `prop` rebuilt from the entry's bits; `find_next` and `jump` are the same walk | nothing | no |
| `VmSpace::reader/writer` (copies) | the same lockless walk per page, then the copy through the direct map, as the model walk does today; `Err` unless this space is the virtual CPU's active one, as on the tree | nothing | no |
| `VmSpace::activate` | `vm_activate(space)` | records the virtual CPU's current space; adoption at `user_run`. **Must not sleep**: the kernel proper calls it from its post-schedule handler | no |
| `map_iomem`, `find_iomem_by_paddr` | refused in the first version: a tenant cannot map device memory on this host, which the Devices page already says | — | — |
| `drop(VmSpace)` | an `unmap` of the whole range and a `for_all` flush, which releases every frame; then `vm_destroy(space)` | drops the endovisor's reference on the address space; a carrier that still has it adopted drops its own later, as today | yes |
| a page fault in user mode | nothing until `user_run` returns an exception | `fault` and `pfn_mkwrite` record the address and error code in the carrier record, flag the carrier, return `VM_FAULT_NOPAGE`; the kernel proper's handler runs as a task and maps, which inserts; `user_run` returns to user mode and the instruction is retried, with no second fault and **no walk in `user_run`** | — |
| a fault Linux takes on its own account (kernel-mode access to tenant memory) | — | `VM_FAULT_SIGBUS`, so Linux's own exception table turns it into `-EFAULT`, exactly as the model-miss case today | — |

`prot` crosses as three bits (readable for user mode, writable, executable) plus the cache policy, and the endovisor builds Linux's `pgprot_t` from its own constants; a kernelet never supplies raw page-table bits, which is what kept the global bit out of tenant mappings in the current design (D113) and still does.

### 3.3 The service table

Removed: `pt_root_register`, `pt_root_unregister`, `tlb_shootdown`. Renamed: `pt_activate` becomes `vm_activate(space)`. Added:

```c
int64_t (*vm_create)(void);                                     /* >= 0: space id; publishes the root's paddr in the space record */
int64_t (*vm_destroy)(uint64_t space);
int64_t (*vm_activate)(uint64_t space);                         /* 0 means "no tenant address space"; never sleeps */
int64_t (*vm_map)(uint64_t space, uint64_t va, uint64_t paddr, uint32_t prot);
int64_t (*vm_map_batch)(uint64_t space, uint64_t va, const uint64_t *paddrs, uint32_t n, uint32_t prot);  /* paddrs read with a fallible copy */
int64_t (*vm_unmap)(uint64_t space, uint64_t va, uint64_t len);  /* >= 0: entries cleared; not flushed */
int64_t (*vm_protect)(uint64_t space, uint64_t va, uint64_t len, uint32_t prot);  /* not flushed */
int64_t (*vm_flush)(uint64_t space, uint64_t va, uint64_t len);   /* len == UINT64_MAX: everything; synchronous; never sleeps */
int64_t (*vm_wait)(uint64_t space, uint32_t word, uint64_t expected);  /* the range lock's slow path: sleep while *word == expected; -KLET_CANCEL on a kill */
int64_t (*vm_wake)(uint64_t space, uint32_t word);
```

Errors: `-KLET_NOT_OWNED` for a frame outside the grant, `-KLET_INVALID` for an address outside the area or a space that is not this kernelet's, `-KLET_LIMIT` at the configured maximum of spaces. `vm_map` over an entry that is already present is a kernelet bug, as it is on the tree, where the cursor's lock makes it impossible; the endovisor reports it as `-KLET_INVALID` and vOSTD panics the kernelet, because the range lock of §3.4 should have excluded it. `vm_map_batch` is the one new pointer argument: read-only, read with Linux's fallible copy, used only during the call, as the three read-only pointers of the service half are today.

Which may sleep in Linux: `vm_create`, `vm_destroy`, `vm_map`, `vm_map_batch`, `vm_wait`, and `grains_request` as today; `vm_unmap` and `vm_protect` may yield between page tables on a long range, as Linux's own `zap_pte_range()` does, so they are on this list too. Which never do, and so may be called from an upcall handler: `vm_activate`, `vm_flush`, `vm_wake`. `flush_tlb_mm_range()` is called by Linux itself under page-table spin locks, so `vm_flush` is a non-sleeping service, which matters because the kernel proper dispatches flushes while it still holds the cursor.

`grains_request` is unchanged. The owner array is unchanged: it is what `vm_map` checks.

### 3.4 Concurrency and ownership

- **The cursor's range lock is load-bearing, and it stays.** On the tree a cursor spins on page-table node locks for its whole life, and the kernel proper relies on that in two places the audit found: its page-fault handler queries, allocates and maps under one cursor, so two threads faulting on one page never both map it; and a page-cache page is committed and mapped atomically under the cursor, while eviction and truncation unmap through a cursor of their own (`vm_mapping.rs:478-559`, `vmo/mod.rs:330-332,396-398`, `rmap.rs:101,143` at `ab9a4cfdc`). Dropping the lock and reporting a second map as an error would break the second case. So vOSTD keeps a range lock per space, held for the cursor's lifetime, shared for `Cursor` and exclusive for `CursorMut`, striped by 2 MiB region over a small array of lock words **in kernelet memory**. Uncontended, taking it is one compare-and-swap and no crossing. Contended, the waiter calls `vm_wait(space, word, expected)` and its carrier **sleeps in Linux** until the holder's release calls `vm_wake`; this is a futex whose waiters are carriers. The holder may itself be asleep in Linux inside `vm_map`; the waiter then sleeps too, and nothing spins, which is why R2 exempts this lock. The host holds nothing across kernelet code: the lock is the kernelet's, a dying kernelet's waiters are woken with `-KLET_CANCEL`, and invariant I7's "nothing of Linux's at depth zero" is untouched. Fork holds two cursors at once, the parent's shared and the child's exclusive, always in that order, so two forks of one parent serialize on the parent and cannot deadlock.
- **Within a region, entries are serialized by Linux's PTE lock** inside `vmf_insert_pfn_prot()` and the `apply_to_*` callbacks, and `vm_destroy` waits for the space's lock words to be free. The kernel proper's own locks (the VMAR's `inner` lock, the rmap lock) serialize overlapping operations above this level, as they do on the tree; §4.2 records which is held where.
- **Frame ownership.** On the tree the page table holds a counted reference on every mapped frame; the kernel proper relies on the count, since its copy-on-write path reuses a frame in place when `reference_count() == 1` (`vm_mapping.rs:509`). Here the *Linux entry* holds that reference: `Frame::into_raw` at map, `Frame::from_raw(paddr)` when `unmap` reads the entry back, and a borrowed `FrameRef` from `query`, exactly the tree's arithmetic, so the count a frame shows is the same as on the tree. This is sound if Linux never clears an entry of the area on its own initiative (property P1, §4.1). A violation would leak a reference until the sandbox is destroyed, which reclaims by the grant, so it degrades a tenant's page cache rather than breaking anything.
- **Lockless reads.** vOSTD reads Linux's entries through the direct map without the PTE lock, at the cost the model walk has today (3.6 to 13 ns, measured in a model). That is safe because the area's page-table pages are freed only in `exit_mmap()`, which the endovisor's reference holds off until `vm_destroy` (property P2 and §3.1), and because entries are written with single 64-bit stores and read with single loads on a host without `PARAVIRT_XXL` (property P3); a torn or stale read is then impossible, and a concurrent modification of the same range is excluded by the range lock. Nothing here relies on interrupts being off during the read, which Linux's own lockless walkers do: on this host freed page tables are RCU-deferred only under `PARAVIRT`, so the reference, not the interrupt state, is what the design leans on.
- **Across kernelets.** Nothing is shared: each space belongs to one kernelet, checked on every service.

### 3.5 TLB flushing

The tree's structure is kept exactly: `unmap` and `protect_next` change entries and flush nothing; the kernel proper issues flush operations on the `TlbFlusher` and dispatches them, and OSTD releases unmapped frames only after the dispatch. In vOSTD the dispatch becomes `vm_flush(space, va, len)`, one call per operation, and `flush_tlb_mm_range()` does the shootdown on every processor in the address space's CPU mask, which Linux keeps current as carriers adopt and leave the space. `fork` therefore keeps its single `for_all` flush after protecting the whole parent, instead of one per run, and the window between a cleared entry and its flush is the same window the tree has, closed the same way: the frame is not released until the flush has returned.

`flush_tlb_mm_range()` is **not exported** in v6.12 (checked, `arch/x86/mm/tlb.c`). The patch gains one export; the alternative, `unmap_mapping_range()`, flushes but also evicts, which is the cost §1 removes. The ledger in [What it asks of Linux](src/blueprint/linux-mode/endovisor.md#patch) grows from five exports to six.

### 3.6 Large pages

Deferred, recorded as the extension: the tree's cursor can map a 2 MiB page, grains are 2 MiB-aligned, and `vmf_insert_pfn_pmd()` exists for exactly this, which would make such a page one insert and one entry. The first version maps 4 KiB only, as today, and `vm_map` refuses a large-page `prot`.

### 3.7 What the endovisor must undo

As today, minus the cache: a space is destroyed when its `VmSpace` drops, which the kernel proper does at process exit, in task context; a carrier that dies drops its adopted address space's reference; at sandbox destroy, carriers go first, then every remaining space's address space, then the grant. The area's page-table pages are Linux's, charged to the sandbox's control group through `__GFP_ACCOUNT`, and go back with the address space.

## 4. What the design rests on

### 4.1 Three properties of Linux v6.12, verified and to be recorded as host properties

Verified by a reviewer agent against the v6.12 tree under `kernelet-in-linux-poc/kernelet-linux/.build/linux-6.12`; `L/` below is that tree. Each becomes a stated host property on the Memory page, in the form the SMAP property has ([Reaching tenant memory](src/blueprint/linux-mode/virtualizing-ostd/memory.md#copies)), with an assumption number in the register.

- **P1 holds, with three conditions the design now states.** Linux never clears or modifies an entry of the area on its own initiative. An entry inserted by `insert_pfn()` is `pte_special` and counts in no mapping count (`L/mm/memory.c:2355`), and `vm_normal_page()` returns nothing for a `VM_PFNMAP` area (`memory.c:603`), so reclaim and rmap never reach it; the multi-generational LRU skips `VM_SPECIAL` areas outright (`vmscan.c:3244,4073`). Compaction isolates only movable or LRU pages (`compaction.c:1082,1129`). KSM excludes `VM_PFNMAP|VM_IO|VM_DONTEXPAND` (`ksm.c:683`). NUMA balancing's `vma_migratable()` refuses `VM_IO|VM_PFNMAP` (`mempolicy.c:1769`). khugepaged honors `VM_NO_KHUGEPAGED` (`huge_memory.c:124`). The OOM reaper skips `VM_PFNMAP` (`oom_kill.c:525`). `process_madvise` offers only COLD, PAGEOUT, WILLNEED and COLLAPSE, and each refuses the area (`madvise.c:577,588,621,1211-1217`; `khugepaged.c:2720`); DONTNEED and FREE forbid `VM_PFNMAP` (`madvise.c:856`). `mprotect`, `mremap` and `munmap` act on `current->mm` only. `get_user_pages()` refuses `VM_IO|VM_PFNMAP` (`gup.c:1268`), so `ptrace` and `/proc/pid/mem` fall back to `vm_ops->access`, which the file does not define. The conditions: the frames are the grant's, never on an LRU or in a page cache; the area has `VM_DONTCOPY`, since `dup_mmap()` would otherwise copy a PFN-mapped area's entries (`memory.c:1340`, `fork.c:667,746`); and each space's file has its own inode (§3.1).
- **P2 holds.** v6.12 has no page-table reclaim (`mm/pt_reclaim.c` does not exist). `zap_pte_range()` clears entries and frees no table (`memory.c:1552,1691`); `free_pgtables()` is reached only from `exit_mmap()` (`mmap.c:1934`), `unmap_region()` (`vma.c:356`) and `vms_clear_ptes()` (`vma.c:1100`), the last also for a `MAP_FIXED` overmap, which the endovisor performs once, at area creation, before any entry exists. A carrier is not a kernel thread (`fork.c:2261-2263`), so it can be OOM-killed, killed by root, or traced, and `exit_mmap()` then runs in whichever task drops the last reference; §3.1's reference is what keeps that after `vm_destroy`.
- **P3 holds on a host without `PARAVIRT_XXL`.** `native_set_pte()` is a `WRITE_ONCE` and the clear an `xchg` (`arch/x86/include/asm/pgtable_64.h:67,94`); `ptep_get()` is a `READ_ONCE` (`include/linux/pgtable.h:317`). Under `PARAVIRT_XXL` (Xen PV) the store becomes a paravirt operation, and the design does not cover that host.

Also checked: no lockdep assertion sits on the `vmf_insert_pfn_prot()` path (`memory.c:2403-2427,2316-2366`); the real requirement is exclusion from `free_pgtables()`, which §3.1 provides. For RAM, `track_pfn_insert()` imposes write-back (`pat/memtype.c:1065,662-666`), which is what the kernel proper asks for. `zap_vma_ptes()` is exported too and would do for `unmap` but may sleep in `cond_resched()` (`memory.c:1742`); the design uses `apply_to_existing_page_range()` so that it can read the entry it clears. On x86 `pte_alloc_one()` uses `GFP_PGTABLE_USER | PGTABLE_HIGHMEM` plus a `GFP_KERNEL` lock allocation (`arch/x86/mm/pgtable.c:29-31`, `memory.c:6918`), which is why `vm_map` sleeps, and `__GFP_ACCOUNT` charges the carrier's control group, which is the sandbox's.

### 4.2 The kernel proper's use of `VmSpace`, audited at `ab9a4cfdc`

Audited by a reviewer agent over `kernel/core/src` (`k/` below); the design above was corrected against it at iteration 2.

**The surface.** `VmSpace::new` (one per Vmar, `k/vm/vmar/vmar_impls/mod.rs:64`); `activate` (from the post-schedule handler, `k/thread/mod.rs:57`, and at exec with preemption disabled: it must not sleep); `reader`/`writer`/`reader_writer` (`Err` unless the space is the one active on the CPU, `ostd/src/mm/vm_space.rs:160`); `cursor`/`cursor_mut` with `G: AsAtomicModeGuard`; `query` returning the one item at the cursor, `MappedRam { frame: FrameRef, prop }` or `MappedIoMem`, the `FrameRef` borrowed from the page table for the cursor's lifetime and cloned by callers that keep it (`k/vm/vmar/fork.rs:100`, `remap.rs:241`); `find_next`, `jump` (backwards too, `vm_mapping.rs:527-529`), `virt_addr`; `map(UFrame, PageProperty)`, which takes one counted reference; `map_iomem`, `find_iomem_by_paddr`; `unmap(len) -> usize`, a count used for RSS, the frames being dropped by OSTD after the flush and an RCU grace period (`vm_space.rs:459,487-495`); `protect_next(len, FnMut(&mut PageFlags, &mut CachePolicy)) -> Option<Range>`, which never flushes and whose closures never touch the cache policy; `flusher()` with `issue_tlb_flush`, `dispatch_tlb_flush`, `sync_tlb_flush` (which panics with interrupts disabled) and `TlbFlushOp::{for_range, for_all}`; `inject_user_page_fault_handler`. RAM is always write-back; flags are compared only as read, write, execute; accessed and dirty bits are written and never read; no large pages; `PrivilegedPageFlags` unused by the kernel proper.

**What it relies on.** The page table holds a counted reference per mapping, and the copy-on-write path reuses a frame when `reference_count() == 1` (`vm_mapping.rs:509`). The cursor's lock: the fault handler queries, allocates and maps under one cursor (`vm_mapping.rs:478-559`); a page-cache page is committed and mapped atomically under it (`k/vm/page_cache/vmo/mod.rs:330-332,396-398`). Fork holds two mutable cursors on two spaces under one guard (`fork.rs:46,48`). Every site holds the cursor across `dispatch_tlb_flush`.

**Locks live at the cursor sites.** Every site passes its own `disable_preempt()` guard. Beyond it: the Vmar's `inner` `RwMutex` (a sleeping lock) read for faults and write for map, unmap, protect and remap; the rmap `Mutex` for protect, rmap and fork; the heap `Mutex` for fork; sleeping page-cache locks for flush and evict; the page cache's XArray spin lock during the commit callback, **released before `map`**. **No site holds a kernel-proper spin lock across `map`, `unmap` or `protect`; none runs with interrupts disabled or in interrupt context.** The futex code drops its bucket lock before it faults. So R2 holds for every operation of §3.2 by inspection.

**Eager operations**, which the cache pays an extra fault for: fault-around, up to sixteen pages per fault for ELF segments (`vm_mapping.rs:641-700`, enabled at `k/process/program_loader/elf/load_elf.rs:427`); `fork`, a `protect_next` per run of the parent and a `map` per page into the child, then one `for_all` flush (`fork.rs:61,92-106`); `mremap`, an `unmap` and `map` per page; device mmap through `map_iomem`; the forced software faults of `access_alien`, the exec `.bss` tail and futexes. Not eager: `MAP_POPULATE` and `MAP_LOCKED` are accepted and ignored, `MADV_POPULATE_*` is `EINVAL`, there is no `mlock`, exec text is faulted in lazily.

### 4.3 Facts already checked

- `vmf_insert_pfn_prot()` is `EXPORT_SYMBOL`; `apply_to_page_range()`, `apply_to_existing_page_range()` and `zap_vma_ptes()` are `EXPORT_SYMBOL_GPL` (v6.12, `mm/memory.c`). `apply_to_existing_page_range()` calls the callback only for present entries, under the PTE lock, and allocates nothing.
- `insert_pfn()` takes only the PTE lock through `get_locked_pte()` and leaves an existing entry alone (v6.12, `mm/memory.c`). `get_locked_pte()` allocates a missing page-table page with `GFP_PGTABLE_USER = GFP_KERNEL | __GFP_ZERO | __GFP_ACCOUNT` (v6.12, `include/asm-generic/pgalloc.h`), which may sleep.
- `flush_tlb_mm_range(mm, start, end, stride_shift, freed_tables)` is declared in `arch/x86/include/asm/tlbflush.h` and not exported from `arch/x86/mm/tlb.c` (v6.12).
- OSTD's cursors take an `InAtomicMode` guard (`ostd/src/mm/page_table/cursor/mod.rs:106`, `vm_space.rs:112` at `ab9a4cfdc`); spin-lock, RwLock and RCU guards are all `AsAtomicModeGuard` (`ostd/src/sync/spin.rs:140`, `rwlock.rs:284`, `rcu/mod.rs:382`), which is why R2 needs its own count rather than the guard count.
- The kernel proper's `VmSpace` cursor sites in `vm/vmar/vm_mapping.rs` and `vm/vmar/rmap.rs` pass a `disable_preempt()` guard (at `ab9a4cfdc`; the full list is §4.2's).

## 5. Costs

All *estimated* until §7 measures them.

- **Per cursor**: one compare-and-swap to take the range lock and one to release it; a crossing only when contended.
- **Per page mapped**: one crossing (the stack switch measured at 18 cycles, the prologue and epilogue around 25), one owner-array load, the PTE lock and store that native Linux also pays, and Linux's `track_pfn_insert()`. Batched, the crossing is per run.
- **Per first touch of a mapped page**: nothing beyond the hardware. This is the container's cost.
- **Per demand-paged page**: one Linux fault, the gate's resume hook, the kernel proper's handler, one `vm_map` (sixteen, or one batch, with fault-around). One hardware fault as native, plus the detour, which this design does not remove.
- **Per `protect` of a run**: one crossing, one PTE rewrite per present entry; the flush is the kernel proper's, batched as on the tree. No refault.
- **Per `unmap` of a range**: one lockless walk by vOSTD, one crossing, one clear per entry; the flush as above.
- **Per flush dispatched**: one crossing and one `flush_tlb_mm_range()`, an inter-processor interrupt per processor in the space's CPU mask, which is at most the sandbox's virtual CPUs that have adopted the space.
- **Per copy to or from tenant memory**: the lockless walk, as the model walk today: about 10 ns on a small copy, nothing on a large one.
- **Per space**: one Linux address space and its page-table pages, charged to the sandbox; no model nodes in the grant.
- **Removed**: the fill-on-fault fault per eagerly mapped page, the refault after every protect, the per-model fill/flush semaphore, the walk in `user_run`, the page-table nodes of the model.

## 6. What changes in the book

- `src/blueprint/linux-mode/virtualizing-ostd/memory.md`: "Linux's page tables are a cache of the kernelet's" and "Filling against flushing" are replaced by the design above; both figures go (the six-step figure is replaced by a four-step one: fault, record, handle, map); "Reaching tenant memory" keeps the walk and its table, over Linux's table; the costs and decisions sections are rewritten; the three host properties are stated.
- `src/blueprint/linux-mode/kernelet-api-service.md`: the table (§3.3), the sleeping rule (R1, R2), "Which calls can block inside Linux".
- `src/blueprint/linux-mode/virtualizing-ostd/scheduling.md`: the contract of §2 beside the mirror; the upcall-handler rule becomes R2.
- `src/blueprint/linux-mode/virtualizing-ostd/user-mode.md`: `user_run` no longer walks; adoption unchanged.
- `src/blueprint/linux-mode/endovisor.md`: six exports.
- `src/blueprint/linux-mode/alternatives.md`: the model-and-cache design moves here, with §1's reasons, next to the side table it replaced.
- `src/blueprint/linux-mode/principles.md`: I2's Linux form reworded (the check is in `vm_map`); I3 unchanged.
- `src/blueprint/linux-mode/prototype.md`: the new phase's measurements.
- `src/notes/design-register.md`: D95 superseded; D111 retired; D113 kept (the protection is built by the endovisor); D119 revised (R1, R4); D120 kept; D82 kept (the walk is over Linux's table); D108 unchanged; new D125 (Linux-native `VmSpace`), D126 (the atomic-mode contract and R2), D127 (synchronous flush, `flush_tlb_mm_range` export), D128 (frame reference held by the Linux entry); new assumptions A35, A36, A37 for P1, P2, P3.
- `src/blueprint/overview/api-virtualization.md` and `terminology.md`: no change needed; the Asterinas chapter: no change.

## 7. What the prototype must show

In `kernelet-in-linux-poc/kernelet-linux`, where vOSTD is a hand-written crate with its own `mm/page_table.rs` (the model) and `mm/vm_space.rs`, and the endovisor's `vmem.c` holds the models, the fault handler and `kernelet_switch_mm()`.

| experiment | pass criterion |
|---|---|
| **E1, functional.** Replace the model with `vm_*` services and the lockless walk; `make hello`, `make probe` (demand paging, `ud2`, eviction) and `make sched` still pass | the serial logs show the same results as `REPORT.md` |
| **E2, no fill faults.** A kernel maps 2 MiB eagerly, then the tenant touches every page; and a fault-around test: one tenant fault maps sixteen pages, the tenant then touches all sixteen | zero Linux page faults in the area after the maps; one Linux fault for the sixteen pages, where the model-and-cache design takes seventeen (count them in the fault handler) |
| **E3, protect in place.** Map, touch, `protect` to read-only, read every page again, then write one | zero faults on the reads; exactly one `pfn_mkwrite` report on the write |
| **E4, sleeping under a preempt guard.** `map` into a fresh 2 MiB region, forcing a page-table-page allocation, from inside a kernelet `disable_preempt()` section with the mirror lifted, on a guest with `CONFIG_DEBUG_ATOMIC_SLEEP` | no "sleeping function called from invalid context", the mapping is correct, the kernelet's own scheduler did not switch during the section |
| **E5, R2's assertion.** The same under a kernelet spin lock | the kernelet is killed by vOSTD's assertion, not Linux |
| **E6, cost.** First touch of a mapped page, old against new; `vm_map` per page, single and batched; `protect` of 2 MiB then 512 reads, old against new; a copy through the lockless walk against the model walk | numbers, five boots, medians and p99, on the same guest as `bench/RESULTS.md` |
| **E7, P1 under pressure.** With tenant pages mapped, a host process allocates until reclaim runs, then the tenant reads every page | every entry still present (a counter in the fault handler), contents intact |
| **E8, concurrency.** Two virtual CPUs in one process: one maps and unmaps a range in a loop, the other reads through the walk and copies; then both open a mutable cursor on the same region, one of them sleeping in `vm_map` | no torn read, no stale frame handed back (the frame's owner check in `from_raw`); the second cursor waits in `vm_wait` and proceeds after the release, with no spinning and no kernelet task switch on either virtual CPU |
| **E9, batching.** `fork` of a process with 64 MiB mapped, old against new; `munmap` of the same, old against new | the child's maps go as batches, one `for_all` flush; numbers as E6 |

Every deviation from this document is a finding, reported with what it changes.

## 8. Review log

*Iteration 1*: draft written; verification and audit launched.

*Iteration 2, finding against the current design as prototyped*: the prototype's `endovisor/vmem.c:196` creates a model's file with `anon_inode_getfile()`, which shares one inode among every caller, so its `unmap_mapping_range()` flush (`vmem.c:549`) on one model's file reaches every model's window at the same addresses, and every other anonymous-inode mapping in the guest with an overlapping offset. With one model per test it went unnoticed. The Memory page's "one file per model" needs a private inode (`anon_inode_create_getfile()`), whichever design is kept; this design needs it for the area, though it no longer flushes through the file.

*Iteration 2*: the audit of §4.2 corrected four premises. `unmap` returns a count and OSTD releases the frames after the flush, so the design now keeps the tree's flusher and adds `vm_flush` instead of flushing inside `vm_unmap` and `vm_protect`, which also keeps fork's single flush. `query` reads one slot and lends a `FrameRef`. The cursor's lock is load-bearing for the page cache, so the design gains a range lock in kernelet memory with a futex-like slow path in Linux (`vm_wait`, `vm_wake`), and `vm_map` over an existing entry becomes a kernelet bug rather than an error to handle. Fault-around and fork, not populate and `mlock`, are the eager cases that motivate §1. `activate` must not sleep. `map_iomem` is refused.

## 9. Open questions

- Whether the mirror (D119) earns its keep once services may sleep (R4): to be decided by measurement, not here.
- The range lock's striping (how many words per space, and whether a 2 MiB region is the right grain for fault-around's sixteen pages) is a tuning question for the prototype.
- Large pages (§3.6): when, and whether `prot`'s cache policy needs more than write-back; the kernel proper never asks for anything else today.
- Whether `vm_destroy` should be allowed while a carrier still has the space adopted (today's rule: the carrier drops its reference later), or `VmSpace::drop` should first force the virtual CPU off it.
