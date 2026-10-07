# Memory

*Where a kernelet's memory comes from on Linux, how the kernel proper's page tables relate to Linux's, and how kernelet code reaches its tenant's memory when the processor forbids the obvious way. It discharges the memory half of safety and of fairness.*

## What a kernelet's memory is

A kernelet owns a **grant**: a set of **runs**, each a physically contiguous sequence of 2 MiB **grains** that the endovisor has given it. Everything the kernel proper allocates comes out of the grant: the frames of every tenant process, every page-table node, its heap. vOSTD runs its own frame allocator over the grant, exactly as OSTD runs one over a machine's memory, and learns of new runs by reading the **grant table**, a page the endovisor writes and the kernelet only reads.

Nothing on this page changes what the kernel proper sees. `FrameAllocOptions`, `Frame`, `Segment`, `VmSpace`, the page-table cursors, `VmReader` and `VmWriter` keep their names, signatures and meaning. What changes is below them, in three places: where grains come from, what a `VmSpace`'s page table *is*, and how a copy to or from tenant memory is performed.

## Grains come from Linux's page allocator

The service `grains_request` asks the endovisor for more memory, up to the sandbox's configured maximum. The endovisor takes it from Linux:

- up to 4 MiB at a time, two grains, directly from the page allocator, whose largest block is [`MAX_PAGE_ORDER`](https://elixir.bootlin.com/linux/v6.12/source/include/linux/mmzone.h#L30) = 10. The request is made with the flags that tell Linux not to retry hard (`__GFP_NORETRY`, `__GFP_NOWARN`): whatever compaction it does is done synchronously on the calling carrier, on the sandbox's time, and a request that would take more than that fails, to be retried one grain at a time. Compaction moves other tenants' movable pages and flushes their translations, and that interference is not charged; it is bounded by the same rate limit as the failures, and it is the price of asking Linux's allocator for 2 MiB blocks at all, which the design does because everything below that size costs the kernelet a page-table level. After a failure the endovisor refuses further requests from that kernelet for a short interval, so that a kernelet cannot turn failing requests into a loop of reclaim work for the rest of the machine;
- longer runs only from a pool the operator reserved at boot. Linux can assemble long runs at run time ([`alloc_contig_range()`](https://elixir.bootlin.com/linux/v6.12/source/mm/page_alloc.c#L6531) is exported), but that path does not charge a control group, and a tenant that could trigger it could make Linux migrate other tenants' pages without paying for it. A reserved pool is accounted by the reservation itself.

The page allocator can sleep, and can fail under fragmentation where a host that owned its allocator would not, so a tenant can see an allocation fail on Linux that would have succeeded on Asterinas. A contiguous request above 4 MiB fails on a machine with no reserved pool.

**Accounting costs nothing to arrange.** `grains_request` runs on a carrier, and every carrier is a member of the sandbox's control group ([Tasks](tasks.md#root)). The endovisor allocates with [`__GFP_ACCOUNT`](https://elixir.bootlin.com/linux/v6.12/source/include/linux/gfp_types.h#L153), which makes Linux charge the pages to the allocating task's group. The sandbox's memory limit is therefore Linux's own, enforced by Linux's own machinery, and a grant is visible in the group's statistics as kernel memory.

Every grain is zeroed before the kernelet can see it, and recorded in the **owner array**: one entry per 2 MiB of physical memory, naming the kernelet that owns that grain. Nothing but grains is ever recorded there, and in particular not an instance's private data, its shared pages, its kernelet stacks or its metadata, so no translation to any of those can ever be installed for a tenant by the [fault handler](#cache), which admits only frames the array names. The array is 8 bytes per entry, 4 MiB of table for a 1 TiB machine. The owner array is how trusted code answers "may this kernelet touch this frame?" in one load.

## Frames are reached through Linux's direct map

OSTD turns a physical address into a usable pointer by adding a constant, because the host maps all of physical memory at one base. Linux does the same: its **direct map** holds every frame, in order, with large pages, at the base [`page_offset_base`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/head64.c#L65). The endovisor writes that base into the kernelet's boot arguments, and vOSTD's `paddr_to_vaddr` is one addition, the same code as on the other host. Granting a grain needs no page-table work at all.

**Frame metadata.** OSTD keeps a 64-byte record per frame (its reference count and type). A kernelet keeps such records only for frames in its grant, in a region of its own that the endovisor allocates from Linux's `vmalloc` area and grows with the grant. A record is found through a two-level index: the physical address selects a 128 MiB *section*, the section selects a block of records. That keeps the region's size a function of the grant and not of the machine; with a flat array indexed by physical address, the reserved address space per kernelet would have capped a 1 TiB machine at about two thousand kernelets (*arithmetic*). The cost is one extra, cache-resident load when a frame's reference is taken or dropped.

## Linux's page tables are a cache of the kernelet's {#cache}

This is the central idea of the page.

The kernel proper builds a page table for every tenant address space: `VmSpace::new()` makes one, the cursor methods `map`, `unmap` and `protect_next` edit it, and its own page-fault handler implements demand paging and copy-on-write over it. On a machine the kernelet controlled, the processor would walk that page table. On Linux it cannot: a Linux task runs on the page table of its Linux address space, and only Linux writes those.

So the kernelet keeps its page tables, and the processor never walks them. They are the **model**: authoritative, complete, written only by the kernelet, in the hardware's own format, in frames of the grant. The Linux page table that the processor does walk while a tenant runs is a **cache** of the model, filled on demand and flushed on command. The relationship is the one between a page table and a TLB, one level up:

<figure class="fwd-fig">
<div class="head">
<div class="tag">Tenant address spaces</div>
<div class="title">The kernelet's page table is the truth; Linux's is a cache of it</div>
</div>
<svg viewBox="0 0 900 360" role="img" aria-label="Left: the model, a page table built and edited by the kernel proper through vOSTD's cursors, stored in frames of the grant, never walked by the processor. Right: the cache, the page table of the model's Linux address space, which the processor does walk. A tenant access that misses in the cache raises a Linux page fault; the endovisor's fault handler walks the model, checks every frame against the grant, and on a hit inserts the translation into Linux's page table. On a miss in the model it reports a page fault to the kernel proper, which maps the page in the model. When the kernel proper unmaps or write-protects a page, its TLB flush becomes a removal of the cached translations from Linux's page table.">
<defs>
<linearGradient id="mm-cg" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="#00F7FF" stop-opacity=".22"/>
<stop offset="100%" stop-color="#1937FF" stop-opacity=".22"/>
</linearGradient>
<marker id="mm-a" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#9AA0BE"/>
</marker>
<marker id="mm-ac" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#00F7FF"/>
</marker>
</defs>
<g font-family="ui-monospace,monospace" font-size="10.5">
<rect x="20" y="20" width="330" height="320" rx="10" fill="rgba(0,247,255,.04)" stroke="rgba(0,247,255,.42)"/>
<text x="34" y="40" fill="#00F7FF" font-size="9" letter-spacing="1.4">THE MODEL &#183; KERNELET</text>
<rect x="36" y="52" width="298" height="52" rx="6" fill="url(#mm-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="185" y="73" fill="#8FF6FC" text-anchor="middle">kernel proper</text>
<text x="185" y="91" fill="#5C93A8" text-anchor="middle" font-size="8.5">mmap &#183; brk &#183; fork &#183; its own page-fault handler</text>
<path d="M185 104 V128" stroke="#00F7FF" stroke-width="1.3" marker-end="url(#mm-ac)"/>
<text x="194" y="120" fill="#5C93A8" font-size="8.5">cursor.map / unmap / protect</text>
<rect x="36" y="130" width="298" height="120" rx="6" fill="url(#mm-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="185" y="150" fill="#8FF6FC" text-anchor="middle">VmSpace page table</text>
<rect x="150" y="160" width="70" height="16" rx="3" fill="rgba(6,10,36,.55)" stroke="rgba(0,247,255,.35)"/>
<rect x="90" y="186" width="70" height="16" rx="3" fill="rgba(6,10,36,.55)" stroke="rgba(0,247,255,.35)"/>
<rect x="210" y="186" width="70" height="16" rx="3" fill="rgba(6,10,36,.55)" stroke="rgba(0,247,255,.35)"/>
<rect x="56" y="212" width="60" height="16" rx="3" fill="rgba(6,10,36,.55)" stroke="rgba(0,247,255,.35)"/>
<rect x="126" y="212" width="60" height="16" rx="3" fill="rgba(6,10,36,.55)" stroke="rgba(0,247,255,.35)"/>
<rect x="250" y="212" width="60" height="16" rx="3" fill="rgba(6,10,36,.55)" stroke="rgba(0,247,255,.35)"/>
<path d="M175 176 L130 186 M195 176 L240 186 M115 202 L90 212 M135 202 L155 212 M250 202 L278 212" stroke="rgba(0,247,255,.45)" stroke-width="1"/>
<text x="185" y="243" fill="#5C93A8" text-anchor="middle" font-size="8.5">hardware format &#183; frames of the grant</text>
<text x="185" y="274" fill="#C9CCE0" text-anchor="middle" font-size="9.5">authoritative</text>
<text x="185" y="290" fill="#C9CCE0" text-anchor="middle" font-size="9.5">written only by the kernelet</text>
<text x="185" y="306" fill="#C9CCE0" text-anchor="middle" font-size="9.5">never walked by the processor</text>
<rect x="550" y="20" width="330" height="320" rx="10" fill="rgba(255,255,255,.025)" stroke="rgba(255,255,255,.12)"/>
<text x="564" y="40" fill="#9A9DB0" font-size="9" letter-spacing="1.4">THE CACHE &#183; LINUX</text>
<rect x="566" y="52" width="298" height="52" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="715" y="73" fill="#C9CCE0" text-anchor="middle">tenant program, in user mode</text>
<text x="715" y="91" fill="#6A6F8C" text-anchor="middle" font-size="8.5">loads, stores, instruction fetches</text>
<path d="M715 104 V128" stroke="#9AA0BE" stroke-width="1.3" marker-end="url(#mm-a)"/>
<text x="724" y="120" fill="#6A6F8C" font-size="8.5">the processor walks this one</text>
<rect x="566" y="130" width="298" height="120" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="715" y="150" fill="#C9CCE0" text-anchor="middle">the model's Linux page table</text>
<rect x="680" y="160" width="70" height="16" rx="3" fill="rgba(6,10,36,.55)" stroke="rgba(255,255,255,.22)"/>
<rect x="620" y="186" width="70" height="16" rx="3" fill="rgba(6,10,36,.55)" stroke="rgba(255,255,255,.22)"/>
<rect x="586" y="212" width="60" height="16" rx="3" fill="rgba(6,10,36,.55)" stroke="rgba(255,255,255,.22)"/>
<rect x="656" y="212" width="60" height="16" rx="3" fill="rgba(6,10,36,.55)" stroke="rgba(255,255,255,.22)" stroke-dasharray="3 2"/>
<path d="M705 176 L660 186 M645 202 L620 212 M665 202 L685 212" stroke="rgba(255,255,255,.3)" stroke-width="1"/>
<text x="715" y="243" fill="#6A6F8C" text-anchor="middle" font-size="8.5">one area, filled page by page on demand</text>
<text x="715" y="274" fill="#C9CCE0" text-anchor="middle" font-size="9.5">a subset of the model, never more</text>
<text x="715" y="290" fill="#C9CCE0" text-anchor="middle" font-size="9.5">written only by Linux, asked by the endovisor</text>
<text x="715" y="306" fill="#C9CCE0" text-anchor="middle" font-size="9.5">may be emptied at any time</text>
<rect x="372" y="120" width="156" height="66" rx="6" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="450" y="140" fill="#00F7FF" text-anchor="middle">endovisor</text>
<text x="450" y="156" fill="#5C93A8" text-anchor="middle" font-size="8.5">fault handler: walk the</text>
<text x="450" y="169" fill="#5C93A8" text-anchor="middle" font-size="8.5">model, check the grant</text>
<path d="M566 146 H532" stroke="#9AA0BE" stroke-width="1.3" marker-end="url(#mm-a)"/>
<text x="549" y="112" fill="#9AA0BE" text-anchor="middle" font-size="8.5">1 miss</text>
<path d="M372 146 H338" stroke="#00F7FF" stroke-width="1.3" marker-end="url(#mm-ac)"/>
<text x="356" y="112" fill="#00F7FF" text-anchor="middle" font-size="8.5">2 walk</text>
<path d="M528 172 H562" stroke="#00F7FF" stroke-width="1.3" marker-end="url(#mm-ac)"/>
<text x="546" y="200" fill="#00F7FF" text-anchor="middle" font-size="8.5">3 fill</text>
<rect x="372" y="236" width="156" height="66" rx="6" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="450" y="256" fill="#00F7FF" text-anchor="middle">endovisor</text>
<text x="450" y="272" fill="#5C93A8" text-anchor="middle" font-size="8.5">tlb_shootdown: remove</text>
<text x="450" y="285" fill="#5C93A8" text-anchor="middle" font-size="8.5">cached translations</text>
<path d="M334 268 H368" stroke="#00F7FF" stroke-width="1.3" marker-end="url(#mm-ac)"/>
<path d="M528 268 H562" stroke="#00F7FF" stroke-width="1.3" marker-end="url(#mm-ac)"/>
<text x="450" y="322" fill="#00F7FF" text-anchor="middle" font-size="8.5">after unmap or protect: flush</text>
</g>
</svg>
<figcaption>A TLB miss is resolved by walking a page table; a miss in Linux's page table is resolved by walking the model. A TLB flush invalidates cached translations; the kernel proper's TLB flush invalidates Linux's.</figcaption>
</figure>

**One Linux address space per model.** A model is a tenant process's address space, and any [virtual CPU](tasks.md#vcpus) may be asked to run any thread of that process. So the cache belongs to the model and not to a carrier: when a model is registered, the endovisor creates an empty Linux address space for it with `mm_alloc()`, the function Linux uses to make the address space of a new program (the [patch](../endovisor.md#patch) exports it), and a carrier [adopts](user-mode.md#adopt) that address space whenever it is about to run a thread of that process. All the threads of a tenant process therefore share one Linux page table, as the threads of a Linux process do, whichever virtual CPUs they run on.

**The area.** Each model also has a file object in the endovisor, created with it; it has no contents, is opened for reading and writing as a shared writable mapping requires, and exists so that Linux knows which area belongs to which model. The first time any carrier adopts a model's address space, inside [`user_run`](user-mode.md#user-run), the endovisor maps the model's file into it with [`vm_mmap()`](https://elixir.bootlin.com/linux/v6.12/source/mm/util.c#L598), which works on the caller's current address space; two carriers adopting the same model at once are serialized by a per-model mutex that is taken before Linux's address-space lock and outside the model's reader-writer lock, and the second finds the area made. It is one area that spans the whole user range, from Linux's lowest mappable address to one page below the top, at file offset equal to address. Mapping the endovisor's file is what makes the endovisor the area's fault handler ([`struct vm_operations_struct`](https://elixir.bootlin.com/linux/v6.12/source/include/linux/mm.h#L598)). The mapping is *shared* and at a *fixed* address, and both words are load-bearing: Linux stops the machine (`BUG_ON`) if raw frame numbers are inserted into a private writable area, and only a fixed mapping covers exactly the range the design claims. The area is marked as holding *raw frame numbers* and as not to be copied by `fork`. Its size counts against the carrier's address-space limit, which the runtime therefore leaves unlimited, and Linux's security modules see a shared, writable, executable mapping of an endovisor file, which the host's policy must permit. Raw frame numbers is the mode device drivers use to map their memory into a process: Linux maps exactly what it is told, keeps no reference count on the frames, never reclaims, migrates or swaps them, and refuses to pin them for anyone else. That is the right contract here, because the frames' lifetime belongs to the kernelet.

**Binding.** The service `pt_activate(root)` records a model's root as *the calling virtual CPU's* current tenant address space, as loading the page-table register does on a machine. `VmSpace::activate` calls it, and the kernel proper activates the right space whenever it switches to a tenant thread, as it does on a machine. The call only records; the Linux address space is changed at the next `user_run`, so a virtual CPU that passes through several kernel threads and tenant threads before it next reaches user mode pays for one switch at most. Because the activation belongs to the virtual CPU, as the page-table register belongs to a processor, a task that never activates anything runs in whatever space its virtual CPU last activated, which is what the Hello World kernel relies on ([the prototype](../prototype.md#findings)). A tenant's `execve` makes a new model, and so a new Linux address space; the old one goes when the kernel proper drops the old model and vOSTD unregisters its root.

**A miss in the cache.** The tenant touches an address Linux has no translation for. Linux calls the area's fault handler, on the carrier, in kernel mode. The handler is endovisor code, and it does not enter the kernelet. It walks the model itself, four loads through the direct map, as a processor would. At every level it checks that the frame it is about to read, and finally the frame it is about to map, is in this kernelet's grant: the frame number must be below the machine's highest, which bounds the index, and the owner-array entry at that index must name this kernelet. An entry that claims a large page is treated as absent, since the design maps tenant memory in 4 KiB pages only.

The handler never copies bits from a model entry into Linux's page table. It reads three facts from the leaf (writable, executable, present for user mode) and builds the protection itself, from Linux's own constants for a user page with those rights. A model is kernelet memory, and a hostile or buggy one could set any bit; one of them, the *global* bit, would make a translation visible to every process on the machine. If the leaf is present and permits the access, it asks Linux to install the translation with [`vmf_insert_pfn_prot()`](https://elixir.bootlin.com/linux/v6.12/source/mm/memory.c#L2403), and the tenant's instruction is retried. The handler always installs the leaf's *full* permissions, not just what this access needed, because Linux ignores an insertion over an existing entry, and a narrower entry would make the next, wider access fault forever (*found by the prototype*).

Two kinds of access to a page that is *already* cached take other routes through Linux, and both end in the same place. A write to a cached read-only translation does not call `fault`; Linux calls the area's `pfn_mkwrite` function if it has one, and makes the entry writable on its own if it has none. The endovisor supplies that function and consults the model in it, under the same rules as `fault`: if the model's leaf permits the write *and names the same frame as the cached entry*, the function returns success and Linux itself makes the cached entry writable (re-inserting would do nothing, as above); if the model does not permit the write, this is a miss in the model; and if the frames differ, which means a flush was skipped, the handler removes that one stale entry from the cache, and no other, and lets the access fault again. A read or an instruction fetch that the cached entry forbids never reaches the area at all; Linux raises `SIGSEGV` by itself, and the gate's resume hook turns that signal back into a page-fault exception for the kernel proper, as it does for [every exception](user-mode.md#exceptions).

That check is a stronger position than the design has when Asterinas is the host. There, the processor walks page tables the kernelet wrote, and a bug in vOSTD could map any frame of the machine into a tenant. Here, no translation reaches hardware without trusted host code having checked the frame against the grant.

**Faults that Linux takes on its own account.** Linux itself sometimes touches a task's user memory from kernel mode: the 32-bit fast system-call entry reads one word from the user stack before it reaches the gate, and the emulation of the legacy vsyscall page reads the caller's return address. If such an access misses in the cache, the handler fills it from the model when the model has the page, and otherwise answers with an error (`VM_FAULT_SIGBUS`), so that Linux's own exception table turns the access into `-EFAULT`, which that code is written to expect. Linux tells the two cases apart for the handler with a flag that is set only for faults taken in user mode. The rule is strict, and it is what makes the next paragraph's protocol safe: **the kernel proper is told about a fault only if the tenant took it in user mode.** A handler that claimed a kernel-mode fault and waited for the return to user mode to make progress would wait forever, in a loop no signal could break (*found in review*).

**A miss in the model.** If the tenant took the fault and the model has no mapping for the address, or not one that permits the access, the fault is the kernel proper's to handle: it may be a page to demand-load, a copy-on-write, or a genuine error that ends in a signal. The handler cannot run the kernel proper where it stands, inside Linux's fault path with Linux's address-space lock held. It records the address and the error code in the carrier record, [flags the carrier](user-mode.md#gate) as having work, and tells Linux the fault is handled (the handler returns `VM_FAULT_NOPAGE` without having installed anything, so Linux heads back to user mode to retry the instruction). On the way back to user mode the gate's [resume hook](user-mode.md#exceptions) runs, `user_run` returns *exception*, and the kernel proper's handler runs on the kernelet stack, as a normal task, free to sleep on disk I/O. It maps the page into the model and calls `user_run` again. Before returning to user mode, the endovisor walks the model once more for the address that faulted and, if it is now mapped, installs the translation itself, so that the retried instruction does not fault a second time.

<figure class="fwd-fig">
<div class="head">
<div class="tag">A demand-paged page, step by step</div>
<div class="title">The kernel proper handles the fault as a task, not inside Linux's fault path</div>
</div>
<svg viewBox="0 0 900 300" role="img" aria-label="Six steps. One: the tenant touches a page that is in neither Linux's page table nor the model. Two: Linux's fault path calls the endovisor's handler, which walks the model, finds nothing, records the address and error code, flags the carrier, and returns without installing anything. Three: on the way back to user mode the gate's resume hook switches to the kernelet stack. Four: in the kernel proper, execute returns a page-fault exception; its own handler picks a frame, maps it in the model, and calls execute again. Five: the endovisor's user_run walks the model for the recorded address, now finds the page, checks the frame against the grant, and installs the translation. Six: the tenant's instruction is retried and succeeds.">
<defs>
<linearGradient id="pf-cg" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="#00F7FF" stop-opacity=".22"/>
<stop offset="100%" stop-color="#1937FF" stop-opacity=".22"/>
</linearGradient>
<marker id="pf-a" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#00F7FF"/>
</marker>
</defs>
<g font-family="ui-monospace,monospace" font-size="9.5">
<g>
<rect x="20" y="20" width="270" height="110" rx="8" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="32" y="40" fill="#9A9DB0" font-size="9" letter-spacing="1.2">1 TENANT &#183; USER MODE</text>
<text x="32" y="64" fill="#C9CCE0">touches a page that is in neither</text>
<text x="32" y="80" fill="#C9CCE0">Linux's page table nor the model</text>
<rect x="315" y="20" width="270" height="110" rx="8" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="327" y="40" fill="#00F7FF" font-size="9" letter-spacing="1.2">2 ENDOVISOR &#183; IN LINUX'S FAULT PATH</text>
<text x="327" y="64" fill="#C9CCE0">walk the model: nothing there</text>
<text x="327" y="80" fill="#C9CCE0">record address and error code</text>
<text x="327" y="96" fill="#C9CCE0">flag the carrier; install nothing</text>
<text x="327" y="116" fill="#5C93A8" font-size="8.5">no kernelet code runs here</text>
<rect x="610" y="20" width="270" height="110" rx="8" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="622" y="40" fill="#00F7FF" font-size="9" letter-spacing="1.2">3 THE GATE &#183; RESUME HOOK</text>
<text x="622" y="64" fill="#C9CCE0">on the way back to user mode:</text>
<text x="622" y="80" fill="#C9CCE0">switch to the task's</text>
<text x="622" y="96" fill="#C9CCE0">kernelet stack; user_run returns 1</text>
<rect x="610" y="170" width="270" height="110" rx="8" fill="url(#pf-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="622" y="190" fill="#8FF6FC" font-size="9" letter-spacing="1.2">4 KERNEL PROPER &#183; A NORMAL TASK</text>
<text x="622" y="214" fill="#C9CCE0">execute() = UserException(page fault)</text>
<text x="622" y="230" fill="#C9CCE0">its handler picks a frame, maps it</text>
<text x="622" y="246" fill="#C9CCE0">in the model, calls execute() again</text>
<text x="622" y="266" fill="#5C93A8" font-size="8.5">may sleep: disk I/O, locks, allocation</text>
<rect x="315" y="170" width="270" height="110" rx="8" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="327" y="190" fill="#00F7FF" font-size="9" letter-spacing="1.2">5 ENDOVISOR &#183; USER_RUN</text>
<text x="327" y="214" fill="#C9CCE0">walk the model for the recorded</text>
<text x="327" y="230" fill="#C9CCE0">address: found; check the frame</text>
<text x="327" y="246" fill="#C9CCE0">against the grant; install it</text>
<rect x="20" y="170" width="270" height="110" rx="8" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="32" y="190" fill="#9A9DB0" font-size="9" letter-spacing="1.2">6 TENANT &#183; USER MODE</text>
<text x="32" y="214" fill="#C9CCE0">the instruction is retried,</text>
<text x="32" y="230" fill="#C9CCE0">and succeeds</text>
</g>
<g stroke="#00F7FF" stroke-width="1.4" fill="none">
<path d="M290 75 H313" marker-end="url(#pf-a)"/>
<path d="M585 75 H608" marker-end="url(#pf-a)"/>
<path d="M745 130 V168" marker-end="url(#pf-a)"/>
<path d="M610 225 H587" marker-end="url(#pf-a)"/>
<path d="M315 225 H292" marker-end="url(#pf-a)"/>
</g>
</g>
</svg>
<figcaption>Steps 2 and 5 are the same walk. The first time it fails and the kernel proper is asked; the second time it succeeds and the tenant never sees a second fault.</figcaption>
</figure>

**Flushing.** When the kernel proper unmaps or write-protects pages it must flush stale translations, which OSTD expresses as a `TlbFlusher` that the kernel proper drives and that, in vOSTD, calls the service `tlb_shootdown(root, start, len)`. On Linux that service removes the range from the model's cache with one call, [`unmap_mapping_range()`](https://elixir.bootlin.com/linux/v6.12/source/mm/memory.c#L3858) on the model's file: Linux itself keeps the list of areas that map a file, finds the one that covers the range, removes its translations and performs the real TLB shootdown on the processors where that address space is loaded. This is the interface drivers use to revoke memory they have mapped into processes. The endovisor clamps the range to the user range the areas cover, and vOSTD refuses, at `map` time, a mapping outside that range (its bounds are in the boot arguments), so that no part of a model can lie where a flush would silently not reach.

### Filling against flushing {#interlock}

A fault handler on one carrier and an unmap on another can run at the same time, and without care the handler could read a leaf, lose the processor, and install the translation *after* the flush had come and gone, leaving a tenant with a window onto a frame the kernel proper has freed. The same goes for an intermediate table that the kernel proper frees while the handler is reading it. Two rules close this, one on each side of the boundary.

- *In the endovisor*, each model has a reader-writer lock. It is a sleeping lock (Linux's `rw_semaphore`), because inserting a translation can allocate a page-table page. Its order is fixed: a fault handler already holds Linux's address-space lock when it takes the model's lock (Linux tries a lighter, per-area lock first, but only for areas that also supply a bulk `map_pages` function, which this one deliberately does not); a flush takes the model's lock and then, inside `unmap_mapping_range()`, Linux's lock on the file's list of areas; and nothing takes the model's lock and then an address-space lock, which is why a carrier creates a model's area outside it. A fault handler (`fault` or `pfn_mkwrite`) holds it shared from before its walk until after its insertion; any number of handlers proceed together. `tlb_shootdown` and `pt_root_unregister` take it exclusively around the `unmap_mapping_range()` call. So when a flush returns, every fill that might have read the old model has finished inserting, and what it inserted has been removed; every later fill reads the new model.
- *In vOSTD*, a frame or a page-table node that an unmap removes from a model is not freed or reused until the `tlb_shootdown` that covers it has returned. OSTD's `TlbFlusher` already holds unmapped *frames* until its flush completes; vOSTD extends that to page-table nodes, which OSTD otherwise frees under its own RCU, a protection the endovisor's walk is not part of.

The flush is therefore synchronous in the strong sense, and OSTD's guarantee holds: a frame is not freed until no translation to it remains and none can appear. **[unverified]**: the prototype's first three phases did not have this interlock; with one virtual CPU and kernels that map everything before their tenant runs, they could not race.

**One cache per model, not per thread.** Threads of one tenant process share a model and therefore one Linux page table: a page touched by eight threads is filled once, and a flush visits one area. The alternative, a Linux task with its own address space per tenant thread, multiplies both by the thread count.

## Reaching tenant memory from kernelet code {#copies}

A system call that carries a buffer makes the kernel proper read or write tenant memory, through OSTD's `VmReader` and `VmWriter`. On its own machine OSTD simply dereferences the tenant's address and lets the page-fault handler sort out the failures.

On Linux that is forbidden by the processor. Since Broadwell and Zen, x86-64 refuses a kernel-mode access to a page mapped for user mode unless a flag is set, a protection called SMAP, and Linux turns it on ([`setup_smap()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/cpu/common.c#L367)). Linux's own copy routines set the flag around each copy and recover from bad addresses through an exception table that lists the instructions allowed to fault. A kernelet can set the flag too, but it cannot recover: Linux consults only its own tables and those of loaded modules, and a kernelet image is neither. *Measured* in an earlier experiment: an access with the flag set succeeds on a good address and takes the task down on a bad one ([the prototype](../prototype.md#earlier)). Since validating tenant pointers *by faulting on them* is the kernel proper's normal practice, that is disqualifying.

The model removes the need. vOSTD never dereferences a tenant address. To copy, it walks the model from the tenant address to the frame and copies through the frame's direct-map alias, which is an ordinary kernel address that SMAP does not guard. An unmapped or protected address is a *result* of the walk, not a fault, and vOSTD turns it into the same call to the kernel proper's page-fault handler that a hardware fault would have caused. There is no exception table, no fixup, and no instruction in a kernelet that is expected to fault. An atomic compare-and-exchange on tenant memory, which is what a futex needs and what a copy cannot express, becomes an ordinary atomic instruction on the alias.

**What the walk costs** was the question this design was most likely to fail on, so it was measured before anything else was decided (*measured in a model*: a user-space replica of the walk on the development machine; [the prototype](../prototype.md#walk) has the method and the full tables).

| what | cost |
|---|---|
| one walk, its four table entries already in the first-level cache | 3.6 ns (15 cycles) |
| one walk, random over a 16 MiB working set | 5 ns |
| one walk, random over a 1 GiB working set | 13 ns |
| one walk, all four entries evicted from cache first | about 300 ns |
| copying 16 bytes from a just-touched tenant page: direct dereference / through a second 4 KiB mapping / **walk plus direct map** | 2.6 / 4.7 / **12.1 ns** |
| the same with a 1 GiB working set | 2.6 / 17.4 / **23.7 ns** |
| copying 4 KiB, 16 MiB working set | 245 / 266 / **265 ns** |
| for scale: one `getppid` system call on the same machine, with its mitigations | 479 to 497 ns |

So the walk adds about 10 ns to a system call that carries a small buffer, and disappears into the copy for a large one. A cold walk is as expensive as feared, 300 ns, but it is paid when the tenant's own access to the same page has just pulled the page table's neighborhood into cache, which is the common case for a buffer the tenant has just filled or is about to read. The walk takes no lock: OSTD already protects page-table nodes from being freed under a reader by RCU, and a copy only reads. A small cache of recent leaf tables in front of the walk was measured too; it saves about 5 ns on small working sets, is slower than the plain walk on large ones, and was left out.

The standing of these numbers is limited: they were taken in user space, on one machine, on a replica of the walk and not on vOSTD.

The rejected alternative maps the model's top-level entries a second time into the *kernel* half of the carrier's address space, with the user bit cleared, so that tenant address plus a constant is a kernel address and a copy needs no walk. It was not adopted: it writes page-table entries Linux believes it owns, in a slot of the address-space layout that a later Linux could allocate; it must be reinstalled in every new address space; it gives kernel mode a window onto user pages, which is the hole SMAP exists to close. Against that, the measurement above says it would save about 7 ns on a small copy and nothing on a large working set, where its extra TLB misses cost what the walk costs. That was the gate the alias had to pass, and it did not ([register D108](../../../notes/design-register.md#decisions)).

## What the endovisor must undo

- **A model is unregistered.** The endovisor empties the cache and drops its own reference on the Linux address space, the one it took at `mm_alloc()`. A carrier that has adopted an address space holds a reference of its own ([User mode](user-mode.md#adopt)), which it drops when it adopts another or dies. Linux frees an address space when its last user lets go, and a carrier that adopted this one and has not yet adopted another is still a user, so the teardown may happen a little later, on that carrier; because the area maps the model's file, Linux itself takes it off that file's list then. Nothing is freed on the kernelet's side; frames belong to the model. A model's file, and so the model's record, lives until the area that maps it is gone, and a fault handler that finds its model unregistered fills nothing.
- **A carrier dies.** Linux drops the carrier's reference on whatever address space it had adopted. (Its blank one went at its first adoption.) The address space records as its *owner* whichever carrier registered the model; nothing in this design charges or looks anything up through that field, and Linux may clear it when that carrier dies.
- **A grain goes back to Linux**, when a sandbox is destroyed. By then every carrier is dead, and the endovisor has dropped its reference on every model's address space, so every cache is gone. The order is part of the [destroy sequence](../faults-and-reclamation.md#destroy): carriers first, then address spaces, grant last. While a kernelet lives, memory only grows, as on the other host.
- **Fork.** A tenant `fork` copies a *model*, in the kernel proper, with copy-on-write implemented there. Linux's `fork` plays no part; the new model gets a new, empty Linux address space when it is registered.

## Costs

- **Per grain granted**: a Linux allocation, 2 MiB of zeroing, eight metadata pages, one owner-array store.
- **Per first touch of a page by a tenant process**: a Linux page fault, a four-load walk with four owner checks, and one page-table insertion. **[unverified]**: about a microsecond (*estimated* from Linux's minor-fault cost).
- **Per demand-paged or copy-on-write fault**: the above, plus one return to the exit loop and the resume hook before the kernel proper's handler runs.
- **Per copy to or from tenant memory**: one walk of the model per page touched, in place of nothing at all on the other host. See above for the measurement.
- **Per flush**: one exclusive acquisition of the model's lock and one `unmap_mapping_range()`, which visits the model's one area.
- **Per model**: one Linux address space, which is a few kilobytes of Linux's memory and the page-table pages of the cache, charged to the sandbox.
- **Per fill**: one shared acquisition of the model's lock, uncontended unless a flush is in progress.

## What a tenant sees

The same virtual memory behavior as on the other host, with one difference it can measure and none it can otherwise observe: the first touch of a page is slower. A tenant cannot see or affect its Linux address space, because every interface to it is a Linux system call.

## What this page decides

- **The kernelet's page table is the model and Linux's page table is a cache of it, filled by a fault handler that validates every frame against the grant** ([register D95](../../../notes/design-register.md#decisions)). The alternative kept Linux's address space as the truth and gave the kernelet a side table of address-to-frame pairs; that table can go stale silently when Linux tears a mapping down, and a stale entry resolves to a frame that may have been given to another tenant.
- **The kernel proper hears of a fault only if the tenant took it in user mode, and the handler builds protections itself** ([register D113](../../../notes/design-register.md#decisions)). Both were found in review: claiming Linux's own kernel-mode accesses hangs a carrier unkillably, and copying a leaf's bits lets a model set the global bit.
- **Filling and flushing are interlocked by a per-model reader-writer lock, and vOSTD frees nothing an unmap removed until the flush has returned** ([register D111](../../../notes/design-register.md#decisions)). The alternative, a sequence counter that lets a fill detect a concurrent flush and undo itself, avoids the lock but leaves a moment in which a stale translation exists.
- **Kernelet code never dereferences a tenant address; it walks the model and uses the direct map** ([register D82](../../../notes/design-register.md#decisions), kept and completed). The alternatives, bracketing the access or aliasing tenant memory into the kernel half, are above.
- **Each model has one Linux address space, which the virtual CPUs adopt in turn** ([register D120](../../../notes/design-register.md#decisions), which supersedes D96 and withdraws assumption A26). It costs one helper function in the patch. The alternative, a cache per Linux task, needs no helper and multiplies every fill and every flush by the number of threads.
- **Grains are allocated on a carrier with `__GFP_ACCOUNT`** ([register D97](../../../notes/design-register.md#decisions)), so that a sandbox's memory is charged by membership.
- **Frame metadata is indexed by section** ([register D86](../../../notes/design-register.md#decisions), kept).
