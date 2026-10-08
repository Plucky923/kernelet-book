# Boundaries and trust

*Who the parties are, what separates them, what the boundary promises a tenant, and the invariants every later page has to uphold. This page designs nothing; it fixes the terms the rest of the chapter is held to.*

## The parties

Six kinds of code take part, and they are not equally trusted.

| party | what it is | trusted for |
|---|---|---|
| **the tenant** | the programs in a sandbox: arbitrary native code in user mode | nothing; assumed hostile |
| **the kernel proper** | the Linux-compatible kernel inside a kernelet: safe Rust, unchanged source | *memory safety only*, which the compiler enforces. It may have any logic bug, and the tenant is looking for one. Its failures are its own tenant's problem |
| **vOSTD** | the framework the kernel proper is compiled against; may use `unsafe`; one instance per kernelet | correctness. It runs in kernel mode with Linux's direct map in reach, so a memory-safety bug in it is a bug in the host |
| **the endovisor** | the Linux module that creates and serves kernelets | everything, as the rest of Linux is |
| **Linux** | the host kernel | everything. Every tenant trusts it, as every container trusts its kernel and every guest its hypervisor |
| **the kernelet runtime** | host user space that configures sandboxes | by the host, as a container runtime is; no tenant sees it |

The trusted base is therefore Linux, the endovisor, vOSTD and the Rust compiler. What is *removed* from a tenant's attack surface is Linux's system-call interface: several hundred entry points into tens of millions of lines of C, replaced by the kernel proper, which is the tenant's own and cannot corrupt memory.

## The boundaries

<figure class="fwd-fig">
<div class="head">
<div class="tag">Who can reach what</div>
<div class="title">Four narrow interfaces, and no other way across</div>
</div>
<svg viewBox="0 0 900 300" role="img" aria-label="From top to bottom. The tenant reaches its kernel proper only through system calls and faults, which the gate delivers. The kernel proper reaches vOSTD only through OSTD's safe Rust interface, which the compiler enforces. vOSTD reaches the endovisor only through the service table of twenty-five C functions, because the image has no undefined symbols. The kernelet runtime reaches the endovisor only through its character device. The endovisor reaches Linux through exported kernel functions.">
<defs>
<linearGradient id="pr-cg" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="#00F7FF" stop-opacity=".22"/>
<stop offset="100%" stop-color="#1937FF" stop-opacity=".22"/>
</linearGradient>
</defs>
<g font-family="ui-monospace,monospace" font-size="10.5">
<rect x="20" y="16" width="520" height="40" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="280" y="41" fill="#C9CCE0" text-anchor="middle">tenant programs &#183; hostile &#183; user mode</text>
<rect x="600" y="16" width="280" height="40" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="740" y="41" fill="#C9CCE0" text-anchor="middle">kernelet runtime &#183; user mode</text>
<text x="280" y="74" fill="#00F7FF" text-anchor="middle" font-size="9">1 system calls and faults, delivered by the gate</text>
<rect x="20" y="84" width="520" height="40" rx="6" fill="url(#pr-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="280" y="109" fill="#8FF6FC" text-anchor="middle">kernel proper &#183; safe Rust &#183; memory-safe, otherwise untrusted</text>
<text x="280" y="142" fill="#00F7FF" text-anchor="middle" font-size="9">2 OSTD's safe API, enforced by the compiler</text>
<rect x="20" y="152" width="520" height="40" rx="6" fill="url(#pr-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="280" y="177" fill="#8FF6FC" text-anchor="middle">vOSTD &#183; trusted &#183; one instance per kernelet</text>
<text x="280" y="210" fill="#00F7FF" text-anchor="middle" font-size="9">3 the service table: 25 C functions; the image has no other symbol</text>
<text x="740" y="142" fill="#00F7FF" text-anchor="middle" font-size="9">4 /dev/kernelet</text>
<path d="M740 56 V220" stroke="rgba(0,247,255,.5)" stroke-width="1.2" stroke-dasharray="4 3"/>
<rect x="20" y="220" width="860" height="30" rx="6" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="450" y="240" fill="#00F7FF" text-anchor="middle">endovisor &#183; a Linux module &#183; trusted</text>
<rect x="20" y="256" width="860" height="30" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="450" y="276" fill="#9AA0BE" text-anchor="middle">Linux &#183; patched with the gate &#183; trusted</text>
</g>
</svg>
</figure>

