# Design for Linux

*The same kernelets, with Linux as the host kernel. An operator keeps the kernel they already run, applies a small patch, loads one module, and selected workloads get a kernel of their own in safe Rust. This chapter is how Linux meets the host-independent design of [Design](../design/index.md): it carries what is Linux's, and links to that chapter for what is not. A prototype of its central mechanisms runs a kernel whose source is unchanged from the Asterinas tree, inside a patched Linux 6.12.*

<figure class="fwd-fig">
<div class="head">
<div class="tag">The architecture</div>
<div class="title">One Linux kernel; each sandbox has a kernel of its own inside it</div>
</div>
<svg viewBox="0 0 900 420" role="img" aria-label="Left, the host side: Linux applications and the kernelet runtime in user mode; below them the Linux kernel, with its own subsystems, the gate patched into its entry path, and the endovisor module containing the loader, the service table implementation, the fault handler, containment and device models. Right, one sandbox of many: the tenant's programs run in user mode on the kernelet's virtual CPUs, each of which is a Linux task called a carrier; their system calls pass through the gate into the kernelet, which is the unchanged Asterinas kernel proper in safe Rust over vOSTD, holding the model page tables and its grant of memory. The kernelet calls the endovisor only through the service table. Linux's page tables for the tenant's processes are filled from the kernelet's model by the endovisor's fault handler.">
<defs>
<linearGradient id="ix-cg" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="#00F7FF" stop-opacity=".22"/>
<stop offset="100%" stop-color="#1937FF" stop-opacity=".22"/>
</linearGradient>
<marker id="ix-a" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#9AA0BE"/>
</marker>
<marker id="ix-ac" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#00F7FF"/>
</marker>
</defs>
<g font-family="ui-monospace,monospace" font-size="10.5">
<text x="20" y="22" fill="#9A9DB0" font-size="9" letter-spacing="1.6">HOST</text>
<text x="880" y="22" fill="#00F7FF" font-size="9" letter-spacing="1.6" text-anchor="end">A SANDBOX (ONE OF MANY)</text>
<rect x="20" y="34" width="150" height="36" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="95" y="56" fill="#9AA0BE" text-anchor="middle">Linux apps</text>
<rect x="186" y="34" width="224" height="36" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="298" y="56" fill="#9AA0BE" text-anchor="middle">kernelet runtime</text>
<rect x="490" y="34" width="390" height="36" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(0,247,255,.30)"/>
<text x="685" y="50" fill="#C9CCE0" text-anchor="middle">tenant programs</text>
<text x="685" y="63" fill="#6A6F8C" text-anchor="middle" font-size="8">run on the kernelet's virtual CPUs; each is a Linux task, a carrier</text>
<path d="M12 90 H888" stroke="rgba(255,255,255,.28)" stroke-dasharray="5 4"/>
<text x="14" y="86" fill="#6A6F8C" font-size="8">user mode</text>
<text x="14" y="101" fill="#6A6F8C" font-size="8">kernel mode</text>
<rect x="12" y="108" width="876" height="296" rx="10" fill="rgba(255,255,255,.025)" stroke="rgba(255,255,255,.12)"/>
<text x="24" y="124" fill="#9A9DB0" font-size="9" letter-spacing="1.4">THE LINUX KERNEL THE OPERATOR ALREADY RUNS</text>
<rect x="24" y="134" width="386" height="40" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="217" y="159" fill="#9AA0BE" text-anchor="middle">Linux's own subsystems, unchanged</text>
<rect x="490" y="134" width="386" height="40" rx="6" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="683" y="152" fill="#00F7FF" text-anchor="middle">the gate: a patch to Linux's entry path</text>
<text x="683" y="166" fill="#5C93A8" text-anchor="middle" font-size="8.5">a carrier's system calls and faults go to its kernelet, never to Linux</text>
<path d="M685 70 V132" stroke="#00F7FF" stroke-width="1.4" marker-end="url(#ix-ac)"/>
<text x="694" y="116" fill="#00F7FF" font-size="8.5">system calls, faults</text>
<rect x="490" y="192" width="386" height="196" rx="10" fill="rgba(0,247,255,.04)" stroke="rgba(0,247,255,.42)"/>
<text x="502" y="208" fill="#00F7FF" font-size="9" letter-spacing="1.4">KERNELET</text>
<rect x="502" y="216" width="362" height="46" rx="6" fill="url(#ix-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="683" y="236" fill="#8FF6FC" text-anchor="middle">kernel proper: Linux-compatible, safe Rust</text>
<text x="683" y="252" fill="#5C93A8" text-anchor="middle" font-size="8.5">source unchanged &#183; forbid(unsafe_code)</text>
<rect x="502" y="270" width="362" height="46" rx="6" fill="url(#ix-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="683" y="290" fill="#8FF6FC" text-anchor="middle">vOSTD: OSTD's API, virtualized</text>
<text x="683" y="306" fill="#5C93A8" text-anchor="middle" font-size="8.5">model page tables &#183; frame allocator &#183; tasks on virtual CPUs</text>
<rect x="502" y="324" width="362" height="52" rx="6" fill="rgba(6,10,36,.55)" stroke="rgba(0,247,255,.35)"/>
<text x="683" y="345" fill="#C9CCE0" text-anchor="middle">image (text shared by every instance) + data</text>
<text x="683" y="362" fill="#C9CCE0" text-anchor="middle">grant: memory from Linux, charged to the sandbox</text>
<path d="M683 174 V190" stroke="#00F7FF" stroke-width="1.4" marker-end="url(#ix-ac)"/>
<rect x="24" y="192" width="386" height="196" rx="10" fill="rgba(25,55,255,.12)" stroke="rgba(0,247,255,.5)"/>
<text x="36" y="208" fill="#00F7FF" font-size="9" letter-spacing="1.4">ENDOVISOR: ONE LOADABLE MODULE</text>
<g fill="rgba(6,10,36,.55)" stroke="rgba(0,247,255,.35)">
<rect x="36" y="216" width="176" height="34" rx="5"/><rect x="222" y="216" width="176" height="34" rx="5"/>
<rect x="36" y="258" width="176" height="34" rx="5"/><rect x="222" y="258" width="176" height="34" rx="5"/>
<rect x="36" y="300" width="176" height="34" rx="5"/><rect x="222" y="300" width="176" height="34" rx="5"/>
<rect x="36" y="342" width="362" height="34" rx="5"/>
</g>
<g fill="#8FF6FC" font-size="9.5" text-anchor="middle">
<text x="124" y="237">image loader</text><text x="310" y="237">carriers &#183; virtual interrupts</text>
<text x="124" y="279">service table (20 calls)</text><text x="310" y="279">page-fault handler</text>
<text x="124" y="321">eviction &#183; fault containment</text><text x="310" y="321">device models &#183; channels</text>
<text x="217" y="363">/dev/kernelet, for the runtime</text>
</g>
<path d="M500 293 H414" stroke="#00F7FF" stroke-width="1.4" marker-end="url(#ix-ac)"/>
<text x="456" y="286" fill="#00F7FF" font-size="8.5" text-anchor="middle">service</text>
<text x="456" y="306" fill="#00F7FF" font-size="8.5" text-anchor="middle">calls</text>
<path d="M396 70 V190" stroke="#9AA0BE" stroke-width="1.2" stroke-dasharray="4 3" marker-end="url(#ix-a)"/>
<text x="388" y="186" fill="#9AA0BE" font-size="8.5" text-anchor="end">ioctl, exec</text>
</g>
</svg>
<figcaption>Kernelets run in kernel mode, inside Linux, beside each other. What confines one is that everything above vOSTD is safe Rust, and that the only way out of its image is a table of twenty functions.</figcaption>
</figure>

