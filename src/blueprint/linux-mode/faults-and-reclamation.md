# Faults, termination, and reclamation

*What ends a kernelet, how each of its virtual CPUs is stopped even if it will not cooperate, and how everything it held goes back to Linux. It discharges fault containment, and the half of fairness that says a tenant cannot hold resources past its own death.*

## The shape of a death

A kernelet ends in one of three ways: its kernel asks to (`power::poweroff` becomes the service `stop`), its kernel fails (a panic nothing catches, or a fault), or the host decides (the operator or the endovisor kills it). Whatever the cause, the same sequence follows:

1. the kernelet is **marked dying**, which is a single store;
2. every one of its carriers is **stopped** at a point where it holds nothing of Linux's;
3. when no task of the sandbox is left, the root carrier and the device threads included, the kernelet has **exited**, and the runtime is told why;
4. **destroy** returns its memory and removes every record that named it.

No code from the kernelet image runs after the mark. Nothing is unwound and no destructor runs: a kernelet's state is abandoned, not tidied, because tidying would mean running the tenant's kernel on the host's behalf at the moment it is least trusted.

## When a carrier may be pulled out {#safe}

A carrier can be removed from kernelet code by force only at a moment when it holds nothing of Linux's: no lock, no reference, no half-finished update. Two tests together identify such a moment, and every mechanism on this page uses both.