1. **Tenant to kernel proper**: system calls and exceptions, and nothing else. The [gate](virtualizing-ostd/user-mode.md) guarantees that a carrier's system calls go to its kernelet and never to Linux.
2. **Kernel proper to vOSTD**: OSTD's Rust interface. The kernel proper is compiled with `unsafe` forbidden, so it can name and touch only what that interface gives it. This boundary is enforced by the compiler and checked by the [build audit](builds-and-images.md#audit).
3. **vOSTD to the endovisor**: the [service table](kernelet-api-service.md). The image has no undefined symbols, so it has no other way to call out.
4. **Runtime to endovisor**: [one character device](endovisor.md#abi).

Kernelets are not separated from each other or from Linux by hardware. They run in kernel mode, in one address space. What separates them is that the only code in a kernelet that *could* reach across is vOSTD, which is trusted, and everything above it is safe Rust. That is the design's central bet, and this page does not soften it: against arbitrary kernel-mode code inside a kernelet, nothing here holds.

## Threat model

The tenant runs arbitrary code in user mode, drives its kernelet down every path a system call reaches, and tries to read, corrupt or starve other tenants and the host. It may find and exploit any *logic* bug in the kernel proper. Denial of service is in scope. Speculative-execution side channels are out of scope, as they are for containers; so is what a tenant can infer about its neighbors' activity from timing, which two-level scheduling makes a little easier to read (a virtual CPU learns how much time Linux took from it), as a virtual machine's guest can. A bug in Linux, the endovisor or vOSTD is a bug in the trusted base.

## What the boundary owes a tenant

Three properties, and how they stand on Linux:

| property | meaning | standing on Linux |
|---|---|---|
| **safety** | no state is read or written across the boundary except through a mediated channel | held. The tenant's only kernel surface is its kernelet; every frame that enters a tenant's page table is checked against its kernelet's grant by the endovisor; the residue is Linux's own trusted base |
| **fault containment** | one kernelet's failure ends that kernelet only, and everything it held is returned | held for a kernelet's own code (service-call depth 0), at the price of one printed oops when the failure is a fault. Not held for a failure inside a service call, which is a host bug; the design has the same bound on either host |
| **fairness** | every resource a kernelet consumes is charged to it and bounded | held for processor time and memory by Linux's control groups, because every Linux task and every allocation of a kernelet belongs to the sandbox's group, and for the kernelet's own task count by the configured ceiling on its stack pool. Not held for interrupt-time work that a tenant's I/O and timers induce, which Linux charges to whatever it interrupts; the design bounds that work (ring depths, a floor on timer deadlines) but does not charge it. And not held for *latency*: a kernelet in a critical section may hold a processor against Linux for about 2 ms, two watch periods and a delivery, ([Scheduling](virtualizing-ostd/scheduling.md#cooperative)), charged but not preventable, which is a tax a container does not levy. Global memory pressure is the operator's to prevent, by keeping the sum of sandbox limits within the machine |

## Invariants

The table after the list says which page carries the mechanism behind each. Each has a standing: *checked* where a tool or a type enforces it, *argued* where review does, *trusted* where it holds only because trusted code is correct.

- **I1, reach.** *Checked.* The kernel proper can name only what vOSTD and the allowlisted crates define; the image reaches the host only through the service table. Enforced by the compiler under `forbid(unsafe_code)` and by the audit's "no undefined symbols".
- **I2, ownership.** *Checked at the endovisor; trusted inside vOSTD.* Every frame a kernelet maps for a tenant or hands to a device is in its grant, and its own writes go only to its grant, its instance's data and its stacks. On Linux the second of those is stronger than on the other host: no translation reaches a tenant's page table without the endovisor checking the frame, in `vm_map`, which is the only way an entry is made ([Memory](virtualizing-ostd/memory.md#ops)). vOSTD's own writes through the direct map are trusted.
- **I3, privacy.** *Trusted.* A kernelet's data is ordinary kernel memory. It is private because no safe code is ever handed a reference to it, not because hardware guards it.
- **I4, no retained reference.** *Checked by types; the drain list is argued.* The endovisor holds no pointer into a kernelet across the return of a service call, except the records [destroy](faults-and-reclamation.md#destroy) enumerates. The words of a tenant address space's [range lock](virtualizing-ostd/memory.md#range-lock), which `vm_wait` reads, live in the space's shared record, a page of the endovisor's that the kernelet also writes, and are named by index, so no pointer into kernelet memory is retained for them.
- **I5, no closure crosses.** *Checked by types.* The endovisor stores no function pointer into a kernelet beyond the entry table, and a kernelet none into the host beyond the service table. The endovisor never sees a task or a closure of a kernelet at all; it enters an image at three fixed addresses (the ELF entry point, and the entry table's entry for a secondary virtual CPU and its upcall stub), and checks at registration that they lie in the image's text.
- **I6, charged work.** *Checked by membership.* Every carrier and device thread of a kernelet is in the sandbox's control group, and every grain is allocated there. Linux sees a sandbox as a fixed number of tasks in one group, whatever the kernelet's own scheduler does with its tasks, and no service changes a carrier's priority, class or affinity; the one way a kernelet can hold a processor against Linux's wishes is [bounded at 2 ms of its own run time and charged](virtualizing-ostd/scheduling.md#fair), and what it can send to other processors (kicks, spin yields, flushes) is bounded and stays inside its `cpuset`, which is why latency-critical host work is kept out of it.
- **I7, termination.** *Eviction is shown on the prototype for one spinning kernelet; the rest is argued.* A carrier whose instruction pointer is in kernelet text holds nothing of Linux's beyond an increment of its preemption count, which the exit stub gives back, and references that Linux's own exit path returns (its address space, its lifeline), and can be removed at any instruction; every sleep inside a service call is killable, or ends with Linux's own work ([service half](kernelet-api-service.md#depth)); and a kernelet can be destroyed without running any of its code ([Faults](faults-and-reclamation.md)).
- **I8, compatibility.** *Checked by the build.* The kernel proper's source is the same on every host, and its behavior differs only where the [classification](virtualizing-ostd/README.md) says an item is virtualized or absent.

| invariant | where its mechanism is |
|---|---|
| I1 reach | [Builds and images](builds-and-images.md#audit) (the audit); [service half](kernelet-api-service.md) (the table is the only way out) |
| I2 ownership | [Memory](virtualizing-ostd/memory.md#ops) (`vm_map`'s grant check); [Devices](virtualizing-ostd/devices.md) (the check on every buffer) |
| I3 privacy | [Builds and images](builds-and-images.md#sharing) (what is shared and what is private) |
| I4 no retained reference | [service half](kernelet-api-service.md) (integer arguments); [Memory](virtualizing-ostd/memory.md#range-lock) (lock words named by index); [Faults](faults-and-reclamation.md#destroy) (the drain list) |
| I5 no closure crosses | [service half](kernelet-api-service.md#abi) (three fixed ways in) |
| I6 charged work | [Tasks](virtualizing-ostd/tasks.md#root) (membership by inheritance); [Scheduling](virtualizing-ostd/scheduling.md#fair) (why a kernelet's scheduling cannot cost its neighbors); [Memory](virtualizing-ostd/memory.md) (charged grains); [The endovisor](endovisor.md#patch) (the list of what is charged) |
| I7 termination | [Faults](faults-and-reclamation.md#stopping) |
| I8 compatibility | [Virtualizing OSTD](virtualizing-ostd/README.md) |

## What this page decides

- **The boundary is the language and four narrow interfaces, not hardware** ([register D89](../../notes/design-register.md#decisions) restates this for Linux). The alternative that restores hardware separation, running each kernelet in a hardware-virtualized guest, is [considered and rejected](alternatives.md) for its cost on every memory access.
