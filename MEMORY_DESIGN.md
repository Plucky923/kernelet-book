# Tenant memory on a Linux host, without a model

*A working design document, outside the book. It proposes replacing the Linux chapter's model-and-cache design for tenant memory with a `VmSpace` implemented directly over Linux's own page tables, states the contract that makes that sound, lists what must be verified, and records each review and prototype iteration. When it is done its content moves into the book and this file is retired. Written 2026-10-08 on branch `linux-native-vmspace`, against `main` at `070311f`.*

## Status

| step | state |
|---|---|
| 1. Draft the design (this file) | **done**, iteration 1 |
| 2. Verify the host properties (§4.1) in Linux v6.12 | running |
| 3. Audit the kernel proper's use of `VmSpace` (§4.2) | running |
| 4. Review the design (reviewer: Linux maintainer, implementer, security skeptic) | not started |
| 5. Fix and re-review until no blocking issue remains | not started |
| 6. Prototype (§7) in `kernelet-in-linux-poc` | not started |
| 7. Fold the prototype's findings back; final review | not started |
| 8. Update the book (§6), `make check` and `make build`, render the Memory page | not started |

---

## 0. The decision in one paragraph

On the Linux host, vOSTD's `VmSpace` is implemented over the tenant process's **Linux address space**: Linux's page table is the only page table, written through Linux's own functions by the endovisor, in service calls, and read by vOSTD through the direct map. There is no model, no cache, no fill-on-fault and no coherence protocol. What makes this sound is a contract the Linux chapter has so far left implicit: **a kernelet's atomic mode governs the kernelet's own scheduler and its virtual interrupts, and nothing else**; Linux may pause the carrier anywhere, so a service may sleep in Linux inside kernelet atomic mode, provided the caller holds no kernelet spin lock. The design keeps the kernel proper unmodified, keeps one Linux address space per tenant process, keeps the adoption at `user_run`, and changes the service table, the endovisor's memory code, vOSTD's `mm` module, and one line of the patch.

## 1. Why