## The idea in five sentences

A kernelet is a complete Linux-compatible kernel, written in safe Rust against a small framework interface, and it touches the machine only through that interface ([background](kernelets-in-brief.md)). Nothing in that arrangement says who implements the interface, so Linux can. A kernelet's processors are ordinary Linux tasks, its **virtual CPUs**, on which its own scheduler runs its own tasks; and a small patch to Linux's entry path, **the gate**, hands every system call made on one of them to the kernelet before Linux would look at it. The kernelet keeps its own page tables for its tenant's processes, and Linux's page tables are filled from them on demand, by a fault handler that checks every frame against what the kernelet owns. Everything else a kernel needs (timers, interrupts, memory, devices) maps onto what Linux already exports to modules.

## What it asks of Linux

| it asks for | which is |
|---|---|
| a patch | **the gate**: one pointer in the task structure, one flag bit, and two calls in the generic entry layer, which x86-64, RISC-V, s390 and LoongArch share; and **one helper function**, by which a task that is not a kernel thread changes its address space |
| exported symbols | **five**: `kernel_clone`, `mm_alloc`, `set_memory_rox`, `set_memory_rw`, `set_memory_ro` |
| a module | the **endovisor** |
| of the operator | three host settings: no panic on an oops, no per-fault lines in the kernel log, and the default core-dump policy; not a real-time kernel, and high-resolution timers active; and processors for latency-critical host work kept apart from sandboxes by `cpuset` |
| not needed | a boot parameter, hardware virtualization, any change to Linux's scheduler, any change in behavior for tasks outside a sandbox beyond a per-switch notifier test and one stray watch-timer firing after a carrier leaves a processor, while the module is loaded; nor a particular preemption model: where Linux does not preempt kernel code, the endovisor [reschedules kernelet code itself](virtualizing-ostd/tasks.md#yield) |

