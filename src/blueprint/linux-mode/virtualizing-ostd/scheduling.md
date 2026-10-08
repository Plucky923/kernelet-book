# Scheduling

*How a kernelet's own scheduler decides which of its tasks run, while Linux decides only how much processor the sandbox gets. This page replaces the earlier rule that a kernelet's scheduler is inert. It discharges the processor half of fairness, and it is where the design meets the oldest problem of running one scheduler on top of another.*

## Two schedulers, and who decides what

The kernel proper contains a complete scheduler: run queues per CPU, priorities and `nice` values, real-time classes, time slices, load balancing. OSTD lets a kernel supply one with `inject_scheduler`, and calls it to *enqueue* a task that became runnable, to *pick the next* task for a CPU, and to *update* the running task's accounting on every timer tick.

There are two ways to honor that scheduler inside a sandbox, and they differ in who has the last word.

In the first, every kernelet task is a task of the host, and the host's scheduler runs them all. The kernelet's scheduler is compiled in and never asked. That is simple, and it is what this chapter specified until now. Its price is that a tenant gets the *host's* idea of scheduling: a tenant's real-time thread is not real-time, its `nice` values mean what Linux's fair class says they mean among however many other threads it has, and the kernel proper's carefully written policy is dead code.

In the second, there are **two levels**. Linux schedules *virtual CPUs*, a handful per sandbox, and knows nothing else about the sandbox. On each virtual CPU, the kernelet's own scheduler decides which kernelet task runs, for how long, and what preempts what. This page specifies the second.

<figure class="fwd-fig">
<div class="head">
<div class="tag">Two levels</div>
<div class="title">Linux decides how much processor a sandbox gets; the kernelet decides who inside it runs</div>
</div>
<svg viewBox="0 0 900 360" role="img" aria-label="Two levels of scheduling. At the bottom, Linux's scheduler shares four processors among the control groups of the machine by weight and limit: sandbox A, sandbox B, and the host's own workloads. Each sandbox appears to Linux as a small fixed number of Linux tasks, its virtual CPU carriers: two for sandbox A, however many tasks run inside. At the top, inside sandbox A, the kernelet's own scheduler keeps a run queue per virtual CPU holding many kernelet tasks, tenant threads and kernel threads, and picks which one runs on each virtual CPU, by its own priorities and time slices. Linux never sees those tasks.">
<defs>
<linearGradient id="sc-cg" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="#00F7FF" stop-opacity=".22"/>
<stop offset="100%" stop-color="#1937FF" stop-opacity=".22"/>
</linearGradient>
<marker id="sc-ac" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#00F7FF"/>
</marker>
<marker id="sc-a" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#9AA0BE"/>
</marker>
</defs>
<g font-family="ui-monospace,monospace" font-size="10">
<rect x="20" y="16" width="560" height="176" rx="10" fill="rgba(0,247,255,.04)" stroke="rgba(0,247,255,.42)"/>
<text x="32" y="34" fill="#00F7FF" font-size="9" letter-spacing="1.4">LEVEL 2 &#183; INSIDE SANDBOX A &#183; THE KERNELET'S OWN SCHEDULER</text>
<rect x="36" y="46" width="256" height="92" rx="6" fill="url(#sc-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="48" y="64" fill="#8FF6FC">run queue of virtual CPU 0</text>
<g fill="rgba(6,10,36,.55)" stroke="rgba(0,247,255,.35)">
<rect x="48" y="74" width="54" height="22" rx="4"/><rect x="108" y="74" width="54" height="22" rx="4"/><rect x="168" y="74" width="54" height="22" rx="4"/><rect x="228" y="74" width="54" height="22" rx="4"/>
<rect x="48" y="104" width="54" height="22" rx="4"/><rect x="108" y="104" width="54" height="22" rx="4"/>
</g>
<g fill="#C9CCE0" font-size="8.5" text-anchor="middle">
<text x="75" y="89">rt 50</text><text x="135" y="89">nice 0</text><text x="195" y="89">nice 0</text><text x="255" y="89">nice 10</text><text x="75" y="119">kthread</text><text x="135" y="119">nice 19</text>
</g>
<rect x="308" y="46" width="256" height="92" rx="6" fill="url(#sc-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="320" y="64" fill="#8FF6FC">run queue of virtual CPU 1</text>
<g fill="rgba(6,10,36,.55)" stroke="rgba(0,247,255,.35)">
<rect x="320" y="74" width="54" height="22" rx="4"/><rect x="380" y="74" width="54" height="22" rx="4"/><rect x="440" y="74" width="54" height="22" rx="4"/>
</g>
<g fill="#C9CCE0" font-size="8.5" text-anchor="middle">
<text x="347" y="89">nice -5</text><text x="407" y="89">nice 0</text><text x="467" y="89">kthread</text>
</g>
<text x="164" y="160" fill="#5C93A8" text-anchor="middle" font-size="8.5">picks one: by ITS priorities, slices, preemption</text>
<text x="436" y="160" fill="#5C93A8" text-anchor="middle" font-size="8.5">picks one</text>
<path d="M164 166 V212" stroke="#00F7FF" stroke-width="1.4" marker-end="url(#sc-ac)"/>
<path d="M436 166 V212" stroke="#00F7FF" stroke-width="1.4" marker-end="url(#sc-ac)"/>
<text x="300" y="184" fill="#6A6F8C" text-anchor="middle" font-size="8.5">Linux never sees these tasks</text>
<rect x="600" y="16" width="130" height="176" rx="10" fill="rgba(0,247,255,.04)" stroke="rgba(0,247,255,.30)"/>
<text x="665" y="34" fill="#5C93A8" font-size="9" text-anchor="middle" letter-spacing="1.2">SANDBOX B</text>
<text x="665" y="104" fill="#6A6F8C" text-anchor="middle" font-size="8.5">its own scheduler,</text>
<text x="665" y="118" fill="#6A6F8C" text-anchor="middle" font-size="8.5">its own tasks</text>
<path d="M665 166 V212" stroke="rgba(0,247,255,.5)" stroke-width="1.4" marker-end="url(#sc-ac)"/>
<rect x="20" y="214" width="860" height="130" rx="10" fill="rgba(255,255,255,.025)" stroke="rgba(255,255,255,.12)"/>
<text x="32" y="232" fill="#9A9DB0" font-size="9" letter-spacing="1.4">LEVEL 1 &#183; LINUX'S SCHEDULER &#183; CONTROL GROUPS SHARE THE PROCESSORS BY WEIGHT AND LIMIT</text>
<rect x="36" y="244" width="256" height="40" rx="6" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="164" y="268" fill="#00F7FF" text-anchor="middle">vCPU 0 carrier (a Linux task)</text>
<rect x="308" y="244" width="256" height="40" rx="6" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="436" y="268" fill="#00F7FF" text-anchor="middle">vCPU 1 carrier (a Linux task)</text>
<rect x="600" y="244" width="130" height="40" rx="6" fill="rgba(25,55,255,.16)" stroke="rgba(0,247,255,.35)"/>
<text x="665" y="268" fill="#5C93A8" text-anchor="middle">B's carriers</text>
<rect x="750" y="244" width="114" height="40" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="807" y="268" fill="#9AA0BE" text-anchor="middle">host workloads</text>
<text x="300" y="306" fill="#C9CCE0" text-anchor="middle" font-size="9">sandbox A's control group: weight 100</text>
<text x="665" y="306" fill="#C9CCE0" text-anchor="middle" font-size="9">B: weight 100</text>
<text x="807" y="306" fill="#C9CCE0" text-anchor="middle" font-size="9">others</text>
<text x="450" y="332" fill="#6A6F8C" text-anchor="middle" font-size="9">A gets the same share whether it runs one task or five hundred, at whatever priorities</text>
</g>
</svg>
</figure>