The model-and-cache design ([Memory](src/blueprint/linux-mode/virtualizing-ostd/memory.md#cache), register D95) was chosen so that vOSTD could keep OSTD's page-table code verbatim. Its page-table cost is paid in faults rather than in writes, and that is the wrong currency for a workload that forks, maps and exits constantly:

- **Every page the kernel proper maps without a tenant fault** (populate, `mlock`, anything mapped ahead) costs one Linux fault at first touch, estimated at about a microsecond, **[unverified]**, that a container does not pay.
- **A write-protect is an eviction.** `protect` becomes a `tlb_shootdown`, which removes the cached translations with `unmap_mapping_range()`, so after a `fork` or an `mprotect` even *reads* fault again to refill. Native Linux narrows the entry in place and only writes fault. For fork-heavy work, shells and build systems and the agents this book is for, this is the amplification that matters.
- **A demand-paging fault takes a detour** through Linux's fault path, the resume hook, the kernel proper's handler and a refill.
- Two tables are kept coherent by a per-model reader-writer semaphore and a rule that vOSTD frees nothing until the flush returns (D111), with a prototype that never raced them.

Steady state, once a page is cached, is already native: a one-level page table, no second-level walk, no exit. What the model costs is first touch and every protection change. The owner's requirement is that kernelets be as efficient as containers and far more efficient than virtual machines; a design that pays a fault per mapped page and a refault per protected page is neither.

The objection recorded against writing Linux's page tables, that a vOSTD bug could map any frame, does not stand: vOSTD is in the trusted computing base on both hosts, and the endovisor still checks every frame against the grant in the map service, so the Linux chapter's stronger form of invariant I2 survives unchanged.

## 2. The contract: kernelet atomic mode is not Linux atomic mode

A kernelet cannot mask hardware interrupts, only virtual ones; its tasks run on virtual CPUs that Linux schedules; the time it reads is the host's. So its atomic mode (a `disable_preempt()` guard, a spin lock, an RCU read guard, all of which are `AsAtomicModeGuard` on the tree) can promise only this: **on this virtual CPU, no kernelet task switch happens and no virtual interrupt is delivered until the guard drops.** It never promised that the carrier keeps the processor: Linux takes it at any instruction, by an interrupt the kernelet cannot see, for a whole timeslice, and the kernelet is already correct under that, as a guest is correct when its hypervisor deschedules a vCPU inside a critical section.

A service that sleeps in Linux while the kernelet is in atomic mode is therefore the same event as that preemption, taken voluntarily. On the Linux host kernelet code always runs in Linux *task* context (the upcall stub runs at the trap return, on the task's stack), so the only thing that stops a service from sleeping is the design's own choice to mirror the kernelet's guard count into Linux's preemption count (D119). The sleeping services already lift the mirror in their prologue and restore it in their epilogue ([Who is calling](src/blueprint/linux-mode/kernelet-api-service.md#depth)). This design makes that the rule rather than the exception:

- **R1.** Any service may sleep in Linux. Its prologue lifts the mirror; its epilogue restores it.
- **R2.** A service that may sleep must not be called while the caller holds a kernelet **spin lock** (or an interrupts-off guard, which on the Linux host is the same thing: `irq_off` plus a guard). Under a plain preempt guard it may. The reason is lock-holder preemption, not correctness: a spin lock held across a Linux sleep makes every other virtual CPU that wants it spin for the sleep's length, which under memory pressure is milliseconds. vOSTD asserts R2 in debug builds: it keeps a per-virtual-CPU count of spin-lock guards separately from the guard count that the shared record publishes, and a sleeping service's stub panics the kernelet if it is nonzero.
- **R3.** What a sleep costs the kernelet is stated, not hidden: this virtual CPU's pending virtual interrupts and the kernelet's RCU grace periods wait for the carrier to return, and its clock jumps by the sleep, as a descheduled vCPU's does. The pending tick count already absorbs the backlog.
- **R4.** The mirror stays, as a latency optimization for the non-sleeping path: it keeps Linux from preempting a carrier inside a short critical section, at the measured bound of about 2 ms. It is no longer load-bearing for correctness anywhere, and the prototype's "mirror with and without" comparison is what should decide whether it is kept.

The rule the Linux chapter states today, that an upcall handler may call only non-sleeping services, becomes R2: an upcall handler runs with the interrupted task's guards in place, and if those include a spin lock, it may not call a sleeping service. The service page's `-STATE` refusal of a sleeping call "under a nonzero preemption count" becomes a refusal under a nonzero *spin-lock* count.

## 3. The design

### 3.1 One Linux address space per `VmSpace`

Unchanged from today in shape, new in meaning: when the kernel proper calls `VmSpace::new()`, vOSTD calls the service `vm_create()`, and the endovisor creates an empty Linux address space with `mm_alloc()`, a file object for it, and the one PFN-mapped area over the whole user range, as it does for a model today. The area's flags are `VM_PFNMAP | VM_SHARED | VM_IO | VM_DONTEXPAND | VM_DONTDUMP | VM_DONTCOPY`, its operations supply `fault` and `pfn_mkwrite` and deliberately no `map_pages`. The difference is that nothing else exists: the address space's page table **is** the tenant process's page table. The endovisor returns a space identifier and publishes, in the per-space record, the physical address of the address space's root (`mm->pgd`) so that vOSTD can read the table.

The area is created at `vm_create`, not at first adoption, so that `vm_map` has somewhere to insert before any carrier runs the process. Creating it needs the address space's lock and may sleep, which R1 allows; `VmSpace::new()` is called from task context in the kernel proper under no spin lock (§4.2 confirms the sites).

A carrier adopts a space's Linux address space at `user_run` with `kernelet_switch_mm()`, as today ([Adopting an address space](src/blueprint/linux-mode/virtualizing-ostd/user-mode.md#adopt)); `vm_activate(space)` records the virtual CPU's current space, as `pt_activate` does today. One Linux address space per tenant process, shared by whichever virtual CPUs run its threads (D120), is kept.

### 3.2 Each operation of `VmSpace`

| the kernel proper calls | vOSTD does | the endovisor does, in Linux | sleeps? |
|---|---|---|---|
| `VmSpace::new()` | `vm_create()` | `mm_alloc()`, the file, `vm_mmap()` of the area over the user range; records the space under the kernelet | yes |
| `cursor_mut(..).map(frame, prop)` | checks the range; `Frame::into_raw`; `vm_map(space, va, paddr, prot)`, or one `vm_map_batch` for a run | checks `paddr` against the grant (owner array) and `va` against the area; builds the protection from `prot`'s three bits as the fault handler does today, never from the kernelet's bits; `vmf_insert_pfn_prot()` | yes, when a page-table page must be allocated (`GFP_PGTABLE_USER` includes `GFP_KERNEL`, checked v6.12) |
| `cursor_mut(..).unmap(len)` | reads the leaves of the range through the direct map (lockless, §3.5), remembers their frames; `vm_unmap(space, va, len)`; then `Frame::from_raw` each remembered frame and hands them back as the tree's `unmap` does | `apply_to_existing_page_range()` with a callback that clears each present entry under the PTE lock; `flush_tlb_mm_range()` over the range; returns the count cleared | may (the per-space lock) |
| `cursor_mut(..).protect_next(len, op)` | `vm_protect(space, va, len, prot)` | the same walk, rewriting each present entry in place with the new protection, then `flush_tlb_mm_range()`; no eviction | may |
| `cursor(..).query()` / `find_next` / `jump` | a lockless walk of Linux's table through the direct map, reading `pte_special` entries as `(paddr, prot)` and reconstructing the `Frame` handle from the frame metadata | nothing | no |
| `VmSpace::reader/writer` (copies) | the same lockless walk per page, then the copy through the direct map, as the model walk does today | nothing | no |
| `flusher().dispatch_tlb_flush()` | nothing: the flush happened inside `vm_unmap` or `vm_protect`, which return only after it | nothing | no |
| `VmSpace::activate` | `vm_activate(space)` | records the virtual CPU's current space; adoption at `user_run` | no |
| `drop(VmSpace)` | an `unmap` of the whole range, which returns every frame; then `vm_destroy(space)` | empties nothing (already empty), drops the endovisor's reference on the address space; a carrier that still has it adopted drops its own later, as today | yes |
| a page fault in user mode | nothing until `user_run` returns an exception | `fault` and `pfn_mkwrite` record the address and error code in the carrier record, flag the carrier, return `VM_FAULT_NOPAGE`; the kernel proper's handler runs as a task and maps, which inserts; `user_run` returns to user mode and the instruction is retried, with no second fault and **no walk in `user_run`** | — |
| a fault Linux takes on its own account (kernel-mode access to tenant memory) | — | `VM_FAULT_SIGBUS`, so Linux's own exception table turns it into `-EFAULT`, exactly as the model-miss case today | — |

`prot` crosses as three bits (readable for user mode, writable, executable) plus the cache policy, and the endovisor builds Linux's `pgprot_t` from its own constants; a kernelet never supplies raw page-table bits, which is what kept the global bit out of tenant mappings in the current design (D113) and still does.

### 3.3 The service table

Removed: `pt_root_register`, `pt_root_unregister`, `tlb_shootdown`. Renamed: `pt_activate` becomes `vm_activate(space)`. Added:

```c
int64_t (*vm_create)(void);                                     /* >= 0: space id; publishes the root's paddr in the space record */
int64_t (*vm_destroy)(uint64_t space);
int64_t (*vm_activate)(uint64_t space);                         /* 0 means "no tenant address space" */
int64_t (*vm_map)(uint64_t space, uint64_t va, uint64_t paddr, uint32_t prot);
int64_t (*vm_map_batch)(uint64_t space, uint64_t va, const uint64_t *paddrs, uint32_t n, uint32_t prot);  /* paddrs read with a fallible copy */
int64_t (*vm_unmap)(uint64_t space, uint64_t va, uint64_t len);  /* >= 0: entries cleared; flushed on return */
int64_t (*vm_protect)(uint64_t space, uint64_t va, uint64_t len, uint32_t prot);
```

Errors: `-KLET_NOT_OWNED` for a frame outside the grant, `-KLET_INVALID` for an address outside the area or a space that is not this kernelet's, `-KLET_LIMIT` at the configured maximum of spaces, `-KLET_EXISTS` from `vm_map` when an entry is already present (Linux's `insert_pfn` leaves an existing entry alone, so the endovisor checks first under the PTE lock and reports it; vOSTD then returns the frame to the caller, matching the tree's contract for a map over an existing mapping, which §4.2 states precisely). `vm_map_batch` is the one new pointer argument: read-only, read with Linux's fallible copy, used only during the call, as the three read-only pointers of the service half are today.

`grains_request` is unchanged. The owner array is unchanged: it is what `vm_map` checks.

### 3.4 Concurrency and ownership

- **Within a space.** Individual entries are serialized by Linux's PTE lock inside `vmf_insert_pfn_prot()` and the `apply_to_*` callbacks. Range operations (`vm_unmap`, `vm_protect`, `vm_destroy`) take a per-space Linux reader-writer semaphore exclusively; `vm_map` takes it shared. This replaces, Linux-side and therefore sleepable, the sub-tree lock the tree's cursor holds for its lifetime. The kernel proper's own locks (the VMAR's) serialize overlapping operations above this level, as they do on the tree; §4.2 records which.
- **Frame ownership.** On the tree the page table holds a reference on every tracked frame and `unmap` hands it back. Here the *Linux entry* holds it: `Frame::into_raw` at map, `Frame::from_raw(paddr)` at unmap, with the physical address read back from the entry. This is sound if Linux never clears an entry of the area on its own initiative (property P1, §4.1). A violation would leak a reference until the sandbox is destroyed, which reclaims by the grant, so it degrades a tenant's page cache rather than breaking anything.
- **Lockless reads.** vOSTD reads Linux's entries through the direct map without the PTE lock, at the cost the model walk has today (3.6 to 13 ns, measured in a model). That is safe if the area's page-table pages are freed only at `vm_destroy` (property P2) and entries are written with single 64-bit stores (property P3); a torn or stale read is then impossible, and a concurrent `vm_unmap` on another virtual CPU is a race the kernel proper already serializes above `VmSpace`.
- **Across kernelets.** Nothing is shared: each space belongs to one kernelet, checked on every service.

### 3.5 TLB flushing

`vm_unmap` and `vm_protect` flush synchronously with `flush_tlb_mm_range()` before they return, as Linux's own `zap_page_range_single()` and `change_protection()` do. The kernel proper's `TlbFlusher` keeps its code; in vOSTD its dispatch issues no service, and the frames `unmap` returns are already safe to free when it returns. One consequence to state: the tree batches flushes across several unmaps and this design flushes per `vm_unmap` call, which is per cursor operation; a cursor that unmaps a long range makes one call, so the batch is the range.

`flush_tlb_mm_range()` is **not exported** in v6.12 (checked, `arch/x86/mm/tlb.c`). The patch gains one export; the alternative, `unmap_mapping_range()`, flushes but also evicts, which is the cost §1 removes. The ledger in [What it asks of Linux](src/blueprint/linux-mode/endovisor.md#patch) grows from five exports to six.

### 3.6 Large pages

Deferred, recorded as the extension: the tree's cursor can map a 2 MiB page, grains are 2 MiB-aligned, and `vmf_insert_pfn_pmd()` exists for exactly this, which would make such a page one insert and one entry. The first version maps 4 KiB only, as today, and `vm_map` refuses a large-page `prot`.

### 3.7 What the endovisor must undo

As today, minus the cache: a space is destroyed when its `VmSpace` drops, which the kernel proper does at process exit, in task context; a carrier that dies drops its adopted address space's reference; at sandbox destroy, carriers go first, then every remaining space's address space, then the grant. The area's page-table pages are Linux's, charged to the sandbox's control group through `__GFP_ACCOUNT`, and go back with the address space.

## 4. What the design rests on

### 4.1 Three properties of Linux v6.12, to be verified and recorded as host properties

- **P1.** Linux never clears or modifies an entry of a `VM_PFNMAP | VM_IO` area on its own initiative: reclaim and rmap, migration and compaction, KSM, NUMA balancing, khugepaged, the OOM reaper, `madvise` and `process_madvise` from another process, and GUP-based access (`ptrace`, `/proc/pid/mem`) all skip such areas or such entries. *Verification running; result to be pasted here with file:line evidence.*
- **P2.** The area's page-table pages are freed only by `free_pgtables()`, from `munmap` or `exit_mmap`, which only the endovisor causes; `unmap_mapping_range()`, `zap_vma_ptes()` and the `apply_to_*` family never free them. *Verification running.*
- **P3.** On x86-64 an entry is written and read as one 64-bit store and load, so a lockless reader sees an old or a new entry, never a torn one. *Verification running.*

Each becomes a stated host property on the Memory page, in the form the SMAP property has ([Reaching tenant memory](src/blueprint/linux-mode/virtualizing-ostd/memory.md#copies)), with an assumption number in the register.

### 4.2 The kernel proper's use of `VmSpace`, audited at `ab9a4cfdc`

*Audit running.* What it must establish: (a) the full API surface the Linux-native `VmSpace` has to implement, with the semantics the kernel proper relies on (what `map` over an existing entry returns, what `unmap` returns, how `protect_next` is used, whether frames are kept alive by the page table); (b) at every cursor site, which guards are live, so that R2 is known to hold for `map`, `unmap`, `protect` and `VmSpace::new/drop`; (c) which operations the kernel proper performs eagerly, since those are what the current design pays an extra fault for.

### 4.3 Facts already checked

- `vmf_insert_pfn_prot()` is `EXPORT_SYMBOL`; `apply_to_page_range()`, `apply_to_existing_page_range()` and `zap_vma_ptes()` are `EXPORT_SYMBOL_GPL` (v6.12, `mm/memory.c`). `apply_to_existing_page_range()` calls the callback only for present entries, under the PTE lock, and allocates nothing.
- `insert_pfn()` takes only the PTE lock through `get_locked_pte()` and leaves an existing entry alone (v6.12, `mm/memory.c`). `get_locked_pte()` allocates a missing page-table page with `GFP_PGTABLE_USER = GFP_KERNEL | __GFP_ZERO | __GFP_ACCOUNT` (v6.12, `include/asm-generic/pgalloc.h`), which may sleep.
- `flush_tlb_mm_range(mm, start, end, stride_shift, freed_tables)` is declared in `arch/x86/include/asm/tlbflush.h` and not exported from `arch/x86/mm/tlb.c` (v6.12).
- OSTD's cursors take an `InAtomicMode` guard (`ostd/src/mm/page_table/cursor/mod.rs:106`, `vm_space.rs:112` at `ab9a4cfdc`); spin-lock, RwLock and RCU guards are all `AsAtomicModeGuard` (`ostd/src/sync/spin.rs:140`, `rwlock.rs:284`, `rcu/mod.rs:382`), which is why R2 needs its own count rather than the guard count.
- The kernel proper's `VmSpace` cursor sites in `vm/vmar/vm_mapping.rs` and `vm/vmar/rmap.rs` pass a `disable_preempt()` guard (at `ab9a4cfdc`; the full list is §4.2's).

## 5. Costs

All *estimated* until §7 measures them.

- **Per page mapped**: one crossing (the stack switch measured at 18 cycles, the prologue and epilogue around 25), one owner-array load, the PTE lock and store that native Linux also pays, and Linux's `track_pfn_insert()`. Batched, the crossing is per run.
- **Per first touch of a mapped page**: nothing beyond the hardware. This is the container's cost.
- **Per demand-paged page**: one Linux fault, the gate's resume hook, the kernel proper's handler, one `vm_map`. One hardware fault as native, plus the detour, which this design does not remove.
- **Per `protect` of a range**: one crossing, one PTE rewrite per present entry, one flush. No refault.
- **Per `unmap` of a range**: one lockless walk by vOSTD, one crossing, one clear per entry, one flush.
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
| **E2, no fill faults.** A kernel maps 2 MiB eagerly, then the tenant touches every page | zero Linux page faults in the area after the maps (count them in the fault handler) |
| **E3, protect in place.** Map, touch, `protect` to read-only, read every page again, then write one | zero faults on the reads; exactly one `pfn_mkwrite` report on the write |
| **E4, sleeping under a preempt guard.** `map` into a fresh 2 MiB region, forcing a page-table-page allocation, from inside a kernelet `disable_preempt()` section with the mirror lifted, on a guest with `CONFIG_DEBUG_ATOMIC_SLEEP` | no "sleeping function called from invalid context", the mapping is correct, the kernelet's own scheduler did not switch during the section |
| **E5, R2's assertion.** The same under a kernelet spin lock | the kernelet is killed by vOSTD's assertion, not Linux |
| **E6, cost.** First touch of a mapped page, old against new; `vm_map` per page, single and batched; `protect` of 2 MiB then 512 reads, old against new; a copy through the lockless walk against the model walk | numbers, five boots, medians and p99, on the same guest as `bench/RESULTS.md` |
| **E7, P1 under pressure.** With tenant pages mapped, a host process allocates until reclaim runs, then the tenant reads every page | every entry still present (a counter in the fault handler), contents intact |
| **E8, concurrency.** Two virtual CPUs in one process: one maps and unmaps a range in a loop, the other reads through the walk and copies | no torn read, no stale frame handed back (the frame's owner check in `from_raw`) |

Every deviation from this document is a finding, reported with what it changes.

## 8. Review log

*Iteration 1*: draft written; verification and audit launched.

## 9. Open questions

- Whether the mirror (D119) earns its keep once services may sleep (R4): to be decided by measurement, not here.
- `vm_map` when an entry already exists: the tree's contract for `map` over an existing mapping (§4.2 will say) decides whether `-KLET_EXISTS` is right or the service should replace.
- Whether `VmSpace::new` and `drop` are ever reached under a kernelet spin lock (§4.2).
- Large pages (§3.6): when, and whether `prot`'s cache policy needs more than write-back for a tenant's device memory, which this design does not map.
