# Tasks, virtual CPUs, and carriers

*What a kernelet's tasks are on Linux, what its processors are, and where the Linux tasks behind those processors come from. A kernelet's tasks are its own and Linux never sees them; what Linux gives a kernelet is virtual CPUs. How the two levels of scheduling then work is the [next page](scheduling.md).*

## Tasks are the kernelet's; processors are Linux's

The kernel proper creates tasks through OSTD's `TaskOptions::new(closure).build()` and starts them with `Task::run()`. Some are kernel threads. Some are tenant threads, whose closure is the loop on the [User mode](user-mode.md) page. There may be thousands.

On Linux none of them is a Linux task. A kernelet task is OSTD's own object: a closure, a stack, saved registers, and a place in the run queues of whatever scheduler the kernel proper injected. OSTD's task layer is the same code in vOSTD as on a machine: creating a task allocates a stack, switching tasks saves one set of callee-saved registers and loads another, and waiting is a matter of OSTD's own wait queues. None of it involves Linux.

What a kernelet cannot make for itself is a processor to run them on. That is what it gets from the host: a fixed number of **virtual CPUs**, chosen when the sandbox is created, each of them a Linux task.

## Carriers {#carriers}

A **carrier** is the Linux task that carries one virtual CPU of a kernelet, for the life of the sandbox. Whatever the virtual CPU does, that Linux task is what does it: it runs the kernel proper's code and vOSTD's, it [enters user mode](user-mode.md) to run whichever tenant thread the kernelet's scheduler picked, it takes that thread's system calls and faults at the gate, and it sleeps in Linux when the virtual CPU has nothing to run. Linux schedules the carriers, like any tasks; it does not know what they carry.