## What the design has to achieve

Four things, and they pull against each other.

- **Exact.** Among a kernelet's own tasks, its scheduler's decisions hold exactly: which task runs on which virtual CPU, that a higher-priority wake-up preempts, that a time slice ends when the policy says. Linux never sees more runnable tasks of a sandbox than a number fixed when it is created: its virtual CPUs, its root carrier and its device threads.
- **Efficient.** Switching between two tasks of a sandbox should cost about what it costs Linux to switch between two threads of a control group, or less.
- **Fair.** Nothing a kernelet's scheduler does, by design, bug or malice, changes what another sandbox or the host receives.
- **Cooperative.** Linux must not take the processor from a virtual CPU at a moment that hurts the kernelet out of proportion, above all while the running task holds a spin lock. This is the classic disease of two-level scheduling, known from virtual machines as **lock-holder preemption**: the host deschedules the virtual CPU that holds a lock, and the guest's other virtual CPUs burn their time slices spinning on a lock whose holder is not running.

## The designs considered

Six families were explored; the losers are described, with their reasons, under [Alternatives considered](../alternatives.md#scheduling). In one table:

| design | exact | efficient | fair | cooperative | asks of Linux |
|---|---|---|---|---|---|
| translate the kernelet's decisions into Linux's `nice`, affinity and real-time parameters | no: only policies Linux can express, approximately | yes | real-time classes escape the group's share | no | nothing |
| keep a carrier per task, but let only the tasks the kernelet picked be runnable (the rest sleep) | yes | no: every switch is a Linux wake-up plus a Linux sleep, in both schedulers | yes | no | nothing |
| a BPF scheduler in Linux (`sched_ext`), fed the kernelet's choices through maps | yes | one Linux switch per switch | only if the BPF program reimplements group fairness | no | the machine's one BPF scheduler slot, for every tenant and the host |
| a hook in Linux's scheduler core that asks the kernelet which task to pick | yes | yes | yes | no | scheduler-core surgery; and it would run kernelet policy under Linux's run-queue lock |
| co-schedule all virtual CPUs of a sandbox at once (gang scheduling) | (not a policy mechanism) | wastes processors | yes | yes, by brute force | a scheduling class Linux does not have |
| **a carrier per virtual CPU; the kernelet multiplexes its own tasks on it** | **yes** | **a switch does not involve Linux at all** | **yes, by construction** | **yes, with the protocol below** | **two small helpers and a configuration option selected** |

The winner is the last. It is also the oldest idea on the list: it is what a virtual machine does, and what *scheduler activations* proposed for user-level threads in 1991, namely that the lower scheduler hands out processors and tells the upper one what happens to them. What is particular here is that the "virtual CPU" is an ordinary Linux task, that the upper scheduler is kernel code in the same address space, and that the two can therefore talk through a few words of shared memory at no cost.

## A carrier carries a virtual CPU {#vcpu}

**A [carrier](tasks.md#carriers) carries one virtual CPU**, for the life of the sandbox, and not one kernelet task. A sandbox configured with *N* virtual CPUs has *N* carriers, cloned from the root carrier when it starts, and never any more.

Kernelet tasks stop being Linux's business altogether. A task is OSTD's own object, with its own [kernelet stack](tasks.md#stacks), exactly as on a machine. OSTD's task layer, its context switch, its wait queues and its scheduler interface are the *same code* in vOSTD as on bare metal; what vOSTD virtualizes is one level lower, the CPU:

| what OSTD does on a machine | what vOSTD does on Linux |
|---|---|
| start the other processors at boot | the service `vcpu_boot(i)` lets carrier *i* enter the image |
| switch tasks: save and load callee-saved registers and the stack pointer | identical; Linux is not involved and does not notice |
| idle a processor with nothing to run (`hlt`) | the service `vcpu_idle(deadline)`: the carrier sleeps in Linux until something is pending |
| tell another processor to reschedule (an inter-processor interrupt) | the service `vcpu_kick(i)` |
| take a timer interrupt every tick | a [virtual interrupt](#upcall), from the endovisor |
| run with interrupts or preemption disabled | a counter in the virtual CPU's record, which [Linux honors](#cooperative) |

So when the kernel proper's scheduler decides that task B should replace task A on virtual CPU 0, vOSTD saves A's registers, loads B's, and B runs: on the same carrier, in the same Linux time slice, in a few dozen nanoseconds. If B is a tenant thread of a different process than the one last in user mode on this carrier, the carrier also [adopts that process's Linux address space](memory.md#space) on its way to user mode, and the kernel proper saves and restores floating-point state as it does on a machine, through OSTD's `FpuContext`, which vOSTD backs with two services.

What Linux sees of a sandbox is *N* carriers, a root carrier and a few device threads in one control group, a number fixed at creation. That is the whole interface between the two schedulers in the downward direction, and it is what makes fairness an argument and not a mechanism ([below](#fair)).

## Virtual interrupts {#upcall}

A scheduler needs to be *interrupted*: by the tick that ends a time slice, by the wake-up on another CPU that makes a higher-priority task runnable here. A kernelet has no interrupts. The endovisor gives it virtual ones, delivered into whatever the virtual CPU is doing.

Each virtual CPU has a small **record**, shared between the endovisor and vOSTD. The endovisor sets bits in its `pending` word: TICK, KICK, and LINES for the [virtual device lines](interrupts-and-time.md). Two more fields reproduce the two states in which a machine holds things off, and they are kept apart because OSTD keeps them apart. `irq_off` is the virtual CPU's interrupt flag, inverted: vOSTD sets it, and later restores it, where OSTD's interrupts-off guard would clear and restore the processor's flag; and the endovisor sets it as it delivers an upcall, as a processor turns interrupts off when it takes one. While it is set no virtual interrupt is delivered. `guards` is the count of preemption guards and spin locks held, which vOSTD keeps in the record (only the count: OSTD's own per-CPU word also carries its "preemption wanted" flag, which stays where it is). It does not hold interrupts off, on a machine or here: a handler may run on top of a task that holds a preemption guard; what must not happen is a task switch. This chapter says a virtual CPU is in a **critical section** when either is non-zero.

The source of ticks is the [watch timer](tasks.md#watch): one Linux timer per processor, armed by a carrier's preemption notifier whenever Linux puts the carrier on that processor, which fires after every millisecond of the carrier's own run time, in hard-interrupt context. Each time it fires on a virtual CPU's carrier it sets TICK, and then delivers according to what it interrupted:

<figure class="fwd-fig">
<div class="head">
<div class="tag">The life of a scheduling decision</div>
<div class="title">A tick ends a time slice: the kernelet's scheduler runs, and Linux only watches</div>
</div>
<svg viewBox="0 0 900 300" role="img" aria-label="Task A is running on virtual CPU 0. One: the watch timer fires on that processor and sets the TICK bit in the virtual CPU's record. Two: if A was in user mode, the endovisor flags the carrier, which re-enters the kernelet through the gate; if A was in kernelet code with virtual interrupts on, the endovisor points the interrupted frame at the image's upcall stub. Three: in the kernelet, the upcall saves A's registers on A's own kernelet stack and runs the tick handler, which calls the kernel proper's scheduler: update current, then pick next. Four: the scheduler picks task B; vOSTD switches stacks from A to B. Five: B runs on the same carrier; A waits in the kernelet's run queue with its interrupted state on its own stack, and will resume from there on whichever virtual CPU next picks it.">
<defs>
<linearGradient id="sd-cg" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="#00F7FF" stop-opacity=".22"/>
<stop offset="100%" stop-color="#1937FF" stop-opacity=".22"/>
</linearGradient>
<marker id="sd-ac" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#00F7FF"/>
</marker>
</defs>
<g font-family="ui-monospace,monospace" font-size="9.5">
<rect x="20" y="20" width="200" height="120" rx="8" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="32" y="40" fill="#9A9DB0" font-size="9" letter-spacing="1.2">1 LINUX &#183; WATCH TIMER</text>
<text x="32" y="64" fill="#C9CCE0">fires on this processor,</text>
<text x="32" y="80" fill="#C9CCE0">in hard-interrupt context;</text>
<text x="32" y="96" fill="#C9CCE0">interrupted: vCPU 0's carrier,</text>
<text x="32" y="112" fill="#C9CCE0">running task A</text>
<rect x="240" y="20" width="200" height="120" rx="8" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="252" y="40" fill="#00F7FF" font-size="9" letter-spacing="1.2">2 ENDOVISOR &#183; DELIVER</text>
<text x="252" y="64" fill="#C9CCE0">set TICK in the record</text>
<text x="252" y="84" fill="#8FF6FC">A in user mode: flag the</text>
<text x="252" y="98" fill="#8FF6FC">carrier; it returns by the gate</text>
<text x="252" y="116" fill="#8FF6FC">A in kernelet code, irqs on:</text>
<text x="252" y="130" fill="#8FF6FC">saved ip := the upcall stub</text>
<rect x="460" y="20" width="200" height="120" rx="8" fill="url(#sd-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="472" y="40" fill="#8FF6FC" font-size="9" letter-spacing="1.2">3 vOSTD &#183; THE UPCALL</text>
<text x="472" y="64" fill="#C9CCE0">save A's registers on A's</text>
<text x="472" y="80" fill="#C9CCE0">own kernelet stack</text>
<text x="472" y="100" fill="#C9CCE0">run the tick handler:</text>
<text x="472" y="116" fill="#C9CCE0">scheduler.update_current(Tick)</text>
<rect x="680" y="20" width="200" height="120" rx="8" fill="url(#sd-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="692" y="40" fill="#8FF6FC" font-size="9" letter-spacing="1.2">4 KERNEL PROPER'S SCHEDULER</text>
<text x="692" y="64" fill="#C9CCE0">A's slice is over</text>
<text x="692" y="84" fill="#C9CCE0">pick_next() = task B</text>
<text x="692" y="108" fill="#5C93A8" font-size="8.5">its policy, its run queues;</text>
<text x="692" y="122" fill="#5C93A8" font-size="8.5">Linux is not consulted</text>
<rect x="240" y="180" width="640" height="100" rx="8" fill="rgba(0,247,255,.04)" stroke="rgba(0,247,255,.42)"/>
<text x="252" y="200" fill="#00F7FF" font-size="9" letter-spacing="1.2">5 vOSTD &#183; SWITCH, AND CARRY ON</text>
<text x="252" y="224" fill="#C9CCE0">switch stacks from A to B: B runs now, on the same carrier, in the same Linux time slice</text>
<text x="252" y="244" fill="#C9CCE0">A waits in the kernelet's run queue; its interrupted registers and instruction pointer are on its</text>
<text x="252" y="260" fill="#C9CCE0">own stack, so whichever virtual CPU picks it next resumes it exactly where the tick found it</text>
<rect x="20" y="180" width="200" height="100" rx="8" fill="rgba(255,255,255,.025)" stroke="rgba(255,255,255,.12)"/>
<text x="32" y="200" fill="#9A9DB0" font-size="9" letter-spacing="1.2">IF IRQS OFF</text>
<text x="32" y="222" fill="#C9CCE0" font-size="9">the bit stays pending;</text>
<text x="32" y="238" fill="#C9CCE0" font-size="9">vOSTD delivers it itself</text>
<text x="32" y="254" fill="#C9CCE0" font-size="9">when irqs go back on</text>
<g stroke="#00F7FF" stroke-width="1.4" fill="none">
<path d="M220 80 H238" marker-end="url(#sd-ac)"/><path d="M440 80 H458" marker-end="url(#sd-ac)"/><path d="M660 80 H678" marker-end="url(#sd-ac)"/><path d="M780 140 V178" marker-end="url(#sd-ac)"/>
</g>
</g>
</svg>
</figure>

- **In user mode.** The endovisor [flags the carrier](user-mode.md#gate); on its way back to user mode the gate's resume hook re-enters the kernelet, `user_run` returns *look for events*, and vOSTD's `execute` loop handles what is pending and then lets the scheduler preempt, which is what it does after a timer interrupt on a machine.
- **In kernelet code, with virtual interrupts on.** The endovisor pushes the interrupted instruction pointer onto the interrupted stack, as a processor does when it takes an interrupt, sets `irq_off`, and points the interrupted frame at the image's **upcall stub**, by the same means as [eviction](../faults-and-reclamation.md#eviction). The stub, which is vOSTD's, pops that pointer, realigns the stack (an interrupt lands between any two instructions), pushes the pointer back with every register and the flags onto the *current task's* kernelet stack, and runs the handlers for what is pending. Then it does what a machine's interrupt return should do for a kernel that preempts: it clears `irq_off`, looks at `pending` once more, and, if the scheduler now wants another task and `guards` is zero, switches; with a guard held it returns, and the switch happens when the guard drops. The stub also keeps the [mirror](#cooperative) honest, by the rule given there: an upcall is a critical section, and the stub performs the increment on entry if none is outstanding, and the decrement on exit if no guard is left; the endovisor's own setting of `irq_off` never touches Linux's count. Because the interrupted state is on the task's own stack, which nothing but this kernelet's code can reach, the preempted task can resume later on any virtual CPU, by popping what the stub pushed.
- **In kernelet code, with virtual interrupts off** (an interrupts-off guard, or an upcall in progress). Nothing is redirected. The bit stays pending, and vOSTD delivers it itself when it turns virtual interrupts back on, which costs one load and a branch on that path.
- **In kernelet code, deep in a stack.** The handlers will run on the interrupted task's kernelet stack, so the endovisor redirects only if the interrupted stack pointer is at least 32 KiB above the bottom of the stack it is on. It finds that stack in its own [pool](tasks.md#stacks) records, by the pointer's value, and redirects nothing if the pointer is in no stack it knows; it does not read the kernelet's `stack_limit` for this, since a stale or wrong value there could send the handlers onto Linux's reserve. The margin is the [16 KiB that belong to Linux](../faults-and-reclamation.md#stack) and 16 KiB (*chosen*) for the handlers. Otherwise the bit waits, as if virtual interrupts were off. A task that is that deep is about to be [stopped for it](../faults-and-reclamation.md#stack) or about to return, and a tick that lands there is not a reason to kill it.
- **Anywhere else** (inside a service call, in the yield stub, in Linux's code): pending until the carrier is back in the kernelet. vOSTD's wrapper around a service call tests `pending` when the call returns and, if no guard is held, handles it by an ordinary function call; after the yield stub, the next watch tick finds it.

A virtual CPU that Linux has descheduled receives no ticks while it is off the processor. Nothing in a kernelet counts ticks, so nothing runs slow: the kernel proper's scheduler accounts run time from the timestamp counter and its timers from the clock ([Interrupts and time](interrupts-and-time.md)). What the kernelet's scheduler does *not* know by itself is that the time was taken from it: a task that was current while Linux ran something else is charged for that time, as in a guest that ignores *steal time*. The record carries the stolen total, `stolen_ns`, for a kernel proper that wants to correct for it; OSTD's scheduler interface has no place for it today, and the design does not add one.

`vcpu_kick(i)` is the inter-processor interrupt. OSTD calls it where a machine would send a reschedule interrupt, typically when `enqueue` places a newly runnable task on another CPU's queue. The endovisor sets KICK and then does the least that will get it seen promptly, which depends on where carrier *i* is, as the endovisor's own carrier record says (idle, in user mode, in the kernelet, in a service); it does not read the target's shared record, whose non-atomic fields are written and read only by the target's own carrier: asleep in `vcpu_idle`, it is woken; in user mode, it is [flagged](user-mode.md#gate), which interrupts its processor; running kernelet code *and on a processor*, which the endovisor knows from the carrier's preemption notifier, that processor is sent a cross-processor call of the kind that may be sent from any context, with a request block preallocated per virtual CPU ([`smp_call_function_single_async()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/smp.c#L710); the synchronous kind is not legal from a hardware interrupt handler, where a device model may raise a line); the call, on arrival, checks once more that the carrier is still current there, and only then makes the watch timer fire at once and deliver the upcall; a carrier that is runnable but off a processor is sent nothing, and finds the bit when it is next switched in, since the notifier fires the watch timer at once when `pending` is non-zero. So the interrupt a kick costs lands, with a small window of doubt, on the sandbox's own carrier, and at most one such call is in flight per virtual CPU: if the request block is still queued (two virtual CPUs kicking one target) the kick leaves the bit and sends nothing, which is harmless since the bit is set, and the firing it asks for re-arms the timer for the remainder of the current period, not a fresh one; anywhere else, the bit is found on the way back into the kernelet. A kick whose bit is already set does nothing, so a thousand kicks cost one delivery. A higher-priority task that wakes on another virtual CPU therefore preempts within microseconds (*estimated*) unless that virtual CPU is in a critical section, and then when the section ends, as on a machine.

**What OSTD must gain.** Two preemption points, both worth having on a machine too. Today OSTD lets the scheduler preempt a task when it returns from user mode, when it halts an idle CPU, and when it starts a new task (*measured on the tree*: those are the callers of `might_preempt()`; a yield takes its own path); a kernel task that computes is never preempted, and dropping the last guard only decrements the count. The design needs (a) **preemption of kernel-mode code when an interrupt has been handled**, in task context, with interrupts back on and no guard held, which is the upcall's last step and which vOSTD's stub supplies; a bare-metal OSTD would need the same at the end of its own interrupt return, which the design does not ask of it; and (b) **a check when a critical section ends**, that is, when the guard count reaches zero with interrupts on or when interrupts are turned back on with no guard held, for a preemption that became due meanwhile; that is also where Linux is given the processor it [was waiting for](#cooperative). The check must not fire while interrupts are off, since OSTD's context switch refuses to run then. A third, smaller addition is on the [Interrupts and time](interrupts-and-time.md) page: a way for `halt_cpu()` to learn when the next timer expires. Everything else above is a new body for something OSTD already has, and the `Scheduler` and `LocalRunQueue` traits are untouched.

## Sharing the processor with Linux, at convenient moments {#cooperative}

Linux will preempt a carrier whenever its own policy says so: the sandbox's time slice in its control group is over, a host task with a better claim woke up. If that happens while the kernelet task on that carrier holds a kernelet spin lock, every other virtual CPU that wants the lock spins until Linux runs the first one again. That is lock-holder preemption, and hypervisors have fought it for twenty years with two families of remedy: *tell the host when not to preempt*, and *make waiting cheap when it preempted anyway*. The design uses both, the first as the rule and the second as the backstop.

<figure class="fwd-fig">
<div class="head">
<div class="tag">Cooperating</div>
<div class="title">Linux's preemption waits for the kernelet's critical section, for a bounded time</div>
</div>
<svg viewBox="0 0 900 250" role="img" aria-label="A timeline of one virtual CPU. The kernelet task takes a spin lock: vOSTD raises its guard count and, with the same instruction Linux uses, Linux's own preemption count, by one. While the lock is held, Linux decides the carrier should be preempted and marks it; because the count is raised, Linux does not preempt. The task releases the lock: the count drops to zero, vOSTD sees that Linux is waiting and calls the vcpu_yield service at once, and Linux switches to another task. A second, lower timeline shows a task that overstays: the watch timer finds it 2 ms of run time past its last real scheduling event and forces the yield, whatever the record says.">
<defs>
<marker id="co-ac" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#00F7FF"/>
</marker>
</defs>
<g font-family="ui-monospace,monospace" font-size="9.5">
<text x="20" y="24" fill="#00F7FF" font-size="9" letter-spacing="1.4">THE RULE</text>
<path d="M20 70 H880" stroke="rgba(255,255,255,.28)" stroke-width="1"/>
<rect x="60" y="52" width="120" height="36" rx="4" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="120" y="74" fill="#9AA0BE" text-anchor="middle">kernelet code</text>
<rect x="180" y="52" width="300" height="36" rx="4" fill="rgba(25,55,255,.30)" stroke="rgba(0,247,255,.55)"/>
<text x="330" y="68" fill="#8FF6FC" text-anchor="middle">holding a spin lock</text>
<text x="330" y="82" fill="#5C93A8" text-anchor="middle" font-size="8.5">guards &gt; 0, and Linux's preemption count raised by one</text>
<rect x="480" y="52" width="70" height="36" rx="4" fill="rgba(0,247,255,.16)" stroke="rgba(0,247,255,.55)"/>
<text x="515" y="74" fill="#00F7FF" text-anchor="middle">yield</text>
<rect x="550" y="52" width="200" height="36" rx="4" fill="rgba(255,255,255,.03)" stroke="rgba(255,255,255,.12)" stroke-dasharray="4 3"/>
<text x="650" y="74" fill="#6A6F8C" text-anchor="middle">Linux runs someone else</text>
<path d="M330 30 V50" stroke="#9AA0BE" stroke-width="1.2" marker-end="url(#co-ac)"/>
<text x="338" y="40" fill="#9AA0BE" font-size="8.5">Linux wants the processor: it marks the carrier, and waits</text>
<text x="480" y="106" fill="#00F7FF" font-size="8.5" text-anchor="middle">lock released: count reaches zero with Linux waiting</text>
<text x="480" y="119" fill="#00F7FF" font-size="8.5" text-anchor="middle">vOSTD calls vcpu_yield() at once</text>
<text x="20" y="156" fill="#00F7FF" font-size="9" letter-spacing="1.4">THE BOUND</text>
<path d="M20 200 H880" stroke="rgba(255,255,255,.28)" stroke-width="1"/>
<rect x="60" y="182" width="560" height="36" rx="4" fill="rgba(25,55,255,.30)" stroke="rgba(0,247,255,.55)"/>
<text x="340" y="204" fill="#8FF6FC" text-anchor="middle">a task that stays in the kernelet for too long, guard or no guard</text>
<rect x="620" y="182" width="90" height="36" rx="4" fill="rgba(0,247,255,.16)" stroke="rgba(0,247,255,.55)"/>
<text x="665" y="204" fill="#00F7FF" text-anchor="middle">forced yield</text>
<g stroke="#9AA0BE" stroke-width="1.2">
<path d="M260 170 V182"/><path d="M440 170 V182"/><path d="M620 170 V182"/>
</g>
<g fill="#9AA0BE" font-size="8.5" text-anchor="middle">
<text x="260" y="166">last real scheduling event</text><text x="440" y="166">tick: 1 ms run since</text><text x="620" y="166">tick: 2 ms run since</text>
</g>
<text x="450" y="238" fill="#6A6F8C" text-anchor="middle" font-size="9">two watch periods of grace, about 2 ms; the extra time is charged to the sandbox like any other</text>
</g>
</svg>
</figure>

**The rule: the kernelet's guards are Linux's guards.** Linux keeps, per processor, a *preemption count*; while it is non-zero Linux does not preempt the running task, and Linux's own [`preempt_disable()`](https://elixir.bootlin.com/linux/v6.12/source/include/linux/preempt.h#L213) is nothing but an increment of it (on a kernel built without preemption debugging or tracing; with either, Linux's own increments go through a function that also records where they happened, which the mirror's do not, so such a kernel's reports attribute a carrier's raised count to nobody). When the kernel proper enters a [critical section](#upcall), by taking a preemption guard, a spin lock or an interrupts-off guard while it held none, vOSTD *executes that same increment*, once, on Linux's counter, whose `%gs`-relative location the endovisor publishes in the boot arguments ([`pcpu_hot`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/current.h#L15) is exported; vOSTD reaches it only by a `%gs`-relative instruction, never by an address it computed earlier, since the carrier may have changed processors). When the section ends it executes the matching decrement. On x86 Linux folds "a reschedule is wanted" into that same word, inverted, so the decrement's zero flag says *the count is zero and Linux is waiting for this processor*; when it does, vOSTD calls the service `vcpu_yield()`, which reschedules in Linux. This is, instruction for instruction, what Linux's own `preempt_enable()` does. Precisely: vOSTD keeps a private bit, *mirrored*, in the record; when a critical section begins and the bit is clear, vOSTD increments Linux's count and then sets it; when a critical section ends and the bit is set, vOSTD clears it and then performs the decrement-and-test. Both operations are idempotent against the bit, and the bit is written on the far side of each instruction so that it never claims more than the count holds: OSTD's guard lifetimes cross a context switch (`switch_to_task` takes a guard that the *next* task releases), so the pairing holds across tasks and virtual CPUs rather than within one function, and a first build that relied on pairing alone took Linux's count below zero (*found by the prototype*). The few instructions of a guard operation run with `irq_off` set, so that no upcall can be delivered between the record's fields and Linux's count changing; whatever became pending meanwhile is delivered as the guard operation ends. The upcall stub uses the same bit: entered with the bit clear it is the beginning of a critical section and increments; as it clears `irq_off`, if no guard is held and the bit is set, it decrements. The endovisor's own setting of `irq_off` at a redirect never touches Linux's count.

The mirror is one increment however deep the kernelet's guards nest, and that is deliberate. Linux's count is a bit field: eight bits of preemption depth, and above them the fields that say "in a softirq", "in a hard interrupt", "in an NMI" ([`preempt.h`](https://elixir.bootlin.com/linux/v6.12/source/include/linux/preempt.h#L33)). A kernel proper that nested 256 guards, which safe Rust can do, would otherwise carry into them and make Linux misjudge its own context on that processor.

The effect is that Linux defers an involuntary preemption of a virtual CPU until the kernelet's critical section ends, by Linux's own mechanism, and that the kernelet gives the processor up the moment it can. On a Linux that does not preempt kernel code at all, the [watch timer](tasks.md#watch) plays Linux's part: it sends the carrier through the yield stub only outside a critical section, and otherwise waits for the section to end. (On a Linux *built* without a preemption count the word exists but is never read; the endovisor reports that in the boot arguments and vOSTD leaves the mirror out, and the yield stub is then the whole mechanism.)

**The mirror exists only while kernelet code runs.** A service may sleep, and Linux forbids sleeping with the count raised. OSTD calls services under a guard all the time: every page-table cursor holds a preemption guard, and a `vm_map` under one may have Linux allocate a page-table page ([the contract below](#contract) says why that is sound). So every [service stub](../kernelet-api-service.md#depth), on its way to the Linux stack, takes out of Linux's count what the kernelet's guards added, and puts it back on return. It does not ask the kernelet how much that is: the endovisor recorded Linux's count when the carrier entered kernelet code, and the excess over that value is the kernelet's. The same subtraction is made by every other path that takes a carrier out of kernelet code, the yield stubs and the [exit stub](../faults-and-reclamation.md#leaving), so that a kernelet which miscounts, or is evicted with a lock held, leaves Linux's count exactly as Linux expects it. The kernelet's own meaning of the guard is unaffected: nobody else can touch this virtual CPU's data, because nobody else *is* this virtual CPU.

**The bound: 2 ms of run time.** A critical section is a processor that Linux cannot take, so it must not last long, and "must not" has to be enforced against a kernel proper whose logic the tenant may control. The watch timer fires in hard-interrupt context whatever the count, and the bound it enforces does not read the record at all, since a kernel proper that could see when it was sampled could arrange to be found innocent every time (*found in review*). The endovisor keeps, per carrier, the time of its last **real scheduling event**: the last time Linux switched it out (the notifier says), it slept in a service, it went to user mode, or it was forced to yield. When a tick finds a carrier in kernelet code at depth 0 that has run 2 ms since that event, it **forces the yield**, whatever the record says and whether or not Linux is waiting for the processor: it sends the carrier through the same [yield stub](tasks.md#yield) as on a non-preemptible Linux, which saves every register, switches to the Linux stack, takes the kernelet's increment out of Linux's count as a service stub does, calls `schedule()`, and puts everything back. The grace is therefore two watch periods of the carrier's own run time, 2 ms (*chosen*) while high-resolution timers are active, which the runtime checks, plus the delivery of one timer interrupt and the stub's path to `schedule()`, which is how a measured longest stay of 2,205 µs is within the bound as stated; it is within what Linux tolerates from its own non-preemptible sections. A busy kernelet task that takes no guard at all is treated the same way and costs the same: one pass through the stub every 2 ms in which it neither slept nor went to user mode, which on a preemptible Linux with other work waiting has usually already happened by Linux's own hand. A firing that `vcpu_kick` requested runs only the delivery half of the callback: it sets no TICK, counts for none of this bookkeeping, and does not advance the tick period. The yield is forced even on an uncontended processor because a raised count also holds off Linux's read-copy-update machinery, which cannot declare that processor quiescent while its preemption count is up ([`rcu_flavor_sched_clock_irq()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/rcu/tree_plugin.h#L730)); RCU would eventually ask for a reschedule itself, after tens of milliseconds, and the forced yield's `schedule()` reports the quiescent state long before. It is also *borrowed, not stolen*: Linux accounts the extra run time to the sandbox's control group like any other, so the sandbox's long-run share does not grow by a microsecond. What a neighbor can lose is latency, at most the grace, per preemption. Forced yields are counted per sandbox, and a sandbox that collects them is misbehaving in a way the operator can see.

That latency has to be stated plainly, because it is the one thing this design costs a host that a container does not. On any processor that a sandbox may run on, the worst case for a wake-up of *any* other task, a real-time task and Linux's own stopper thread included, grows by the grace; a kernel proper that lives in a critical section on every virtual CPU makes that the steady state, and its neighbors then see it on every preemption, at a cost to the sandbox of nothing. The remedy is the operator's, and it is the one used for any latency-sensitive host work: `cpuset` separation, so that such work does not share processors with sandboxes; a real-time Linux (`PREEMPT_RT`) should not host kernelets at all. Whether the tenants' own critical sections are short enough that forced yields are rare is assumption A33, which the prototype tests.

**The backstop: do not spin on a lock whose holder is not running.** After a forced yield, or when Linux preempts a virtual CPU in user mode while another spins, a waiter can still meet a lock whose holder is off the processor. vOSTD's spin-lock slow path, after about a thousand failed spins, calls the service `vcpu_on_spin()`, and the endovisor gives the waiter's turn away in favor of a sibling carrier of the same sandbox that is runnable but not running, with Linux's exported [`yield_to()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/sched/syscalls.c#L1468), at most once per millisecond per virtual CPU, by the clock, and without the option that would interrupt the sibling's processor. KVM does the same for its guests ([`kvm_vcpu_on_spin()`](https://elixir.bootlin.com/linux/v6.12/source/virt/kvm/kvm_main.c#L4020)), with two differences: it has hardware that tells it a guest is spinning, which vOSTD does not need because it *is* the spin lock, and it asks `yield_to()` to preempt the sibling's processor, which this design declines because that processor may be running a neighbor. How much it helps is less than the name suggests, and the page says so: in v6.12 the fair class honors a `yield_to` as a hint only when its *next-buddy* feature is on, which by default it is not, so when Linux declines the request (a throttled group, a sibling in another class, nothing to yield to) nothing has been yielded, the endovisor yields plainly instead, and when Linux accepts, the sibling is next only if that feature is on (*measured on the tree*: [`yield_to_task_fair()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/sched/fair.c#L9035)). And it does nothing for a sandbox whose group has spent its `cpu.max` quota: Linux then takes every carrier off the run queues until the period refills, a lock holder among them, and the sandbox stalls for up to a period, 100 ms by default. That stall is the sandbox's own, which is the right party; a container's threads are throttled the same way.

### Kernelet atomic mode is not Linux atomic mode {#contract}

The mirror makes Linux honor the kernelet's critical sections, and it is natural to read it the other way too, as if a kernelet in a critical section were in Linux's atomic mode and could not sleep in Linux. It is not, and the memory services depend on the difference.

A kernelet cannot mask hardware interrupts, only virtual ones; its tasks run on virtual CPUs that Linux schedules; the time it reads is the host's. So its atomic mode (a preemption guard, a spin lock, an RCU read guard, each of which is an `AsAtomicModeGuard` on the tree, *measured on the tree*: `ostd/src/sync/spin.rs:140`, `ostd/src/sync/rwlock.rs:284`, `ostd/src/sync/rcu/mod.rs:382` at `ab9a4cfdc`) can promise only this: **on this virtual CPU, no kernelet task switch happens until the guard drops.** Virtual interrupts are a separate matter on this host: `irq_off` withholds them and `guards` does not, so an [upcall](#upcall) handler may run on top of a task that holds a guard; and `vcpu_idle` and `user_run` refuse to be called under a guard at all ([service half](../kernelet-api-service.md#depth)). Atomic mode never promised that the carrier keeps the processor: Linux takes it at any instruction, by an interrupt the kernelet cannot see, for a whole time slice, and the kernelet is already correct under that, as a guest is correct when its hypervisor deschedules a virtual CPU inside a critical section.

A service that sleeps in Linux while the kernelet is in atomic mode is therefore the same event as that preemption, taken voluntarily. On this host kernelet code always runs in Linux *task* context: the upcall, yield and exit stubs run on the carrier's own stacks after Linux has returned from the interrupt, and the die notifier and the preemption notifiers run endovisor code, not kernelet code. So the only thing that stops a service from sleeping is the mirror, and the services that may sleep already lift it in their prologue and restore it in their epilogue ([service half](../kernelet-api-service.md#depth)). The design makes that the rule:

- **R1.** A service may sleep whenever its work needs it; the [service half](../kernelet-api-service.md#depth) says which do. A sleeping service's prologue lifts the mirror and its epilogue restores it.
- **R2.** A service that may sleep must not be called while the caller holds a kernelet **spin lock** or has `irq_off` set (an interrupt-disabling lock raises `irq_off`, not the guard count, on this host). Under a plain preemption guard, an RCU read guard, or a page-table cursor's [range lock](memory.md#range-lock), whose waiters sleep in Linux rather than spin, it may. The reason is lock-holder preemption, not correctness: a spin lock held across a Linux sleep makes every other virtual CPU that wants it spin for the sleep's length, which under memory pressure is milliseconds. vOSTD asserts R2 in debug builds: it keeps a per-virtual-CPU count of spin-lock and `RwLock` guards (both spin), separate from the `guards` count the record publishes and private to vOSTD, and a sleeping service's stub panics the kernelet if that count is nonzero or `irq_off` is set. `user_run` is the one exemption: OSTD's `execute` turns interrupts off on purpose before it, holding no lock, and `user_run` can sleep (its adoption of an address space may release the previous one), so its stub checks the spin-lock count only. An audit of the kernel proper's `VmSpace` call sites found no kernel-proper spin lock held across any `map`, `unmap` or `protect`, and none of them running with interrupts off (*measured on the tree*), so R2 holds for the memory services by inspection, and the assertion guards the future. An upcall handler runs on top of whatever the interrupted task holds, so R2 binds it too: it may call a sleeping service only if the interrupted task held no spin lock, which a handler cannot know, so handlers call none.
- **R3.** What a sleep costs the kernelet is stated, not hidden: this virtual CPU's pending virtual interrupts and the kernelet's RCU grace periods wait for the carrier to return, and its clock jumps by the sleep, as a descheduled virtual CPU's does; the pending word absorbs the backlog, as it does for a virtual CPU that Linux has descheduled. A sleep inside a service is *not* the extended quiescent state of an idle virtual CPU ([register A9](../../../notes/design-register.md#assumptions)): the sleeping task may hold an RCU read guard, so vOSTD does not mark the virtual CPU quiescent around it, and a grace period waits. How long is bounded by what Linux's sleep is bounded by: an allocation under pressure ends in reclaim succeeding or in the memory controller's out-of-memory kill of a carrier, so frames waiting on a grace period pile up for at most that long.
- **R4.** The mirror stays, as a latency optimization for the path that does not sleep: it keeps Linux from preempting a carrier inside a short critical section, within the bound above. It is load-bearing for correctness nowhere, and the prototype's comparison with and without it ([the prototype](../prototype.md#sched)) is what should decide whether it is kept.

The rule the [service half](../kernelet-api-service.md#depth) states, that an upcall handler may call only services that never sleep, is R2's consequence. The refusal of `vcpu_idle` and `user_run` under a nonzero `guards` is a different rule and is unchanged: those two are refused because OSTD makes them with its guards in a known state, not because they sleep.

## Why a kernelet's scheduling cannot hurt its neighbors {#fair}

The argument has three steps, and none of them trusts the kernel proper's scheduler.

1. **Linux sees *N* carriers, the root carrier and the device threads, whatever happens inside.** Task creation, priorities, policies, run queues, a scheduler that starves its own tasks or loops: all of it is state inside the kernelet. The carriers are created once, by the endovisor, with one `nice` value, in the sandbox's control group. The kernelet has no service that changes a carrier's Linux priority, class or affinity.
2. **The control group bounds the *N* tasks.** Linux's group scheduling gives the group its weight's share however many of its tasks are runnable, and its bandwidth limit if it has one ([background](../background.md#scheduler)). One busy kernelet task or five hundred is the same *N* busy carriers (*measured on the booted prototype*: three runnable Linux tasks at most, with two virtual CPUs, whether the kernelet ran one task or fifty).
3. **The three ways a virtual CPU can resist Linux are each bounded.** A critical section delays preemption by two watch periods and a delivery, about 2 ms, charged. Kernelet code that never yields on a non-preemptible Linux is [made to](tasks.md#watch). A kernelet that will not stop at all is [evicted](../faults-and-reclamation.md#eviction).

Three things a kernelet can do reach other processors, and each is bounded. `vcpu_kick` interrupts the target's processor, at most once per pending bit; `vcpu_on_spin` interrupts nobody, though it takes a sibling's run-queue lock for a moment, and is limited to one per millisecond per virtual CPU, so to *N* thousand per second per sandbox; a `vm_flush`, or the flush inside a `vm_unmap`, interrupts the processors where the tenant address space is loaded or was last loaded, which Linux tracks per address space and which lie inside the sandbox's `cpuset`, since only its carriers load it. What remains is the interrupt-time residue the chapter already lists: the watch timer's interrupts, one per millisecond per processor that is running a carrier, are charged to whatever they interrupt, which is the carrier itself, unless the host is built to account interrupt time separately (`IRQ_TIME_ACCOUNTING`), in which case they are charged to nobody, like every other interrupt on such a host.

## What this asks for {#asks}

**Of OSTD**, concretely, at commit `ab9a4cfdc`. For both hosts, one addition: the next-expiry hook of `halt_cpu()` (D122; a registered `fn(CpuId) -> Option<u64>` returning an absolute time in nanoseconds on the same clock as `Jiffies`, or none). Under the `kernelet` feature only, new bodies for what OSTD already has. The four architecture primitives behind the interrupt flag (`arch::irq::{is_local_enabled, disable_local, enable_local, enable_local_and_halt}`), which are what the interrupts-off guard, the context switch and `halt_cpu()` all go through (the switch takes a guard, forgets it, and the next task re-enables with the primitive directly, so the guard's `Drop` alone would never see the section end), set and clear `irq_off`, and carry the mirror's transition and the end-of-critical-section check; `DisabledPreemptGuard`'s `Drop` (`task/preempt/guard.rs`), which today only decrements, carries the same two for the guard count; the spin lock's slow path (`sync/spin.rs`) gains a spin count and the call to `vcpu_on_spin`; `halt_cpu()` becomes `vcpu_idle`; and the `virq_entry` stub is new, with the preemption point at its end. Bare-metal OSTD is not asked for the interrupt-return preemption point, though it would be the same idea. The `Scheduler` and `LocalRunQueue` traits are unchanged: a scheduler written for bare-metal OSTD runs in a kernelet as it is.

**Of Linux**, two items, which the chapter's [ledger](../endovisor.md#patch) carries: two helpers. A carrier that runs tenant threads of several processes must be able to change which Linux address space it runs on ([Memory](memory.md#space)). Linux has the operation for kernel threads ([`kthread_use_mm()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/kthread.c#L1439)) and for `exec` ([`exec_mmap()`](https://elixir.bootlin.com/linux/v6.12/source/fs/exec.c#L958)), but not for a user task that wants to swap between address spaces it holds references to. The patch adds `kernelet_switch_mm()`, 29 lines of code in its first form (*measured on the booted prototype*, fourth phase) modelled on those two, and `kernelet_mm_alloc()`, which creates an address space per tenant process around [`mm_alloc()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/fork.c#L1327) and makes the sandbox's root carrier its owner; the second takes the place of an export of `mm_alloc()` that the fourth phase used, and the size of both is to be measured by the prototype's [fifth phase](../prototype.md#phase5).

Nothing is asked of Linux's scheduler itself: no class, no hook, no BPF program, no new flag. What is used of it is its preemption notifiers, an option without a prompt that the patch selects and the module enables machine-wide at load ([Tasks](tasks.md#watch)). The sandbox's share is set with the files every container runtime already writes (`cpu.weight`, `cpu.max`, `cpuset.cpus`), which need a Linux built with group scheduling, as distribution kernels are.

## Costs

- **Per switch between kernelet tasks**: OSTD's own context switch, a save and a load of callee-saved registers, and no crossing at all between two kernel threads: *measured on the booted prototype*, a round trip between two kernel tasks yielding to each other is 2,056 cycles (542 ns), so about 1,000 cycles per switch with the scheduler's decision included, and a wake-up of a more urgent task on the same virtual CPU runs it 1,558 cycles (411 ns) after the waker's call ([the prototype](../prototype.md#sched), which also says why the Linux numbers beside them, 924 ns for a `sched_yield` round trip between two processes, are a scale and not a baseline). Between two tenant threads add two service calls for the [floating-point state](user-mode.md#fpu), and between tenant threads of different processes one [address-space switch](user-mode.md#adopt) at the next `user_run`.
- **Per preemption guard or spin lock**: a store to the record's guard count and, at the boundary of a critical section, one increment or decrement of Linux's per-processor word and the `mirrored` store, all in the first-level cache (*estimated* at a few nanoseconds; the cooperative experiment took and dropped a guard 27 million times a second).
- **Per tick on a running virtual CPU**: one hard-interrupt timer callback, and one upcall or one flagged return from user mode, about a microsecond (*estimated*).
- **Per wake-up of a task on another virtual CPU**: one `vcpu_kick`, which is a Linux wake-up if that virtual CPU is idle, a flag if it is in user mode, and a bit otherwise.
- **Per Linux preemption that finds a critical section**: two watch periods plus a delivery, about 2 ms, of delay for whoever Linux wanted to run, charged to the sandbox; *measured on the booted prototype* with the earlier form of the bound (a second tick that finds a critical section with Linux waiting), 2,205 µs at the longest against a task that held a guard for 10 ms at a time (two periods and a delivery: the timer's own latency, and the interrupted code's path through the stub to `schedule()`, which under the competitors of that run was up to 200 µs), and zero involuntary preemptions inside a critical section over 267 million lock acquisitions with the mirror on, against 1,467 over 270 million with it off. The bound as now specified, which reads no record, is **[unverified]** by measurement; it is stricter than the one measured.
- **Per switch-in of a carrier**: one arming of a high-resolution timer, in the preemption notifier.

The cross-CPU wake-up and the switch between tenant threads of different processes have no cycle figures yet (**[unverified]**; [the prototype](../prototype.md#sched) says why).

## What a tenant sees

Its kernel's scheduler, working. `nice`, real-time priorities, affinity to virtual CPUs and time slices mean what the kernel proper says they mean, among the sandbox's own tasks. What a tenant can also see is what a guest of any hypervisor sees: its virtual CPUs are not always running, so a task can lose the processor for a stretch that its own scheduler did not decide, and wall-clock time can pass without ticks. A tenant that configures more virtual CPUs than its control group's limit can pay for will see more of that, not less.


One thing a tenant can measure is honestly weaker than on a machine: its kernel's time slices are exact in the virtual CPU's own ticks and elastic in wall clock, by however much Linux took from the virtual CPU meanwhile (*found by the prototype*: a five-tick slice measured seven milliseconds while its carrier was off the processor for two). That is the nature of a second level, and a guest under a hypervisor has the same.

## What this page decides

- **A carrier carries a virtual CPU, and the kernelet multiplexes its own tasks on it with OSTD's own task layer** ([register D116](../../../notes/design-register.md#decisions)). It revises D93 (a carrier per task) and retires the seat of D88, which a virtual CPU now is, permanently. For the Linux host it revises D15, "the kernel proper's scheduler is inert". The alternatives are in the table above.
- **The kernelet is interrupted by upcalls: the endovisor redirects a virtual CPU whose virtual interrupts are on to the image's upcall stub, and leaves a bit pending for one whose interrupts are off** ([register D117](../../../notes/design-register.md#decisions)). It replaces the worker tasks and jobs of the earlier design, and the user-mode tick.
- **OSTD gains kernel-mode preemption on interrupt return** ([register D118](../../../notes/design-register.md#decisions)), a prerequisite, useful on a machine too.
- **The kernelet's guard depth is mirrored into Linux's preemption count while kernelet code runs, with a grace of at most two ticks enforced by the watch timer, and `yield_to()` as the backstop for spinning** ([register D119](../../../notes/design-register.md#decisions)). The alternatives were to let Linux preempt anywhere and only treat the symptom, which is what the backstop alone would be, or to require a Linux that never preempts kernel code.
- **A kernelet's atomic mode governs its own scheduler and its virtual interrupts, and nothing else: a service may sleep in Linux under a kernelet preemption guard, but not under a kernelet spin lock or with `irq_off` set** ([register D126](../../../notes/design-register.md#decisions), which revises D119: the mirror is a latency optimization, load-bearing for correctness nowhere). The alternative, treating a kernelet's critical section as Linux's own atomic mode, would forbid every service that Linux must allocate in while a cursor is held.
- **The patch gains `kernelet_switch_mm()` and `kernelet_mm_alloc()`** ([register D120](../../../notes/design-register.md#decisions), as revised by D125), because a virtual CPU runs threads of many address spaces. The alternative, re-pointing one address space at a different process's mappings on every switch, would empty its page table each time.
