# Memory API costs

## Problem {#problem}

The current Linux-host design implements OSTD mapping and protection operations through Linux services. Its adapters add work beyond native OSTD's page-table edits, frame references, and data copies.

The main costs arise from two host integration boundaries:

- **Page-table modification through Linux services.** Linux writes the tenant page-table entries (PTEs), so vOSTD requests mapping and permission changes through host services. Bulk mapping and protection repeat service entry, Linux locking, and helper setup for individual pages. Removing read permission also calls a synchronous unmap service for each page. Each call completes the required invalidation of cached translations before returning.
- **Kernel-mode access without registered fault recovery.** The current kernelet image has no registered Linux exception-table entries for direct user-memory access. Linux vOSTD instead walks page tables in software, checks permissions, and accesses data through kernel direct-map addresses. Copies and user-memory atomics incur this lookup work even when the user mapping is valid and resident. Linux's ownership of the page table does not itself require software translation.

The unified proposal is developed in [Direct vOSTD memory and execution protocol](vostd-overhead-inventory.md#direct-vostd).

## Current design {#current-design}

### One hardware page table per address space {#versions}

Each tenant address space has one Linux page table, which hardware uses directly. The kernel proper decides virtual-memory policy, including demand paging and copy-on-write (COW). vOSTD reads mappings and requests changes through the endovisor; Linux writes the PTEs after ownership and permission checks ([Linux address spaces](../blueprint/linux-mode/virtualizing-ostd/memory.md#space)).

Tenant user-mode loads and stores use ordinary hardware address translation. Linux vOSTD uses software walks for user-buffer access through `VmReader` and `VmWriter`, and for user-memory atomics.

Successful heap allocation and frame-reference operations within an existing grant remain local to the kernelet.

### API responsibilities {#responsibility-choices}

| Operation | Asterinas host | Linux host |
|---|---|---|
| Allocate a frame or heap object within the grant | Local allocator and metadata | Local allocator and metadata |
| Map a readable page or change its permissions | vOSTD updates its own tenant PTEs | vOSTD calls a Linux memory service |
| Unmap a range | Local PTE/reference work; a host service handles remote invalidation | vOSTD records mapped frames; Linux removes PTEs and completes invalidation; vOSTD defers frame release |
| Access a user buffer | Direct user-VA access with registered fault recovery | Software walk, permission check, and direct-map access |
| Select an address space | Host registration/activation and restoration after scheduling | Record selection; before user entry, switch the carrier to the selected Linux address space if it differs from the current one |

Sources: [Asterinas memory](../blueprint/asterinas-mode/virtualizing-ostd/memory.md), [Asterinas user access](../blueprint/asterinas-mode/virtualizing-ostd/user-mode.md), and [Linux operations](../blueprint/linux-mode/virtualizing-ostd/memory.md#ops).

### Repeated setup in page-table operations {#operation-costs}

For each readable RAM page mapped into a child, Linux-host `fork` calls `vm_map`. Each readable parent RAM page whose write permission changes also requires `vm_protect`. Each service reacquires Linux's address-space lock and prepares its helper operation. A map additionally repeats virtual-memory-area (VMA) lookup and a walk verifying the installed entry ([current operations](../blueprint/linux-mode/virtualizing-ostd/memory.md#ops)).

Native OSTD also edits PTEs and maintains frame references per page. Its cursor retains locked nodes and traversal state across adjacent pages, instead of preparing an independent host operation for each page ([cursor state](https://github.com/asterinas/asterinas/blob/4012f3770348991918845ba021a7d5bbbf5f1f12/ostd/src/mm/page_table/cursor/mod.rs#L57), [cursor movement](https://github.com/asterinas/asterinas/blob/4012f3770348991918845ba021a7d5bbbf5f1f12/ostd/src/mm/page_table/cursor/mod.rs#L285)).

| API path | Current Linux work | Added work to examine |
|---|---|---|
| Readable-page `map` | Service wrapper, mmap lock, VMA lookup, grant checks, `vmf_insert_pfn_prot`, and installed-entry verification | Repeated wrapper/helper setup and verification traversal |
| `protect_next` retaining R | One-page service using `apply_to_page_range` and `pte_modify`; unchanged permission flags skip this service | Repeated wrapper, mmap locking, and helper setup |
| Range `unmap` | vOSTD reads mapped entries to recover frame references and count unmapped pages; Linux separately walks the range and removes entries | Separate walks for reference recovery and entry removal; already one service per range |

Ownership, bounds, valid permissions, cache-type handling, and reference transfer remain necessary. Linux's insertion helper can leave an existing entry unchanged, so the adapter walks the table again to verify the installed mapping. Grouping existing per-page helpers into one service call reduces service entries. Much of the helpers' internal work remains ([operations](../blueprint/linux-mode/virtualizing-ostd/memory.md#ops), [Linux insertion](https://elixir.bootlin.com/linux/v6.12/source/mm/memory.c#L2316)).

For the fork mapping loop, let N count readable RAM pages mapped into the child. Let W count readable parent RAM pages whose write permission changes. **Arithmetic:** the current path makes N map calls and W protect calls, followed by the parent's final whole-space invalidation. Already read-only pages need no protect service; unreadable pages use the withheld set. These counts exclude other process-creation work and do not establish a per-call latency.

### Removing read permission requires per-page invalidation completion {#invalidation-costs}

Removing write permission while retaining read permission updates the Linux PTE in place. `vm_protect` does not invalidate cached translations; the caller arranges the required invalidation. Removing read permission takes a different path because the chosen Linux PFN mapping cannot safely retain an unreadable page. vOSTD removes the PTE and retains the frame and properties in its **withheld set** ([operation definitions](../blueprint/linux-mode/virtualizing-ostd/memory.md#ops), [withheld mappings](../blueprint/linux-mode/virtualizing-ostd/memory.md#withheld)).

CPUs cache address translations and access permissions. Revoking access requires invalidating the cached translations that could still permit it. Native `protect_next` leaves this work to the caller, which can dispatch and wait after the protection loop. Linux vOSTD instead calls a single-page `vm_unmap` for each readable page losing read permission. Each call completes the required invalidation before returning, so the loop must finish that work before processing the next page ([native contract](https://github.com/asterinas/asterinas/blob/4012f3770348991918845ba021a7d5bbbf5f1f12/ostd/src/mm/vm_space.rs), [protection caller](https://github.com/asterinas/asterinas/blob/4012f3770348991918845ba021a7d5bbbf5f1f12/kernel/core/src/vm/vmar/vm_mapping.rs#L865)).

**Arithmetic:** N readable pages changed to `PROT_NONE` produce N synchronous unmap services, before any caller-queued invalidation. Repeated invalidation completion inside the loop adds a constraint beyond repeated service entry and setup. The latency effect remains **[unverified]**. N calls do not establish N remote interrupts or N remote waits: actual invalidation depends on entries, targets, and Linux's flush filtering. Removing a whole range can also require intermediate invalidation within Linux ([Linux zap](https://elixir.bootlin.com/linux/v6.12/source/mm/memory.c#L1917), [gather completion](https://elixir.bootlin.com/linux/v6.12/source/mm/mmu_gather.c#L437)).

### Software translation during kernel access {#retained-access}

#### User-buffer access and fault recovery {#user-buffer-access}

When kernelet code accesses a user buffer, Linux vOSTD first reads successive page-table entries, checks permissions, and finds the physical frame. This is a **software walk**: ordinary instructions perform the lookup before accessing data through the frame's kernel direct-map address. Each touched page requires translation even when the mapping is valid and resident. Successful access makes no host service call ([Linux copies](../blueprint/linux-mode/virtualizing-ostd/memory.md#copies)).

For `write(fd, buf, 16)`, assume the file implementation reads a readable, resident buffer contained within one page:

```text
Native OSTD: copy from user VA; hardware translates that address
Linux vOSTD: read page-table entries and check permissions
              -> compute direct-map VA -> copy from that kernel VA
```

The [native copy routine](https://github.com/asterinas/asterinas/blob/4012f3770348991918845ba021a7d5bbbf5f1f12/ostd/src/arch/x86/mm/memcpy_fallible.S#L15) directly uses the user address. A four-level software walk for a 4 KiB page reads four dependent entries before accessing data. A user-address TLB hit does not eliminate these software loads; the direct-map access also needs hardware translation. Frequent small copies and user-memory atomics repeatedly incur this lookup work. Its application-level slowdown remains **[unverified]**.

Direct user-memory access can fault while kernelet code runs in kernel mode. An exception-table entry gives the host a recovery address for a faulting instruction. Linux searches its own and loaded modules' exception tables; the current kernelet image has no registered entries. Linux vOSTD therefore checks mappings in software and accesses data through the kernel direct map ([current limitation](../blueprint/linux-mode/virtualizing-ostd/memory.md#copies), [module fixup experiment](../blueprint/linux-mode/prototype.md#earlier)).

Native OSTD and the Asterinas-host Blueprint support direct user-address access with fault recovery. The Asterinas host recognizes recovery entries registered for the kernelet image; an unmatched fault in kernelet text terminates the kernelet ([native handler](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/mm/fault/mod.rs), [Asterinas recovery protocol](../blueprint/asterinas-mode/virtualizing-ostd/user-mode.md)). In Linux vOSTD, a missing mapping or denied permission invokes the kernel proper's handler. Successful repair requires a fresh software walk; failed repair reports an access error ([Linux copy protocol](../blueprint/linux-mode/virtualizing-ostd/memory.md#copies)).

Linux's ownership of the tables does not require software walking; hardware already uses those tables for tenant user-mode accesses. Tenant user-mode faults still enter Linux and are forwarded to the kernelet. The kernel proper decides demand paging, COW, or rejection; the endovisor installs requested Linux mappings ([tenant faults](../blueprint/linux-mode/virtualizing-ostd/memory.md#faults)).

## Cost priorities and unresolved attribution {#costs}

Linux page-table services introduce repeated page-operation setup and per-page synchronous completion when read permission is removed. Kernel-mode user-memory access uses software translation under the current fault-recovery limitations. These are mechanism-based priorities; application-level dominance has not been measured.

| Problem | Evidence status |
|---|---|
| Repeated map/protect setup | Child-map batching improved one prototype fixture. Service-entry, lock, and helper costs were not isolated. |
| Per-page synchronous permission removal | Functional unmap counts exist; transition latency and remote waiting remain unmeasured. |
| Software translation on successful buffer access | Userspace models show added lookup/copy work. Matched complete-syscall and futex overhead remains **[unverified]**. |

## Safety constraints and limitations {#safety}

Ownership, bounds, valid permissions, cache-type handling, and reference transfer remain necessary. Reducing service calls does not remove these requirements ([entry rules](../blueprint/linux-mode/virtualizing-ostd/memory.md#ops)).

## Evidence and open questions {#validation}

The [phase-5 results](../blueprint/linux-mode/prototype.md#phase5-results) report fewer map service calls and lower fork construction time when child maps are batched. Parent-protection batching was not measured. These experiments compare two Linux-host prototype designs, not native OSTD.

The same prototype's `PROT_NONE` experiment confirms per-page unmap calls when removing read permission. Their transition latency was not reported.

The [userspace walk model](../blueprint/linux-mode/prototype.md#walk) measures additional lookup/copy work. It does not establish complete-syscall or futex overhead. The prototype's model/cache control also uses a software walk, so its copy timings do not isolate the difference from native direct access.

These reported measurements were not reproduced in this editing pass. The present evidence supports the identified adapter costs; their application-level impact remains **[unverified]**.
