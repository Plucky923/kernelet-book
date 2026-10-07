# The kernelet API: control half

*The operations by which kernelets are created, fed, watched, killed and destroyed, and the records the endovisor keeps about each. When Asterinas is the host, this is an interface between two components. On Linux both sides are inside one module, so it is the endovisor's internal structure; it is specified here so that the life cycle and its bookkeeping can be checked.*

## Identity

A kernelet is named by a **kernelet identifier**: a slot number and a generation that is advanced every time the slot is reused. The pair never repeats, so a stale identifier held anywhere in the endovisor cannot come to name a later kernelet. Every table in the endovisor stores identifiers, virtual CPU numbers and physical addresses; none stores a pointer into a kernelet.

The endovisor has no names for a kernelet's tasks, because it never sees them: what it numbers are the kernelet's **virtual CPUs**. A machine holds at most 4,096 kernelets (*chosen*) and a kernelet at most 64 virtual CPUs. A kernelet's task count is bounded by its configuration all the same, because each task costs Linux the memory of a kernelet stack.

## Configuration

Given at create, completed by attaching devices before start, and fixed from start on, except memory, which can grow:

| field | meaning |
|---|---|
| kind | which registered image to instantiate |
| virtual CPUs | their number, 1 to 64; each is one [carrier](virtualizing-ostd/tasks.md#carriers) |
| initial and maximum grains | memory at start, and the ceiling `grains_request` may reach, in units of 2 MiB |
| maximum tasks | the bound on kernelet stacks in the sandbox's [pool](virtualizing-ostd/tasks.md#stacks), the boot stacks included, so at least the number of virtual CPUs; `kstack_alloc` fails beyond it |
| maximum models | the bound on tenant address spaces registered at once, each of which is a Linux address space; `pt_root_register` fails beyond it |
| command line | passed to the kernel proper |
| devices | each with a kind, a register-file size, a virtual interrupt line and the virtual CPU it is bound to |
| policy | the oops budget, the log rate in bytes per second, and the limit on channel connections |

Processor limits are *not* in the configuration. They belong to the sandbox's Linux control group, which the runtime sets up, because that is where Linux enforces them.

## The life cycle

A kernelet moves through six states, each transition a single compare-and-swap so that racing requests resolve cleanly:

<figure class="fwd-fig">
<div class="head">
<div class="tag">Life cycle</div>
<div class="title">Six states, one direction</div>
</div>
<svg viewBox="0 0 900 170" role="img" aria-label="A kernelet is Created by KERNELET_CREATE, becomes Running when its sandbox file is executed, becomes Dying on a stop, a kill, a fault or a lost carrier, becomes Exited when every task of the sandbox is gone, the root carrier and the device threads included, becomes Destroying on KERNELET_DESTROY, and becomes Destroyed when everything has been returned. A kill before start goes from Created straight to Exited.">
<defs>
<marker id="cl-a" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#00F7FF"/>
</marker>
<marker id="cl-g" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#9AA0BE"/>
</marker>
</defs>
<g font-family="ui-monospace,monospace" font-size="10.5">
<g fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)">
<rect x="20" y="60" width="110" height="40" rx="6"/><rect x="172" y="60" width="110" height="40" rx="6"/><rect x="324" y="60" width="110" height="40" rx="6"/><rect x="476" y="60" width="110" height="40" rx="6"/><rect x="628" y="60" width="110" height="40" rx="6"/>
</g>
<rect x="780" y="60" width="100" height="40" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<g fill="#8FF6FC" text-anchor="middle">
<text x="75" y="84">Created</text><text x="227" y="84">Running</text><text x="379" y="84">Dying</text><text x="531" y="84">Exited</text><text x="683" y="84">Destroying</text>
</g>
<text x="830" y="84" fill="#9AA0BE" text-anchor="middle">Destroyed</text>
<g stroke="#00F7FF" stroke-width="1.4" fill="none">
<path d="M130 80 H170" marker-end="url(#cl-a)"/><path d="M282 80 H322" marker-end="url(#cl-a)"/><path d="M434 80 H474" marker-end="url(#cl-a)"/><path d="M586 80 H626" marker-end="url(#cl-a)"/><path d="M738 80 H778" marker-end="url(#cl-a)"/>
</g>
<path d="M75 60 V30 H531 V58" stroke="#9AA0BE" stroke-width="1.2" fill="none" stroke-dasharray="4 3" marker-end="url(#cl-g)"/>
<g fill="#5C93A8" font-size="8.5" text-anchor="middle">
<text x="150" y="116">exec of the</text><text x="150" y="128">sandbox file</text>
<text x="303" y="116">stop, kill, fault,</text><text x="303" y="128">carrier lost</text>
<text x="455" y="116">every task of the</text><text x="455" y="128">sandbox gone</text>
</g>
<text x="303" y="24" fill="#9AA0BE" font-size="8.5" text-anchor="middle">kill before start</text>
<text x="607" y="52" fill="#5C93A8" font-size="8.5" text-anchor="middle">DESTROY</text>
<text x="759" y="52" fill="#5C93A8" font-size="8.5" text-anchor="middle">all returned</text>
</g>
</svg>
</figure>