The details and the patch itself are on [the endovisor page](endovisor.md#patch). An unpatched Linux is ruled out, by function and not by speed: without the gate a tenant's second process would make its system calls to Linux.

## What a tenant gets

The three properties the book's boundary owes a tenant, as they stand on this host ([Boundaries and trust](principles.md) has the argument):

- **Safety**: held. The tenant's only kernel is its kernelet. No system call, by any entry instruction, from any thread or child, reaches Linux.
- **Fault containment**: held for the kernelet's own code. A kernelet that faults, panics, overflows its stack or loops forever ends, and only it ends. A failure inside a service call is a bug in the host, on this host as on the other.
- **Fairness**: held for processor time and memory, by Linux's own control groups, because every task and every page of a kernelet belongs to its sandbox's group. Interrupt-time work that a tenant's I/O induces is charged as Linux charges it for any process.

And one thing a container cannot give: **the tenant's kernel schedules the tenant's threads**. Scheduling has two levels ([Scheduling](virtualizing-ostd/scheduling.md)). Linux decides how much processor time the sandbox gets and when, by its control group's weight and limits; the kernelet's own scheduler, whatever policy the kernel proper implements, decides exactly which of its tasks use that time. Linux never sees more runnable tasks of a sandbox than a number fixed at its creation, its virtual CPUs and a few helpers, so nothing a kernelet's scheduler decides can cost a neighbor anything, and the two cooperate so that Linux does not take a virtual CPU away in the middle of a critical section, within a bound of about two milliseconds.

And what it costs: Linux's own correctness is in the trusted base, as it is for a container; and copying to or from tenant memory costs a software page-table walk where the other host pays nothing ([Memory](virtualizing-ostd/memory.md#copies) has the measurement).

## What has been built

A prototype of the central mechanisms, not the endovisor. On a Linux 6.12 with the gate patch, a module carrying a minimal vOSTD runs the Asterinas tree's 100-line example kernel **with its source byte-for-byte unchanged**: the kernel builds a tenant address space, runs a user program in it, services its `write` and `exit` system calls, and prints *Hello, world*. That exercises the gate, carriers and the root carrier, kernelet stacks, the model and the cache, and tenant copies by walking the model. A second small kernel on the same prototype shows demand paging through a real miss in the model, a tenant exception taken from Linux's signal queue, and **eviction**: a kernel that spins forever in kernel mode, which Linux alone could never kill, is removed and its sandbox reclaimed. A third, on the same Linux booted so that it does not preempt kernel code, shows a busy kernelet being made to share its processor and resuming with its computation intact. Those three ran first on an earlier form of the design, with a Linux task per kernelet task, and run unchanged on the final one. A fourth phase rebuilt the prototype around the [second-level scheduler](virtualizing-ostd/scheduling.md) and showed, against a reference model, a 1,100-line kernel's own strict-priority scheduler deciding every one of 1,842 task switches on two virtual CPUs while Linux saw three tasks; zero preemptions inside a critical section over 267 million lock acquisitions (with the bound in an earlier form; the final form is stricter and unmeasured); a neighbor's share unchanged within 1.2 % whether the sandbox ran one task or fifty; and a task switch of about 1,000 cycles. Earlier experiments established the shared-text loading scheme, the cost of the gate, fault recovery by die notifier, and the processor's refusal of direct tenant access. The [prototype page](prototype.md) says exactly what each run showed and what remains unverified: most importantly the device models, the multi-instance loader and the stack check, none of which has been built.

## How to read the labels

Every number says where it came from: *measured on the booted prototype*, *measured in a model*, *measured on the tree* (the Asterinas source), *estimated*, *arithmetic* or *chosen*. A claim the design leans on that nobody has tested is marked **[unverified]**.

Each page ends with what it decides. The identifiers there (D93, A27) point into the book's [design register](../../notes/design-register.md), one table of every decision (D) with the alternative it rejected, and every assumption (A) with its standing, across both hosts. *Kept* means a decision made for the Asterinas host holds here unchanged; *revised* means this chapter changed it. The register is an index; nothing in this chapter needs it to be understood.

## In this chapter

Background, for a reader new to either side:

- [Kernelets in brief](kernelets-in-brief.md): the two-layer kernel, API virtualization, and the vocabulary.
- [The Linux this chapter needs](background.md): tasks, the entry path, signals, address spaces, modules, program loaders, control groups.

The design, in the order of [Design for Asterinas](../asterinas-mode/index.md):

- [Boundaries and trust](principles.md): the parties, the interfaces, the threat model, the invariants.
- [Builds and images](builds-and-images.md): one source, one shared text, many instances; what the build checks.
- [The kernelet API: control half](kernelet-api-control.md): identity, configuration, the life cycle, the endovisor's records.
- [The kernelet API: service half](kernelet-api-service.md): the twenty services, and what each becomes on Linux.
- [Virtualizing OSTD](virtualizing-ostd/index.md): the map, and then the mechanisms.
  - [Memory](virtualizing-ostd/memory.md)
  - [Tasks, virtual CPUs, and carriers](virtualizing-ostd/tasks.md)
  - [Scheduling](virtualizing-ostd/scheduling.md)
  - [Interrupts and time](virtualizing-ostd/interrupts-and-time.md)
  - [User mode](virtualizing-ostd/user-mode.md)
  - [Devices](virtualizing-ostd/devices.md)
  - [Boot, power, panic, and the rest](virtualizing-ostd/the-rest.md)
- [Faults, termination, and reclamation](faults-and-reclamation.md): stopping a kernelet that will not cooperate, and giving everything back.
- [Channels](channels.md): vsock through a switch.
- [Zero-copy I/O](zero-copy-io.md): lending frames to Linux's block layer and sockets.
- [The endovisor](endovisor.md): the module, the patch, the device node, the life of a sandbox.
- [The kernelet runtime](kernelet-runtime.md): an OCI runtime over the endovisor.

And what stands behind it:

- [Alternatives considered](alternatives.md): what lost, and why.
- [The prototype](prototype.md): what has been verified by code.