A carrier is *not* a Linux kernel thread. A kernel thread has no address space and can never return to user mode, and a virtual CPU must. Linux has exactly one kind of task with that ability that kernel code can start in an in-kernel function: the kind Linux itself uses to launch `init` and its user-mode helpers, created by [`kernel_clone()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/fork.c#L2745) with a start function and without the kernel-thread flag ([`user_mode_thread()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/fork.c#L2854) is the in-tree caller). Such a task runs the function in kernel mode, and when the function returns, Linux's fork-return path takes the task to user mode with whatever is in its saved register file ([`ret_from_fork()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/process.c#L140)). `kernel_clone()` is not exported to modules; the patch exports it.

## The root carrier, and the family it starts {#root}

<figure class="fwd-fig">
<div class="head">
<div class="tag">Where carriers come from</div>
<div class="title">A sandbox is a small Linux process tree, cloned from a blank template</div>
</div>
<svg viewBox="0 0 900 330" role="img" aria-label="The kernelet runtime, a host user-space program, places a process in the sandbox's control group with its seccomp filter, credentials and namespaces, and that process executes the sandbox file. The endovisor's binary-format handler gives it a blank address space and it becomes the root carrier, which never runs tenant code. The root carrier clones one carrier per virtual CPU with kernel_clone, and the device threads. Each carrier inherits the gate attachment, the seccomp filter, the control group, the credentials, the namespaces, a blank address space and a reference on the endovisor module. The kernelet's own tasks, however many, run on those carriers and are not Linux tasks.">
<defs>
<linearGradient id="tk-cg" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="#00F7FF" stop-opacity=".22"/>
<stop offset="100%" stop-color="#1937FF" stop-opacity=".22"/>
</linearGradient>
<marker id="tk-a" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#9AA0BE"/>
</marker>
<marker id="tk-ac" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#00F7FF"/>
</marker>
</defs>
<g font-family="ui-monospace,monospace" font-size="10.5">
<rect x="20" y="20" width="250" height="64" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="145" y="42" fill="#C9CCE0" text-anchor="middle">kernelet runtime</text>
<text x="145" y="58" fill="#6A6F8C" text-anchor="middle" font-size="8.5">sets cgroup &#183; seccomp filter &#183; credentials</text>
<text x="145" y="72" fill="#6A6F8C" text-anchor="middle" font-size="8.5">then executes the sandbox file</text>
<path d="M270 52 H330" stroke="#9AA0BE" stroke-width="1.4" marker-end="url(#tk-a)"/>
<rect x="332" y="20" width="250" height="64" rx="6" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="457" y="42" fill="#00F7FF" text-anchor="middle">endovisor: program loader</text>
<text x="457" y="58" fill="#5C93A8" text-anchor="middle" font-size="8.5">recognizes the file &#183; blank address space</text>
<text x="457" y="72" fill="#5C93A8" text-anchor="middle" font-size="8.5">attaches the gate</text>
<path d="M582 52 H642" stroke="#00F7FF" stroke-width="1.4" marker-end="url(#tk-ac)"/>
<rect x="644" y="20" width="236" height="64" rx="6" fill="url(#tk-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="762" y="42" fill="#8FF6FC" text-anchor="middle">root carrier</text>
<text x="762" y="58" fill="#5C93A8" text-anchor="middle" font-size="8.5">never runs tenant code</text>
<text x="762" y="72" fill="#5C93A8" text-anchor="middle" font-size="8.5">clones the carriers, once, at start</text>
<path d="M762 84 V128" stroke="#00F7FF" stroke-width="1.4"/>
<path d="M150 128 H762" stroke="#00F7FF" stroke-width="1.4"/>
<path d="M150 128 V158" stroke="#00F7FF" stroke-width="1.4" marker-end="url(#tk-ac)"/>
<path d="M410 128 V158" stroke="#00F7FF" stroke-width="1.4" marker-end="url(#tk-ac)"/>
<path d="M700 128 V158" stroke="#00F7FF" stroke-width="1.4" marker-end="url(#tk-ac)"/>
<text x="436" y="120" fill="#00F7FF" text-anchor="middle" font-size="9">one clone per virtual CPU, started in an in-kernel function</text>
<rect x="20" y="160" width="260" height="70" rx="6" fill="url(#tk-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="150" y="182" fill="#8FF6FC" text-anchor="middle">carrier of virtual CPU 0</text>
<text x="150" y="200" fill="#C9CCE0" text-anchor="middle" font-size="9.5">runs whichever kernelet task</text>
<text x="150" y="216" fill="#C9CCE0" text-anchor="middle" font-size="9.5">the kernelet's scheduler picked</text>
<rect x="300" y="160" width="220" height="70" rx="6" fill="url(#tk-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="410" y="182" fill="#8FF6FC" text-anchor="middle">carrier of virtual CPU 1</text>
<text x="410" y="200" fill="#C9CCE0" text-anchor="middle" font-size="9.5">kernel threads, tenant threads,</text>
<text x="410" y="216" fill="#C9CCE0" text-anchor="middle" font-size="9.5">in user mode or kernel mode</text>
<rect x="540" y="160" width="320" height="70" rx="6" fill="rgba(25,55,255,.16)" stroke="rgba(0,247,255,.35)"/>
<text x="700" y="182" fill="#5C93A8" text-anchor="middle">device threads</text>
<text x="700" y="200" fill="#6A6F8C" text-anchor="middle" font-size="9.5">threads of the root carrier;</text>
<text x="700" y="216" fill="#6A6F8C" text-anchor="middle" font-size="9.5">run only endovisor code</text>
<rect x="20" y="254" width="860" height="56" rx="8" fill="rgba(255,255,255,.025)" stroke="rgba(255,255,255,.12)"/>
<text x="36" y="274" fill="#9A9DB0" font-size="9" letter-spacing="1.4">INHERITED BY EVERY CLONE, BECAUSE LINUX COPIES IT</text>
<text x="36" y="294" fill="#C9CCE0" font-size="9.5">gate attachment &#183; seccomp filter &#183; control group &#183; credentials &#183; namespaces &#183; blank address space (no vDSO) &#183; module reference</text>
</g>
</svg>
<figcaption>Nothing in the bottom row is set up per carrier by the endovisor. It is what <code>fork</code> does, which is why there is no window in which a carrier lacks it. A sandbox with a thousand tenant threads still has this many Linux tasks.</figcaption>
</figure>

A sandbox begins as an ordinary process that the [kernelet runtime](../kernelet-runtime.md) has prepared: it has been placed in the sandbox's control group, given the sandbox's credentials and namespaces, and has installed the seccomp filter from the [User mode](user-mode.md) page. That process then executes the **sandbox file**, a small in-memory file that the endovisor made for this sandbox and handed to the runtime as a descriptor ([The endovisor](../endovisor.md#abi)), as if it were a program.

Linux lets a module teach it new executable formats: a **binary-format handler** ([`struct linux_binfmt`](https://elixir.bootlin.com/linux/v6.12/source/include/linux/binfmts.h#L82), registered with [`__register_binfmt()`](https://elixir.bootlin.com/linux/v6.12/source/fs/exec.c#L88)) is offered every file that is executed and may claim it. The endovisor registers one that claims sandbox files. Its handler does what every program loader does first, calling [`begin_new_exec()`](https://elixir.bootlin.com/linux/v6.12/source/fs/exec.c#L1222), which gives the process a fresh address space, closes its close-on-exec descriptors, resets its signal handlers, and drops everything else the runtime's child might have registered with Linux that would make Linux write into user memory later: its restartable-sequence area, its robust-futex list, its thread-exit notification address, its timers.

It then makes the address space truly blank. Linux has already placed one area in it, the temporary stack that holds the arguments and environment of the `execve`, which the ELF loader would go on to use; this handler unmaps it, so that nothing of the host's is left for a tenant to read. It maps no program and no stack, and no vDSO, since mapping that page is the ELF loader's act and not Linux's. It initializes the process's saved registers as a 64-bit user task, which matters even though this process will never run user code: a clone that starts in a kernel function copies its parent's saved registers, segment selectors and flags included, so every carrier starts from whatever the root carrier has (*found by the prototype*). It attaches the gate to the process, [flags it](user-mode.md#gate), and returns. The process is now the **root carrier**.

The root carrier never reaches user mode. On its way there the gate's resume hook runs, and for the root carrier that hook is where it does its work: it discards any signal pending on itself other than `SIGKILL` (`kernel_clone()` refuses to run for a caller with a signal pending, and a task that never returns to user mode would never clear one), clones one carrier per virtual CPU with `kernel_clone()` and the endovisor's start function, starts the [device threads](devices.md), and then waits for the sandbox to end ([Faults](../faults-and-reclamation.md#stopping)). A clone that fails because a signal arrived meanwhile is retried after a short killable sleep.

Every carrier is thus a child of the root carrier, and a clone without shared memory: it starts with its own copy of the root's blank address space. What it inherits is the row at the bottom of the figure. Two of those entries deserve a sentence each. The *module reference* is Linux's own: an address space created by a binary-format handler holds a reference on the handler's module, a forked copy takes another ([`dup_mm()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/fork.c#L1682)), and both are dropped by core kernel code when the address space dies ([`__mmput()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/fork.c#L1340)), so the endovisor cannot be unloaded from under a carrier while it is on that address space. A carrier that has [adopted](user-mode.md#adopt) a tenant address space is on one the endovisor made, which holds no such reference, so the endovisor also takes a reference on itself per sandbox, from start to destroy, as any module does that has work in flight. One thing must be added by hand: the endovisor's file objects name the module as their owner, because Linux releases the last of them slightly *after* it drops the address space's reference (*found by the prototype*, as a crash in unloaded code). The *control group* is what makes fair accounting a matter of membership rather than bookkeeping: everything the kernelet executes, it executes on a member of the sandbox's group, so the processor time it burns and the kernel memory Linux allocates on its behalf are charged to the sandbox by the machinery Linux already has.

The carrier of virtual CPU 0 enters the image at its entry point and boots the kernelet. The others wait until vOSTD calls the service `vcpu_boot(i)`, which is what starting a secondary processor becomes, and then enter at the entry table's `vcpu_entry`. The root carrier sets `SIGCHLD` to *ignored*, which makes Linux reap its children without a wait.

## Kernelet stacks {#stacks}

Every kernelet task has a **kernelet stack**, as every task has a kernel stack on a machine: 256 KiB (`KLET_KSTACK_BYTES`; the kernelet build sets OSTD's stack size to match), which vOSTD obtains with the service `kstack_alloc` and returns with `kstack_free`. The endovisor keeps a **pool** of them per sandbox. The pool grows on demand from Linux's `vmalloc` area, which puts an unmapped guard page after each range; the endovisor allocates with [`__vmalloc()`](https://elixir.bootlin.com/linux/v6.12/source/mm/vmalloc.c#L3905) and the accounting flag, because plain `vmalloc()` charges nobody (Linux charges its own task stacks by hand, with a function that is not exported), and with the flag the pages come one at a time from the allocator that does charge them to the sandbox, up to the sandbox's configured maximum of tasks; a freed stack goes back to the pool and not to Linux, and the pool is returned whole at [destroy](../faults-and-reclamation.md#destroy). The reason is a Linux detail that a tenant could otherwise turn against the machine: freeing `vmalloc` memory is batched, and the batch ends in a TLB flush on *every* processor, so a tenant that created and ended threads in a loop would tax processors it is not allowed to run on. Linux caches its own task stacks per processor for the same reason. The boot stack of each virtual CPU ([The rest](the-rest.md)) comes from the same pool.

A carrier's *Linux* kernel stack is a different thing: 16 KiB ([`THREAD_SIZE_ORDER`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/page_64_types.h#L15)), used by Linux's entry code, and emptied on every return to user mode. The rule that relates the two is simple: **kernelet code runs on the current task's kernelet stack, and Linux's and the endovisor's code runs on the carrier's Linux stack.** The carrier switches to a kernelet stack when a gate hook or a start function enters the kernelet, and back when the kernelet calls [`user_run`](user-mode.md#user-run) or idles. It also switches to the Linux stack for the length of every [service call](../kernelet-api-service.md#depth), so that Linux's code, including every sleep, runs on a stack Linux knows. Between those moments the kernelet switches among its own tasks' stacks as its scheduler directs, and the endovisor neither sees nor cares which one is current: it remembers only where the kernelet was when it last left, and resumes there.

Linux tolerates kernelet code on stacks it did not allocate:

- The scheduler saves and restores only the stack pointer, so a carrier can be preempted on a kernelet stack.
- Linux finds the current task through a per-processor pointer, not through the stack.
- An interrupt that arrives on a kernelet stack pushes its frame there and then moves to Linux's per-processor interrupt stack, as it would on any kernel stack.
- The saved user registers are found from the top of the Linux stack, which does not move.

One thing Linux does not tolerate is an overflow. Running off the end of a kernelet stack hits the guard page while the stack pointer is already bad, which escalates to a double fault, and Linux halts the machine on any double fault ([`exc_double_fault`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/traps.c#L401)). Depth of recursion in the kernel proper can depend on tenant input, so this must not be left to chance. Kernelet images are compiled with a call at every function entry that compares the stack pointer with the current task's limit and ends the kernelet in an orderly way when only a 16 KiB reserve remains; the mechanism is on the [Faults](../faults-and-reclamation.md#stack) page.

## Virtual CPUs and per-CPU data {#vcpus}

The kernel proper uses per-CPU data everywhere: run queues, allocator caches, statistics, the read side of RCU. OSTD's contract for it is that code which has disabled preemption has the current CPU's copy to itself.

With a carrier per virtual CPU that contract holds by construction. Each virtual CPU has its own copy of the image's per-CPU section, and only its carrier ever runs as that virtual CPU. `CpuId::current()` is the virtual CPU's number, which vOSTD reads from the virtual CPU's record; it finds the record through Linux's per-processor pointer to the current task and the [gate pointer](user-mode.md#gate) in that task. Which *physical* processor the carrier happens to be on is of no interest to the kernelet, and Linux may move a carrier between processors whenever it likes: the virtual CPU moves with it.

Where Linux puts the carriers is Linux's decision, within the processors the sandbox's control group allows. Two carriers of one sandbox may share a physical processor for a while; the kernelet then simply has two virtual CPUs that run at half speed. The record of each virtual CPU says how much time was taken from it ([`stolen_ns`](scheduling.md#upcall)), for a kernel proper that cares. An operator who wants better pins the sandbox to as many processors as it has virtual CPUs.

## The watch timer {#watch}

Three things on the [Scheduling](scheduling.md) page need something that runs *on the processor a carrier is on*, in hard-interrupt context, while the carrier executes kernelet code: delivering the tick, making a carrier give way on a Linux that does not preempt kernel code, and enforcing the bound on how long a kernelet may defer Linux's preemption. One mechanism serves all three.

The **watch timer** is one Linux high-resolution timer per processor, with a period of 1 ms (*chosen*, equal to OSTD's tick), created in the mode that is both *hard* (its callback runs in the interrupt itself, even on a real-time kernel) and *pinned* (Linux may otherwise move a timer to another processor to let the arming one stay idle or isolated). The rule for when it runs is one sentence: **a processor's watch timer is armed exactly while the task running there is a carrier**, whatever the carrier is doing. Each carrier registers, in its start function, a *preemption notifier*, and arms the timer of its processor then and whenever Linux puts it on a processor afterwards, which the notifier tells it ([`preempt_notifier_register()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/sched/core.c#L4866), a per-task callback pair that Linux runs at every switch out of and in to the task, and that KVM uses for the same purpose); the patch turns the option on. The callback re-arms itself if it interrupted a carrier and stops otherwise. The period is measured in the carrier's *own* run time, not in wall time: the notifier knows when the carrier was switched out and in, so a carrier that has run 0.7 ms, been switched out, and been switched in again is armed for 0.3 ms, not for a full period (*found in review*: with a fresh period at every switch-in, a carrier that shares its processor with enough runnable tasks would be switched out before its timer ever fired, and receive no ticks at all). A carrier that arrives with a whole period already run, or with a virtual interrupt pending, gets the timer fired at once; that firing may land while the carrier is still in Linux's switch code, where nothing is delivered, and the callback then re-arms once more at the [50 µs floor](interrupts-and-time.md); after that the ordinary period resumes, since a carrier that has meanwhile entered a service will find the bit on the way out. Two carriers that share a processor each keep their own accrued run time, and the one timer serves whichever is current. The endovisor enables preemption notifiers machine-wide when it loads (`preempt_notifier_inc()`; registration warns and the callbacks stay silent otherwise), and each carrier registers its own notifier in its start function and unregisters it as it leaves for good. (The rule survives a processor being taken offline, since Linux moves a pinned timer away from a dying processor and the next switch-in arms the right one.) Nothing else arms or stops it, so a carrier that Linux preempted in the middle of kernelet code and resumed on another processor, which passes through no code of the endovisor's, is watched where it lands, and no timer runs on a processor that has no carrier on it.

An earlier rule (*found by the prototype*, and then found wanting in review) armed the timer on each way into the kernelet and let it stop when it interrupted something else. It failed twice: a runaway kernelet never arms anything, so a timer that had stopped there stayed stopped; and a carrier preempted *inside* kernelet code re-enters nothing, so after a migration it ran unwatched. The notifier closes both, since Linux runs it on every switch, whoever asked for the switch. The same notifier keeps the virtual CPU's `stolen_ns` ([Scheduling](scheduling.md#upcall)): the time between switching out and switching in, while the carrier was runnable and not in a Linux sleep of its own.

Each time it fires, the callback looks at what it interrupted, which Linux's timer interrupt publishes ([`get_irq_regs()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/events/core.c#L11222) is how Linux's profiler reads it), and at the current task, whose gate pointer says whether it is a carrier and of which kernelet. If it is a carrier, it [delivers the tick](scheduling.md#upcall) and [checks the grace](scheduling.md#cooperative). And it does the following.

### Where Linux does not preempt kernel code {#yield}

Many distribution kernels are built or booted so that a task in kernel mode is rescheduled only where the code volunteers. Linux's own code volunteers often; a kernelet never does, since it cannot call Linux. Left alone, a tenant that kept its kernel busy could then hold a processor for as long as it liked, past its control group's limit and against every other task that wants that processor.

Whether it would also stall Linux's RCU, machine-wide, depends on how the kernel was built. Where the preemption model is chosen at boot, RCU is the preemptible kind, and Linux's own tick reports a quiescent state for kernel code that is neither in a read-side section nor running with preemption off, which describes kernelet code outside a [critical section](scheduling.md#cooperative) (*measured on the booted prototype*, which is such a kernel, before the mirrored count existed: grace periods completed at the same rate throughout an 8.6-second monopoly). Inside a critical section the mirrored count says "preemption off", RCU waits, and the 2 ms bound is what keeps the wait under 2 ms. **[unverified]**: the measurement predates the mirror and is to be repeated with it. On a kernel *built* without kernel preemption, RCU hears from a processor only when it runs user code, idles, or passes a voluntary preemption point, and a busy kernelet would stall it; there the mechanism below is what keeps RCU moving as well.

So the endovisor volunteers on the kernelet's behalf. When the watch timer interrupts a carrier in kernelet text, outside a [critical section](scheduling.md#cooperative), and finds that Linux has marked the carrier as due to reschedule, it saves the interrupted instruction pointer in the carrier record and points the frame at a **yield stub**, by the same means as [eviction](../faults-and-reclamation.md#eviction). The stub saves every register and the flags on the current kernelet stack, switches to the Linux stack as a service call does, calls Linux's voluntary preemption point (`cond_resched()`, exported) and, if that declined and the mark is still set, `schedule()` (on some configurations the first is compiled to nothing), switches back, restores everything, and jumps to the saved instruction pointer. Kernelet code is compiled without a red zone, as all kernel code is, so nothing below its stack pointer is live. If the kernelet was marked dying in the meantime, the stub leaves for good instead of resuming. On a Linux that does preempt kernel code, Linux itself reschedules on the way out of the interrupt, and the stub, if the callback armed it, finds nothing left to do; it is the non-preempting configurations the stub exists for.

The callback does not consult the kernelet's record to decide *whether* the kernelet may keep the processor forever; it consults it to decide whether *this* tick is a polite moment, and the [2 ms bound](scheduling.md#cooperative), which is enforced from the endovisor's own clock, overrules it. The stub is the same in both uses, and this chapter calls it *the yield stub* whether the timer sent the carrier there politely or by force; the only difference is that the forced one also takes the kernelet's [increment](scheduling.md#cooperative) out of Linux's count before it calls `schedule()`, and puts it back after. The stub's saved instruction pointer is in the carrier record, in endovisor memory, and the upcall's on the interrupted stack, so the two cannot overwrite each other.

*Measured on the booted prototype* (assumption A29), on Linux 6.12 booted with `preempt=none`, with a kernelet computing a checksum for eight seconds and a competing host process pinned to the same processor: without the watch timer the competitor was starved for 4.3 seconds at a stretch; with it, the longest gap was 0.27 seconds, the two shared the processor evenly, the stub ran more than 800 times at arbitrary instruction boundaries, and the checksum was identical, bit for bit, to the one computed undisturbed and to a reference computed in user space ([the prototype](../prototype.md#yield)).

## When a carrier dies {#death}

A carrier lives as long as its sandbox. When the kernelet is stopped, each carrier [leaves the kernelet for good](../faults-and-reclamation.md#leaving): it switches to its Linux stack for the last time, detaches itself from the gate, and ends by sending itself `SIGKILL`; Linux offers a module no direct way to exit a task that is not a kernel thread. Because no two carriers share a Linux thread group, the signal ends that one task. (A kernelet *task* that ends is no event for Linux at all: OSTD frees its stack and picks another.)

A carrier can also be killed from outside, by the operator or by Linux's out-of-memory killer. Losing a carrier is losing a processor in the middle of whatever it was doing, possibly holding the kernel proper's locks, and the kernelet as a whole cannot survive that. So the unit of killing is the sandbox. Every carrier holds, from the moment it starts, one open file of the endovisor's in its Linux descriptor table, its **lifeline**. Nothing reads or writes it. (A clone begins with a copy of its parent's descriptor table. The root carrier's table holds exactly one descriptor, its own lifeline, because the program loader closes every other descriptor the runtime's child still had; a new carrier's start function closes the inherited copy before it opens its own, so that each lifeline has exactly one holder.) Linux closes a task's descriptors when the task exits, whatever the cause, and the file's release function is the endovisor's notice that the carrier is gone; if the carrier had not left for good by then, the endovisor ends the whole kernelet. A tenant cannot close a lifeline, because closing a descriptor is a Linux system call.

A carrier is an ordinary Linux task, so the host can send it other signals too. The gate's [resume hook](user-mode.md#exceptions) discards every one but `SIGKILL`: a carrier cannot be stopped, continued or terminated politely, one at a time, from the host. To end a sandbox the operator uses `KERNELET_KILL` or the group's `cgroup.kill`. *Pausing* one with the control group's freezer is not supported: Linux freezes a group's tasks as they pass through its signal-delivery code, which a carrier that stays in the kernel (the root carrier, an idle virtual CPU) never reaches. The runtime therefore does not offer the container interface's `pause`. Inside the sandbox, of course, the tenant kills its own processes as often as it likes; those are the kernel proper's signals, and Linux never hears of them.

## Costs

- **Per sandbox**: one root carrier and one carrier per virtual CPU, each a Linux task structure, a 16 KiB Linux stack and an address-space descriptor. It does not grow with the number of kernelet tasks.
- **Per kernelet task**: a 256 KiB kernelet stack and OSTD's task object, as on a machine.
- **Per task creation or exit**: nothing in Linux; a stack from the pool, which grows by one `vmalloc` the first time the sandbox reaches that many tasks.
- **Per millisecond of kernelet execution on a processor**: one watch-timer interrupt.

## What a tenant sees

Its own processes and threads, scheduled by its own kernel. From the host, a sandbox is a control group containing a handful of processes (the root carrier, one per virtual CPU, and the device threads) and nothing that corresponds to a tenant's threads.

## What this page decides

- **A carrier carries a virtual CPU, not a task, and is cloned from the sandbox's root carrier with `kernel_clone()`** ([register D93](../../../notes/design-register.md#decisions), revised by D116). Kernel threads cannot enter user mode; Linux's user-mode-helper interface starts tasks in the wrong control group, with the wrong credentials and without the filter.
- **The root carrier is created by a binary-format handler** ([register D94](../../../notes/design-register.md#decisions)), because `exec` is the one operation that gives a process a blank address space, and because Linux's own reference counting then pins the module for exactly as long as any carrier's address space lives.
- **Kernelet code runs on the current task's kernelet stack, and everything else on the carrier's Linux stack** ([register D84](../../../notes/design-register.md#decisions), kept; D112).
- **Per-CPU data belongs to a virtual CPU, which belongs to one carrier** ([register D88](../../../notes/design-register.md#decisions)'s seat is retired by D116): there is no lease to take, because nothing else can be that virtual CPU.
- **One pinned, hard-interrupt watch timer per processor serves the tick, the yield on a non-preempting Linux, and the grace bound** ([register D115](../../../notes/design-register.md#decisions), extended by D117 and D119).