- **create** checks the configuration, reserves a slot and an identifier, and builds the device table. It allocates nothing for the kernelet itself, because everything a sandbox costs must be charged to its control group, and no member of that group exists until the sandbox file is executed.
- **start** happens inside the [program loader](virtualizing-ostd/tasks.md#root), on the process that becomes the root carrier: load the [instance](builds-and-images.md), build the shared pages, make the initial grant, and leave the rest to the root carrier, which clones one carrier per virtual CPU and starts the device threads. The carrier of virtual CPU 0 enters the image; the others wait for `vcpu_boot`.
- **grant** adds grains, zeroed, recorded in the owner array and then published in the grant table. The kernelet asks with `grains_request`, which is answered at once: granted up to the ceiling, refused beyond it. The runtime can raise the ceiling or push memory with `KERNELET_GRANT`, and a push needs no announcement: it appears in the info page's count of runs, which vOSTD's allocator reads before it asks for more. Memory only grows while a kernelet lives.
- **raise an interrupt** is one atomic operation and a kick of the virtual CPU the line is bound to, legal from any Linux context.
- **kill** marks the kernelet dying and returns at once; [stopping the carriers](faults-and-reclamation.md) is asynchronous.
- **wait** reports the **exit status**: exited with a code, panicked with a message, or killed with a reason.
- **destroy** releases everything, in the [fixed order](faults-and-reclamation.md#destroy) that the book calls the *drain list*: one step for every record below.

## What the endovisor keeps per kernelet

The list matters because destroy must account for every entry, and because a structure missing from it is where a use-after-free would hide.

- identity, state, and a count of operations in progress (destroy waits for it to reach zero);
- the kind, the configuration, the instance's base address and its private pages;
- the grant: each run's physical base, length and Linux page handle; the metadata region;
- the registered models (tenant page-table roots), each with its file object, its Linux address space and its reader-writer lock;
- the carriers, one per virtual CPU: for each, the Linux task, the lifeline, the service-call depth, the saved stack pointers, Linux's preemption count as it was on entry and the kernelet's increment remembered across a service call, the time of its last real scheduling event, the activated model, the pending exception, the `user_run` context pointer, the *leaving* and *do not come back* marks, the yield stub's saved instruction pointer, the preemption notifier, registered on the carrier and unregistered as it leaves for good; the floating-point staging buffer;
- the virtual CPUs' shared records;
- the pool of kernelet stacks, boot stacks included, and which of them are out;
- the devices: model state, inbox, device thread, the Linux file behind it;
- the channel connections that name this kernelet;
- the log ring and the statistics;
- the exit status and those waiting for it.

Two tables are machine-wide: the **slot table**, from slot to kernelet and generation, and the **owner array**, from each 2 MiB of physical memory to the kernelet that owns it, if any.

## What this page decides

- **Every endovisor table names kernelets by generation-stamped identifiers, never by pointer**, as on the other host, so that reuse cannot alias.
- **Memory is granted at start, not at create** ([register D106](../../notes/design-register.md#decisions)), so that it is charged to the sandbox's control group from the first page.
- **Processor limits are the control group's, not the configuration's** ([register D107](../../notes/design-register.md#decisions)). The Asterinas host needs its own throttle because its scheduler has no groups; Linux's has.