- **Its instruction pointer is in a kernelet's text.** This excludes all of Linux's own code and all of the endovisor's: the entry path, the fault path, the exit loop, the gate's hooks, the service bodies. Kernelet code holds nothing of Linux's that it acquired by calling Linux, because it cannot call Linux: the only way out of an image is a service call. It may hold one thing it is *allowed* to, [an increment of Linux's preemption count](virtualizing-ostd/scheduling.md#cooperative), and every path that takes a carrier out of kernelet code gives that back.
- **Its service-call depth is 0.** The endovisor keeps, in each carrier record, a **depth**: 1 from the moment a service prologue begins until its epilogue ends, 0 otherwise ([service half](kernelet-api-service.md#depth)). The record is endovisor memory, so a kernelet cannot hold its own depth at 1. For a running carrier this says the same thing as the first test, from independent bookkeeping, and the mechanisms below require both: an instruction pointer in kernelet text with a depth of 1 means the endovisor's own accounting is wrong, and is treated as a host bug, not acted on.

A carrier that fails either test is in Linux's or the endovisor's code and must be allowed to finish what it is doing. Service calls are short and do not block indefinitely, and that is the endovisor's obligation, not the kernelet's.

### Leaving for good {#leaving}

However a carrier is stopped, it leaves the kernelet the same way it leaves on every trip to user mode: by the [stack switch](virtualizing-ostd/tasks.md) back to its Linux stack, where the frames of whichever gate hook or start function entered the kernelet are waiting, intact. The only difference is a flag that says *do not come back*. A carrier that is stopped inside a service call is already on its Linux stack, and leaves by returning into those same frames instead of switching back to the kernelet stack. Before the switch the carrier sets Linux's preemption count back to what it was when the carrier entered the kernelet, since the kernelet's guards [raise that count](virtualizing-ostd/scheduling.md#cooperative) and a dying kernelet will never lower it again; the endovisor does that from its own record of the value, not from anything the kernelet says ([service half](kernelet-api-service.md#depth)). Linux's code then unwinds normally, the carrier detaches from the gate, and lets the pending `SIGKILL` end the task in Linux's exit loop. This chapter calls that *leaving for good*. No Linux frame is ever abandoned; only kernelet frames are. The kernelet stacks themselves belong to the kernelet's tasks and not to its carriers, and are freed all together by [destroy](#destroy).

## Stopping a carrier {#stopping}

When the kernelet is marked, the endovisor sends `SIGKILL` to every carrier except the root carrier, and starts the eviction described below. Where a carrier is at that moment decides what stops it:

| the carrier is | what stops it |
|---|---|
| in user mode | Linux's own signal handling: the carrier enters the exit loop, the gate's resume hook sees the mark, detaches and steps aside, and Linux kills the task |
| asleep in a service call (an idle virtual CPU in `vcpu_idle`, or a wait inside Linux) | the sleep is killable, so it wakes; the service epilogue sees the mark and the carrier [leaves for good](#leaving) |
| running in a service call | the call completes; the epilogue sees the mark |
| entering a service call | the prologue sees the mark |
| in a [yield stub](virtualizing-ostd/tasks.md#yield), having given Linux the processor | the stub sees the mark when it gets the processor back, and leaves for good instead of resuming |
| not yet started, or waiting for `vcpu_boot` | its start function sees the mark before entering the image |
| **running kernelet code**, with or without guards held | **eviction** |

**The root carrier's own life.** The root carrier carries no virtual CPU and never runs kernelet code; having cloned the carriers, it lives in its resume hook and waits. It is not counted among the kernelet's carriers and is exempt from the sweep. The [device threads](virtualizing-ostd/devices.md) are threads of the root carrier, so the root carrier is also the one that ends them. After the mark it waits for the carrier count to reach zero. Then it stops and joins each device thread, with the helper that pairs with the one that created them; a device thread finishes or cancels its Linux I/O before it stops, so that when it is gone it holds no buffer of the kernelet's. Only then does the root carrier record the exit status, note in its own record that its end is orderly, and end itself as every carrier does, by sending itself `SIGKILL` and returning from the hook into Linux's exit loop. It does not announce *exited*, because it is not gone yet: a task that is ending still has Linux's exit path to run, which closes its descriptors and tears down its address space, calling back into the endovisor as it does. The state moves to *exited*, and whoever is waiting is woken, from the release of the root carrier's [lifeline](virtualizing-ostd/tasks.md#death), which is the last event any task of the sandbox produces. (A release of the root's lifeline *without* the orderly note is the other case, the root carrier killed from outside, below.) The same goes for the carrier count: a carrier is counted until its lifeline is released, not until it leaves for good. So *exited* has one meaning: no task of the sandbox is left, the root carrier and the device threads included, and nothing of Linux's still refers to the kernelet's memory.

If the root carrier is itself killed from outside, Linux kills its device threads with it, since they share its thread group. That is a mark with the reason *carrier lost*, and the root's remaining duties fall to the endovisor's **cleanup thread**: one kernel thread for the whole module.

**Noticing a carrier that Linux killed.** A carrier killed from outside, by the operator or by the out-of-memory killer, never runs endovisor code again. Every carrier therefore holds a [lifeline](virtualizing-ostd/tasks.md#death): an open endovisor file in its descriptor table, which Linux closes when the task exits, for whatever reason and whether or not the carrier ever entered user mode. One case needs help to get that far: a fatal signal sent to a single carrier that is busy in kernelet code. Linux will not act on it until the carrier leaves the kernel, and nothing has marked the kernelet dying, so nothing evicts the carrier. While a kernelet runs, the endovisor therefore looks at its carriers a few times a second, and treats a fatal signal pending on any of them as *carrier lost*, which starts the sweep. A release for a carrier that had not left for good marks the kernelet dying with *carrier lost*: a kernelet that has lost a processor with whatever task was on it, and whatever locks that task held, is not worth keeping.

### Eviction {#eviction}

The last row is the hard one, and it is where Linux differs most from a host built for the purpose. Linux acts on a kill signal only when a task returns to user mode. A carrier looping in kernelet code, say in a `loop {}` reached through a logic bug a tenant found, never returns to user mode, so Linux by itself would never kill it, and the sandbox could never be destroyed.

<figure class="fwd-fig">
<div class="head">
<div class="tag">Forced termination</div>
<div class="title">Eviction: a timer interrupt changes where the carrier resumes</div>
</div>
<svg viewBox="0 0 900 250" role="img" aria-label="A carrier is looping in kernelet code on its kernelet stack at service-call depth zero. The endovisor arms a timer on the processor the carrier is running on. The timer interrupt saves the interrupted registers; the endovisor's timer callback checks that the interrupted instruction lies in a dying kernelet's text and that the carrier's depth is zero, then rewrites the saved instruction pointer. The interrupt returns not into the loop but to an exit stub in the endovisor, which switches to the carrier's Linux stack, leaves the kernelet for good, and lets Linux's pending kill signal end the task.">
<defs>
<linearGradient id="fr-cg" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="#00F7FF" stop-opacity=".22"/>
<stop offset="100%" stop-color="#1937FF" stop-opacity=".22"/>
</linearGradient>
<marker id="fr-a" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#9AA0BE"/>
</marker>
<marker id="fr-ac" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#00F7FF"/>
</marker>
</defs>
<g font-family="ui-monospace,monospace" font-size="10.5">
<rect x="20" y="24" width="196" height="92" rx="8" fill="url(#fr-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="32" y="44" fill="#00F7FF" font-size="9" letter-spacing="1.2">KERNELET STACK &#183; DEPTH 0</text>
<text x="118" y="72" fill="#8FF6FC" text-anchor="middle" font-size="9.5">kernelet code, not returning</text>
<text x="118" y="92" fill="#5C93A8" text-anchor="middle" font-size="9">loop { }</text>
<path d="M216 70 H268" stroke="#9AA0BE" stroke-width="1.4" marker-end="url(#fr-a)"/>
<text x="242" y="60" fill="#9AA0BE" text-anchor="middle" font-size="8.5">1 timer</text>
<text x="242" y="88" fill="#9AA0BE" text-anchor="middle" font-size="8.5">interrupt</text>
<rect x="270" y="24" width="360" height="92" rx="8" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="282" y="44" fill="#00F7FF" font-size="9" letter-spacing="1.2">ENDOVISOR &#183; TIMER CALLBACK</text>
<text x="450" y="66" fill="#C9CCE0" text-anchor="middle" font-size="9.5">2 interrupted ip in a dying kernelet's text?</text>
<text x="450" y="82" fill="#C9CCE0" text-anchor="middle" font-size="9.5">and the carrier's depth is 0?</text>
<text x="450" y="102" fill="#8FF6FC" text-anchor="middle" font-size="9.5">3 saved instruction pointer := the endovisor's exit stub</text>
<path d="M630 70 H682" stroke="#00F7FF" stroke-width="1.4" marker-end="url(#fr-ac)"/>
<text x="656" y="60" fill="#00F7FF" text-anchor="middle" font-size="8.5">4 return</text>
<rect x="684" y="24" width="196" height="92" rx="8" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="696" y="44" fill="#9A9DB0" font-size="9" letter-spacing="1.2">EXIT STUB &#183; LEAVE FOR GOOD</text>
<text x="782" y="66" fill="#C9CCE0" text-anchor="middle" font-size="9">switch to the Linux stack</text>
<text x="782" y="82" fill="#C9CCE0" text-anchor="middle" font-size="9">restore Linux's count, detach</text>
<text x="782" y="102" fill="#C9CCE0" text-anchor="middle" font-size="9">5 SIGKILL ends the task</text>
<rect x="20" y="150" width="860" height="80" rx="8" fill="rgba(255,255,255,.025)" stroke="rgba(255,255,255,.12)"/>
<text x="36" y="170" fill="#9A9DB0" font-size="9" letter-spacing="1.4">WHY IT IS SAFE</text>
<text x="36" y="190" fill="#C9CCE0" font-size="9.5">at depth 0 in kernelet text a carrier holds nothing of Linux's but one count, which the stub gives back &#183; it cannot mask interrupts</text>
<text x="36" y="208" fill="#C9CCE0" font-size="9.5">the kernelet is already dying, so its own half-finished state does not matter &#183; no kernelet code runs afterwards</text>
</g>
</svg>
</figure>

**Eviction** removes such a carrier by force. A Linux timer can only be armed by the processor it will fire on, so the endovisor sends the processor the carrier is running on, which its preemption notifier records, a cross-processor call (`smp_call_function_single()`), and that call arms a high-resolution timer there, pinned to that processor, to fire at once, in the mode that runs its callback inside the timer interrupt itself even on a real-time kernel. Linux's timer interrupt publishes the registers it interrupted ([`sysvec_apic_timer_interrupt`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/apic/apic.c#L1049) calls `set_irq_regs()`), and a timer callback can read them with `get_irq_regs()`, which is how Linux's own profiler samples the interrupted instruction ([`perf_swevent_hrtimer()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/events/core.c#L11222)); the per-processor variable behind it is exported ([`lib/irq_regs.c`](https://elixir.bootlin.com/linux/v6.12/source/lib/irq_regs.c#L13)). The endovisor's callback starts from the task it interrupted: if that task is a carrier, its record names its kernelet. The callback then checks four things: that kernelet is marked dying, the interrupted context was kernel mode, the interrupted instruction pointer lies inside the text range of *that kernelet's instance* (instances of a kind share physical text, but each has its own range of addresses), and the carrier's depth is 0. If all hold, it overwrites the saved instruction pointer with the address of an **exit stub** in the endovisor's text, and nothing else. The interrupt then returns to the stub instead of the loop. The stub runs on the kernelet stack, which always has its [16 KiB reserve](#stack), and does one thing: it [leaves for good](#leaving), through the ordinary stack switch. The interrupted computation is simply never resumed.

A carrier that is not on a processor at that instant is sent nothing, and is caught by its own watch tick when it next runs, since the tick callback makes the same four checks; the endovisor repeats the sweep every millisecond, for the carriers that are on a processor, until the kernelet's carrier count reaches zero. Kernelet code cannot mask interrupts, since OSTD's interface for that is absent from vOSTD, so there is no state in which the timer cannot land. The kernelet's guards do not get in the way either: what they raise is a [virtual CPU's guard count and Linux's preemption count](virtualizing-ostd/scheduling.md#cooperative), which hold off task switches and Linux's scheduler, not hardware interrupts, and eviction consults neither.

Eviction asks nothing of Linux that is not already exported. *Measured on the booted prototype* (assumption A27): a kernel proper whose system-call handler is `loop {}` was evicted in eight runs out of eight, within one or two sweeps, on either of two processors, under a fully preemptible Linux 6.12, and the sandbox then unloaded cleanly with no warning from Linux ([the prototype](prototype.md#probe)). The exit stub's first act is to realign the stack pointer, because an interrupt can land between any two instructions. What has not been tried is eviction under load, with many carriers, or across several kernelets.

## A fault in kernelet code {#fault}

The kernel proper is safe Rust, and in this design no instruction in a kernelet is *expected* to fault: tenant memory is never dereferenced ([Memory](virtualizing-ostd/memory.md#copies)). A page fault, a general-protection fault or an invalid opcode in kernelet text is therefore a bug in vOSTD, a miscompilation, or memory corruption. It must end that kernelet and nothing else.

To Linux, a kernel-mode fault with no recovery entry is an **oops**: it prints the registers and a backtrace, and kills the current task with whatever that task holds. But Linux consults a notifier chain first, and lets a notifier call the oops off: [`__die_body()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/dumpstack.c#L424) returns early if a notifier answers `NOTIFY_STOP`, and [`oops_end()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/dumpstack.c#L359) then returns to the faulting context instead of killing the task. [`register_die_notifier()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/notifier.c#L600) is exported.

The endovisor registers one. Its test is the same as eviction's, that the faulting instruction is in a kernelet's text and the carrier's depth is 0, and its action is the same, to point the saved instruction pointer at the exit stub, after marking the kernelet dying with the fault's address as the reason. *Measured* in an earlier experiment: a module recovered from a deliberate bad access in its own text this way and the machine continued ([the prototype](prototype.md#earlier)).

Three costs come with it, and they are why this mechanism is reserved for bugs.

- **The oops is printed before the notifier is asked**, with interrupts off and a machine-wide lock held, which on a serial console takes tens of milliseconds. It happens once per kernelet, since the kernelet is dead afterwards, and a tenant cannot provoke it without first finding a bug in trusted code.
- **Linux marks itself tainted** on the first oops, permanently. That is an accurate report: trusted kernel code has failed.
- **The operator must not configure the machine to panic or capture a crash dump on an oops.** With `panic_on_oops` set, [`oops_end()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/dumpstack.c#L359) calls `crash_kexec()` before anything else when a crash kernel is loaded, and the machine reboots into the dump kernel. Distributions in the Red Hat family ship that setting on servers. It is an operator requirement of this design. The variable is not visible to a module, so it is the [kernelet runtime](kernelet-runtime.md) that reads `/proc/sys/kernel/panic_on_oops` and refuses to create a sandbox while it is set.

A fault at depth 1 is a fault in Linux or in the endovisor. It is a host bug, it is not contained, and it is handled as Linux handles any oops.

## A kernelet stack that overflows {#stack}

A carrier that runs off the end of its kernelet stack cannot be rescued after the fact: the fault cannot be delivered on a stack that has no room, the processor escalates to a double fault, and Linux halts the machine on any double fault ([`exc_double_fault`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/traps.c#L401)). Recursion depth in a kernel can depend on input (path resolution is the classic case), so the overflow has to be prevented rather than caught.

Kernelet images are built with the compiler's function-entry instrumentation (`-Z instrument-mcount`), which places a call to a fixed symbol at the top of every function that survives inlining, in vOSTD, the kernel proper, and the libraries compiled with them. vOSTD defines that symbol as a short routine, written in assembly because the compiler's contract for it is that it preserves every register: it finds the virtual CPU's record, where vOSTD keeps the current task's stack limit and updates it at every task switch, through Linux's per-processor pointer to the current task and the gate pointer in it (a few dependent loads that are always in the first-level cache, by the offsets published in the kernelet's boot arguments), and compares the stack pointer with the limit plus a reserve of 16 KiB. Below that, vOSTD calls `stop` with the reason *stack exhausted*, and the kernelet dies an ordinary death.

The reserve is for Linux and for the endovisor's own stubs, which push a register set before they switch stacks; the kernelet's own code gets none of it. Service calls run on the carrier's Linux stack ([service half](kernelet-api-service.md#depth)), but two kinds of Linux code still run on top of whatever kernelet frame they interrupt: the scheduler, when Linux preempts the carrier on return from an interrupt, and the exception path, including the printing of an oops, if kernelet code faults. Linux writes all such code to fit in what is left of a 16 KiB stack, so a full 16 KiB is enough by Linux's own standard. The kernelet's own [upcall](virtualizing-ostd/scheduling.md#upcall) also runs on the interrupted task's stack, and is not part of this reserve: the endovisor delivers one only when the interrupted stack pointer is at least 32 KiB above the bottom of the stack it is on, as the endovisor's own records of the stack pool say, so the handlers have 16 KiB of their own before the reserve begins, and a task that is deeper than that simply receives its interrupt a little later. **[unverified]**: the depth has not been measured, and the failure mode if the argument is wrong is a halted machine, not a dead kernelet. The check is made at function entry, before the frame is allocated, so a frame larger than the reserve could still step over it: the compiler's stack probes touch every page of a large frame, but they do not stop at a guard page, and a 64 KiB array on the stack of a function reached at ordinary depth would carry the probes onto the guard and halt the machine, from safe Rust. So the [build audit](builds-and-images.md#audit) refuses an image in which any function's frame exceeds 4 KiB (the compiler reports every frame's size on request), and the reserve is four times that.

**[unverified]** (assumption A28): the cost is *estimated* at one to two percent of kernel time from the call count of typical system calls; it has not been measured on a kernelet.

## What ends a kernelet

| cause | detected by | reason reported |
|---|---|---|
| the kernel proper powers off or exits | the `stop` service | exited, with its code |
| a panic nothing in the kernel proper catches | the `stop` service | panicked, with the message |
| the operator or the endovisor asks | `kill` on the endovisor's device | killed: requested |
| too many caught panics (the *oops budget*) | the `oops` service | killed: oops budget |
| a kernelet stack nearly exhausted | the function-entry check | killed: stack |
| Linux's preemption count found wrong on leaving kernelet code | the service prologue or a stub | killed: host count |
| a fault in kernelet text | the die notifier | killed: kernel fault, with the address |
| a carrier killed from outside (the operator, the out-of-memory killer) | the release of the carrier's [lifeline](#stopping) | killed: carrier lost |
| the sandbox's memory limit, enforced by Linux | the same, since Linux kills a carrier | killed: carrier lost |

The first four are identical on both hosts. One cause on the other host has no counterpart here: there is no "preemption held off too long" kill. A kernelet that holds a guard when Linux wants the processor is given a [grace of at most two ticks](virtualizing-ostd/scheduling.md#cooperative) and is then made to yield where it stands, by the endovisor's [yield stub](virtualizing-ostd/tasks.md#yield); it loses the processor, not its life.

## Destroy {#destroy}

Destroy runs in the endovisor, on the runtime's request, after the kernelet has exited. It is a list, and its order is the argument that nothing can still reach what is being freed.

1. **Enter.** Move from *exited* to *destroying*, or refuse. Wait for operations in progress on the sandbox descriptor to finish.
2. **Carriers and stacks.** Assert the carrier count is zero and the root carrier is gone, which is what *exited* means. Then return the sandbox's whole pool of kernelet stacks to Linux, the boot stacks and whatever the kernelet did not free included; no carrier is left to be standing on one.
3. **Caches.** Flush every model of this kernelet over its whole range, with the same `unmap_mapping_range()` call as a `tlb_shootdown`; then retire the models' files and drop the endovisor's reference on each model's Linux address space. Every carrier is dead, but a Linux address space can outlive its last task for as long as something else on the host holds a reference to it (a reader of its `/proc` entries, for instance), and its page table would still hold translations to the grant. After this step no Linux page table anywhere maps a frame of the grant, whoever holds what, which is what makes step 7 safe.
4. **Timers and notifiers.** Assert that no carrier is left with a preemption notifier registered or a sleep in progress, which *exited* guarantees, since every carrier unregisters its notifier as it leaves for good and a dead task sleeps in nothing. The per-processor [watch timers](virtualizing-ostd/tasks.md#watch) belong to the machine and stop by themselves when they find no carrier to watch.
5. **Devices and channels.** Assert that its device threads are gone, which *exited* guarantees; drop its device models and the references to the files behind them. Its channel connections were reset when it was marked dying; assert that the switch holds none.
6. **The image.** Unmap the instance's range, which removes this instance's mapping of the kind's shared text and leaves the text itself alone, since sibling instances are executing it. Drop the kind's reference. Free the instance's data, shared pages and metadata region ([Builds and images](builds-and-images.md)). The shared text's frames get their write permission back in Linux's direct map, with `set_memory_rw()`, only when the *kind* is unregistered and no instance is left.
7. **Memory.** For each run: zero it, clear its owner-array entries, then free its pages to Linux, which uncharges them from the sandbox's control group. Zeroing at the grant protects the next tenant; zeroing here as well is for Linux, which does not clear memory it hands to its own kernel allocations.
8. **Records.** Free the log ring once the runtime has closed its end, the statistics, and the carrier and virtual CPU records; the exit status stays readable through the sandbox descriptor until that is closed.
9. **Identity.** Retire the kernelet's slot and advance its generation, so that a stale identifier can never name a new kernelet; drop the endovisor's own reference on itself for this sandbox.

The claim that the list is complete is *argued*, not checked: it is complete if every structure that can name a kernelet or a frame of its grant appears in it. On Linux those are the carrier and virtual CPU records, the stack pool, the models with their files and Linux address spaces, the instance's pages, shared pages and metadata region, the kind's reference and the endovisor's own per-sandbox reference (dropped in step 9, as the slot is retired), the log ring and statistics, the exit status, the device and channel tables, the owner array and the slot table; and step 8 shows that *destroyed* waits for the runtime, since the ring is shared with it.

## What this design does not contain

- **A misbehaving service call.** A carrier at depth 1 that loops or faults is a bug in Linux or in the endovisor, and takes down what such bugs take down. This bound is not specific to Linux; the design has it on either host.
- **Induced work Linux does not charge.** Interrupt-time work is charged to whatever it interrupts, as for any Linux process, and a sandbox induces four kinds: the interrupts of the physical devices behind its I/O; its timers; the cross-processor nudges that flag a carrier in user mode when a virtual interrupt is pending for it; and the cross-processor TLB flushes of its `tlb_shootdown`s, which interrupt every processor where the affected address space is loaded. The third kind, like the watch timer's tick, lands on a processor that is running one of the sandbox's own carriers, so the sandbox is the one that pays. The design bounds the others (ring depths, the floor on deadlines, the caller's own charged processor time) and does not charge them.
- **A host I/O that never completes.** The root carrier joins the device threads before the kernelet is *exited*, and Linux's join for such threads cannot be interrupted. Device threads therefore make only waits that can be cancelled or that time out; but a device thread stuck in an uninterruptible wait inside Linux, on a dead disk or a hung network file system, keeps its sandbox from exiting, and its grant from being reclaimed, until the wait ends. It is the state Linux shows as `D` for an ordinary process, with the same cure.
- **Global memory pressure.** A grant is kernel memory, which Linux's machine-wide out-of-memory killer does not attribute to the carriers. If the operator lets the sum of sandbox limits exceed the machine, the victims will be other processes. Within its own limit, a sandbox that runs out is killed whole.
- **The machine-wide stall of an oops**, once per kernelet that dies of a fault.
- **Compaction on a grain request**, which moves other tenants' pages and flushes their translations, rate-limited but not charged ([Memory](virtualizing-ostd/memory.md)); and the run-queue lock traffic of `vcpu_on_spin`, at most a thousand per second per virtual CPU.
- **About 2 ms of latency per preemption** (two watch periods of the sandbox's run time and a delivery) for whoever else wants a processor a sandbox is on, while a kernelet is in a critical section ([Scheduling](virtualizing-ostd/scheduling.md#cooperative)); the same 2 ms defers the sandbox's own `cpu.max` throttling, and Linux carries that deficit into the group's next period.
- **Two small interrupt-time residues of the scheduler**: the one extra watch-timer firing a processor takes after a carrier leaves it, and a kick's cross-processor call in the window between the notifier's report and its arrival.

## What a tenant sees

An exit status that the runtime reports: the code its kernel passed, the panic message, or the kill reason. Processes inside the sandbox see nothing; they stop existing. Connections to other sandboxes are reset. A killed sandbox can leave a torn write in its disk image, as a machine that loses power can.

## What this page decides

- **A carrier running kernelet code is removed by eviction** ([register D98](../../notes/design-register.md#decisions)): a timer callback on its processor points the interrupted frame's instruction pointer at the endovisor's exit stub when the instruction is in a dying kernelet's text at depth 0. The alternative considered earlier, unmapping the kernelet's text so that the carrier faults, needs a machine-wide TLB flush per revocation, because the range flush is not exported, and turns every forced termination into an oops.
- **A fault in kernelet text is contained by a die notifier and the same exit stub** ([register D99](../../notes/design-register.md#decisions)), at the price of one printed oops, a tainted kernel, and an operator requirement. The alternative, a patch that lets Linux's fixup search find the kernelet, was not taken because in this design no kernelet instruction is expected to fault, so the path is for bugs only.
- **Stack exhaustion is prevented by compiler-placed checks, not caught** ([register D100](../../notes/design-register.md#decisions)), because on Linux it cannot be caught.
- **Termination discards the kernelet stack rather than unwinding it** ([register D35](../../notes/design-register.md#decisions), kept), and **grains are zeroed at grant** ([register D55](../../notes/design-register.md#decisions), kept).
