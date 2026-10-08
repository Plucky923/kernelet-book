# The prototype

*What has been verified by running code, what each run showed, and what has not been verified at all. The chapter's claims are only as strong as this page.*

## What was built, at a glance

| what | shows | standing |
|---|---|---|
| **Hello World on the design's real path**: the Asterinas tree's 100-line kernel, source unchanged, as a kernelet in a patched Linux 6.12 | the gate, carriers and the root carrier, kernelet stacks, `user_run`, the model and the cache, tenant copies by walking the model, entry and exit | *measured on the booted prototype*, [below](#hello) |
| **A probe kernel on the same prototype**: demand paging, an illegal instruction, and a kernel that spins forever | a real miss in the model and the kernel proper's fix; an exception taken from Linux's signal queue by the resume hook; **eviction** of a carrier that will not leave kernelet code; the lifeline; the fill/flush machinery over one file per model; service calls on the Linux stack | *measured on the booted prototype*, [below](#probe) |
| **A busy kernel on a Linux booted with `preempt=none`** | the watch timer and the yield stub: a kernelet that never volunteers is rescheduled anyway, and resumes intact | *measured on the booted prototype*, [below](#yield) |
| **The software walk, in a model** | the cost the design adds to every tenant copy, and that the supervisor alias does not earn its risks | *measured in a model*, [below](#walk) |
| **Earlier mechanism experiments** (in a Linux 6.12 guest) | shared text for many instances; the cost of a gate; Linux's refusal of executable `vmap` memory; SMAP's refusal of direct tenant access; recovery from a kernel-mode fault by die notifier | *measured on the booted prototype of each mechanism*, [below](#earlier) |
| **The second-level scheduler**: a 1,100-line kernel with a strict-priority scheduler of its own on two virtual CPUs, against a reference model; the mirror with and without; a sibling control group's share; a kernelet task switch beside Linux's | the policy holds exactly and preempts; zero preemptions inside critical sections; the neighbor's share within 1.2 % whether the sandbox runs 1 or 50 tasks; a task switch of about 1,000 cycles | *measured on the booted prototype*, [below](#sched); the 2 ms bound was measured in its earlier form |
| **Tenant memory on Linux's own page tables** (the fifth phase): the `vm_*` services, the lockless walk, the range lock, the withheld set, the retire list, against the model-and-cache design built from the same tree | the design of the [Memory](virtualizing-ostd/memory.md) page, against eleven experiments and the memory controller's limit: no Linux fault for a page mapped ahead or read after a protect; a first touch of 82 cycles against 2,194; a `fork` of 64 MiB 3.4 times slower, and everything after it faster; every entry intact under reclaim; no torn read in 54 million copies | *measured on the booted prototype*, [below](#phase5); one scheduler test fails intermittently on the new build, for a cause not found, **open** |
| the function-entry stack check, the die notifier on a kernelet's own fault, the vsyscall filter, multi-instance loading of a real image, device models and device threads, channels, the runtime, more than one kernelet, more than two virtual CPUs, the next-expiry hook; and `vcpu_on_spin` with `yield_to()`, and `vm_wait`'s wake by a kill and its `-KLET_STATE` refusal, built but never reached | nothing | **[unverified]**: designed, not built |

The three booted rows were built on an earlier form of the design, in which every kernelet task had a Linux task of its own as its carrier and a virtual CPU was a lease called a *seat* ([why that lost](alternatives.md)). What they show about the gate, the stack switch, the model and the cache, exceptions, eviction, the lifeline and the yield stub does not depend on that difference; where a log or a table below says *seat* or `task_spawn`, that is why.

All four phases were also built on an earlier design of tenant memory, in which the kernelet's page table was a *model* that the processor never walked and Linux's page table a *cache* of it, filled by the endovisor's fault handler ([why that lost](alternatives.md#memory)). Where a log or a table below says *model*, *cache fill*, *model miss*, `pt_activate` or `tlb_shootdown`, it is that design's; what those runs show about the gate, the stack switch, exceptions, eviction, the lifeline and the yield stub does not depend on it, and the fifth phase, [below](#phase5), is the one that tests the design of tenant memory as it now stands.

No full kernelet, meaning the Linux-compatible kernel proper with its file systems and network stack, has run on Linux. What has run is a small kernel written against the same OSTD interface, which uses the interface's hardest parts.

## Hello World on the design's real path {#hello}

**The test.** The Asterinas tree has a teaching example, [a kernel in about 100 lines of safe Rust](https://github.com/asterinas/asterinas/blob/main/book/src/ostd/a-100-line-kernel.md). It allocates frames, copies a user program into them, builds an address space and maps the frames into it, creates a task, enters user mode with `UserMode::execute`, and services two system calls, `write` and `exit`. Small as it is, it uses the parts of OSTD's interface that are hardest to provide on Linux: tasks, a tenant address space, user-mode entry with a returning `execute`, and fallible reads of tenant memory.

**The rule.** The kernel's source file is copied from the tree (commit `ab9a4cfdc`) and the build compares it byte for byte with the original before every run. Only its `Cargo.toml` differs: the dependency named `ostd` resolves to the prototype's vOSTD. If an OSTD item could not keep its path or signature on Linux, that would be a finding. None needed to change.

**The layers**, as built, in a git worktree of the Asterinas repository under `kernelet-linux/`:

| layer | what it is | size |
|---|---|---|
| kernel proper | the 100-line kernel, unchanged, `deny(unsafe_code)` | 136 lines of Rust, 18 of assembly for the user program |
| vOSTD | a Rust crate whose library name is `ostd`, offering the items that kernel uses with their real signatures. It calls no Linux symbol: the linked image has zero undefined symbols | 2,408 lines |
| image ABI | a service table and an entry table of C function pointers, and boot arguments | in both of the above |
| endovisor | one Linux module in C: the program loader and root carrier, carriers by `kernel_clone()`, the stack switch, the gate's two hooks, the memory areas and their fault handler with the model walk and the grant check, lifelines, eviction, the watch timer and yield stub, the services | 2,675 lines |
| the gate | a patch to Linux v6.12 | 103 added lines in 8 files, none removed |
| runtime | a static test program that forks, sets *no new privileges* and executes the sandbox file | 154 lines |

**The run.** Linux 6.12.0, built from `tinyconfig` plus a fragment (SMP, full preemption, modules, seccomp, control groups, address-space randomization), patched, booted in QEMU with hardware virtualization and two processors. One command, `make hello`, builds everything, boots, and checks the serial log. This is the log from the module's loading on; one line is left out, Linux's notice that an out-of-tree module taints the kernel:

```
INIT: loading kernelet.ko
kernelet: kernelet image text [0xffffffffc028e1e7, 0xffffffffc02929a5), 18366 bytes
kernelet: endovisor loaded: grant 4096 KiB at 0x800000, linear map 0xffff894c80000000, 1 seat
INIT: running /tests/10-hello
RUN-HELLO: creating a sandbox from /hello.klet
kernelet: root carrier exec (pid 32), address space emptied
kernelet: boot carrier starting (pid 33)
kernelet: service stub: 10 cycles direct, 18 through the stack switch (n=2000, rdtsc)
kernelet: vOSTD up: grant 0x800000..0xc00000, linear map 0xffff894c80000000, 1 seat(s)
kernelet: model 0x803000 registered, one file object for its windows
kernelet: boot carrier (pid 33) activated the model at 0x803000
kernelet: task carrier starting (pid 34)
kernelet: task carrier (pid 34) user window [0x10000, 0x7fffffffe000) of model 0x803000
kernelet: cache fill pid 34: va 0x401000 <- pa 0x801000 (rwx)
kernelet: syscall 1 serviced by the kernelet (pid 34)
kernelet: Hello, world
kernelet: 
kernelet: syscall 60 serviced by the kernelet (pid 34)
kernelet: stopped, exit code 0
kernelet: task carrier (pid 34) dying
kernelet: root carrier (pid 32) dying
kernelet: task carrier (pid 34) lifeline closed
kernelet: root carrier (pid 32) lifeline closed
kernelet: boot carrier (pid 33) dying
kernelet: root carrier gone, the sandbox is dying; 1 carrier(s) left
kernelet: boot carrier (pid 33) lifeline closed, the last one
RUN-HELLO: no carrier left (module references: 0)
RUN-HELLO: done
INIT: /tests/10-hello exited 0
INIT: unloading kernelet
kernelet: endovisor unloaded
INIT: done
```

Read against the design, line by line:

- *root carrier exec*: the runtime's child executed a file beginning with the magic `KLET`; the endovisor's [binary-format handler](virtualizing-ostd/tasks.md#root) claimed it and emptied the address space.
- *boot carrier*, *task carrier*: two `kernel_clone()`s by the root carrier, one per OSTD task. Each is a separate Linux process (pids 33 and 34).
- *activated the model at 0x803000*: `VmSpace::activate` became `pt_activate`; the root of the kernel proper's page table is a frame of the grant (which starts at 0x800000), and nothing was written to the processor's page-table register.
- *user window*: on the user task's first `user_run`, one memory area covering the user range, created with `vm_mmap()`.
- *cache fill*: the tenant's first instruction fetch faulted in Linux; the endovisor's handler walked the model, checked the frame against the grant, and installed the translation. There is exactly one fill, although the program's message is on another page: the kernel proper read the message [by walking the model](virtualizing-ostd/memory.md#copies), which involves no Linux page table at all.
- *syscall 1 serviced by the kernelet*, *Hello, world*: the [gate](virtualizing-ostd/user-mode.md)'s syscall hook switched to the kernelet stack, `UserMode::execute` returned `UserSyscall`, and the kernel proper's own handler printed the buffer with `println!`, which is the `log_write` service. Linux's `write` was never called.
- *stopped, exit code 0*: `power::poweroff` became the `stop` service; every carrier was killed, each carrier's lifeline closed as Linux tore the task down, the module's reference count reached zero, and the module unloaded.

The checks (`Hello, world`; exit code 0; clean unload; no `BUG:`, `Oops`, `WARNING:` or `Call Trace` in the log) passed on three consecutive runs of the final tree.

**Two paths not on the hello kernel's route were forced, once each, with temporary instrumentation that was then removed.** A forced *model miss* on the first fault made the handler record the fault and return; the resume hook resumed the kernelet with `user_run` returning *exception* (trap 14); the kernel proper ignored it and called `execute` again; the second fault was filled and the program ran to completion. A forced `tlb_shootdown` between the two system calls removed the cached translation, and the log shows it being filled again on the next instruction fetch.

**What the prototype does not have**, and therefore does not show: more than one kernelet, more than one seat, the position-independent loader (the image is linked into the module), the die notifier, the function-entry stack check, the user-mode tick, the FS base and floating-point paths, devices, channels, a real runtime, and control-group accounting of the grant. Its Rust heap is Linux's `kmalloc`, where the design puts the heap in the grant. Implemented but never executed: the write-protection callback, the "present but not permitted" flavor of a model miss, and the refusal of non-64-bit entries. The sizes in the table are of the final tree, which includes the [probe kernel](#probe)'s additions.

## A probe kernel: demand paging, an exception, and eviction {#probe}

Hello World never misses in the model, never faults, and exits politely. A second small kernel, written for the purpose in safe Rust against the same vOSTD (190 lines, `deny(unsafe_code)`), does the three things it does not. It was run after the prototype had been brought in line with what review changed in the design: the rule that the kernel proper hears only of faults taken in user mode, protections built by the handler, one file per model with `unmap_mapping_range()` as the flush behind a per-model lock, service calls on the Linux stack, and a lifeline per carrier. The log below is an excerpt: lines that repeat Hello World's start-up and shutdown are left out, and one long symbol name is shortened.

```
kernelet: kernelet image text [0xffffffffc003e1e7, 0xffffffffc0042f45), 19806 bytes
kernelet: probe: mapped 2 text page(s) at 0x400000; no data page and no stack
kernelet: model 0x802000 registered, one file object for its windows
kernelet: task carrier (pid 34) user window [0x10000, 0x7fffffffe000) of model 0x802000
kernelet: cache fill pid 34: va 0x401000 <- pa 0x801000 (r-x)
kernelet: model miss pid 34: va 0x900000 (w, absent) -> exception
kernelet: probe: page fault at 0x900000 (error code 0x6), mapping a fresh frame
kernelet: cache fill pid 34: va 0x900000 <- pa 0x806000 (rw-)
kernelet: syscall 1 serviced by the kernelet (pid 34)
kernelet: probe: demand paging: read back "PROBE-OK" from 0x900008
traps: probe.klet[34] trap invalid opcode ip:401036 sp:0 error:0 in [kernelet-model][401036,10000+7ffffffee000]
kernelet: signal 4 (si_code 2) from pid 34 is trap 6 -> exception
kernelet: probe: illegal instruction at 0x401036 (trap 6), stepping over two bytes
kernelet: syscall 1000 serviced by the kernelet (pid 34)
kernelet: probe: spinning inside the kernelet at depth 0; only eviction can stop me
RUN-PROBE: destroying the sandbox: SIGKILL to the root carrier (pid 32)
kernelet: root carrier (pid 32) lifeline closed
kernelet: root carrier gone, the sandbox is dying; 2 carrier(s) left
kernelet: arming the eviction sweep every 1000 us on 2 cpu(s)
kernelet: evicting task carrier (pid 34) on cpu 1: ip 0xffffffffc003e6d0 (+0x4e9 in the kernelet image, ...probe..create_user_task..user_task...), sweep 2
kernelet: task carrier (pid 34) dying (evicted)
kernelet: boot carrier (pid 33) lifeline closed, the last one
RUN-PROBE: no carrier left (module references: 0)
kernelet: eviction: 11 sweep(s), 1 eviction(s)
kernelet: endovisor unloaded
INIT: done
```

**Demand paging.** The kernel maps only the program's text. The program stores to an unmapped address. The log shows the six steps of the [model-and-cache design](alternatives.md#memory): a miss in the model, reported to the kernel proper as a page-fault exception with the right address and an error code that says *user, write, not present*; the kernel proper's handler mapping a fresh frame with the ordinary cursor; the translation installed; and the program reading its own data back.

**An exception that is not a page fault.** The program executes `ud2`. Linux forces `SIGILL`; the gate's resume hook takes it off the signal queue before Linux can deliver it, recognizes it as kernel-generated, and `user_run` returns *exception* with trap 6 from the task's thread structure. The kernel proper logs it and steps over the instruction. One thing the design had not said was found here: for an exception that arrives as a signal, the hook must first copy the saved user registers into the context, as the syscall hook does, because the context still holds the registers of the previous trip.

**Eviction.** The kernel proper's handler for system call 1000 is `loop {}`. The test then kills the root carrier from outside. The root's lifeline closes, the endovisor marks the kernelet dying and kills the other carriers, and the spinning carrier, which no signal can reach, is [evicted](faults-and-reclamation.md#eviction): a timer callback on its processor finds the interrupted instruction pointer inside the kernelet image, at depth 0, in a dying kernelet, and points it at the exit stub. The carrier leaves through the ordinary stack switch, gives up its seat, and dies by the pending `SIGKILL`; the module's reference count reaches zero and it unloads. Eight runs evicted eight times, in one or two sweeps each, on either processor, with no complaint from Linux. The interrupted instruction was always one of the loop's two instructions, as Linux's own symbol lookup confirms in the log. The stub realigns the stack pointer, because an interrupt can land between any two instructions.

Three smaller results came with it. Linux's own kernel-mode read of a carrier's user memory (`get_user`, forced from the endovisor for the test) filled the cache on a hit and returned `-EFAULT` on a miss, without the kernel proper hearing of either. `unmap_mapping_range()` on a model's file did remove raw-frame-number translations, and the next access refilled them. And a service call through the stack-switching stub cost 18 cycles against 10 for a direct call in one boot and 26 against 26 in another (*measured on the booted prototype*, 2,000 calls, interrupts off): the pair of stack switches costs under ten cycles.

What this still does not show: eviction under load, with many carriers, or of code that is in the middle of something subtler than a spin; and none of the items in the last row of the table at the top.

## Yielding on a Linux that does not preempt kernel code {#yield}

Review found that on a Linux booted not to preempt kernel code, a busy kernelet would never be rescheduled, and the design answered with the [watch timer and the yield stub](virtualizing-ostd/tasks.md#yield). Both were then built and tested on the same prototype, with the same kernel image booted with `preempt=none` (Linux's log confirms `Dynamic Preempt: none`).

The test kernel's system-call handler, in safe Rust, runs 800 million rounds of a checksum through ten live 64-bit variables, about eight seconds. A competing host process, pinned to the same processor, counts and reports every quarter of a second.

| | watch timer off | watch timer on |
|---|---|---|
| longest gap between the competitor's reports | 4,294 to 4,313 ms | 263 to 274 ms |
| yield-stub invocations | 0 | 807 to 864 |
| the kernelet's checksum | `0x0a9daca3b668810c` | `0x0a9daca3b668810c` |
| the same computation in user space | `0x0a9daca3b668810c` | `0x0a9daca3b668810c` |

Five runs with the timer on. The kernelet was interrupted more than 800 times at arbitrary instruction boundaries, moved to the Linux stack, rescheduled, and resumed, and not one bit of its result differs. With the timer on, the competitor ran at exactly half its solo rate: an even split.

Two things were learned on the way. The first design for re-arming the timer, "only while it interrupts kernelet code", collapses, for the reason now given on the [tasks page](virtualizing-ostd/tasks.md#yield). And the review's worry that a busy kernelet would also stall Linux's RCU is not true of Linux 6.12: a process on the other processor completed grace periods at the same rate with the watch timer off as on, through an 8.6-second monopoly, because Linux's tick reports a quiescent state for kernel code that holds no RCU read lock and has preemption enabled. The harm of a missing yield is starvation of one processor, not a machine-wide stall.

## The walk, measured {#walk}

The design replaces a direct dereference of tenant memory with a software walk of the [page table](virtualizing-ostd/memory.md#copies) followed by a copy through Linux's direct map. Before adopting it, its cost was measured against the two alternatives, in user space on the development machine (Intel Xeon E3-1270 v6, Linux 6.8, gcc 11 `-O2`, one pinned core; medians of five runs).

**The model.** A four-level page table in hardware format was built in user memory, with its nodes placed at shuffled positions so that the walk gets no locality a kernel would not have, over a synthetic process layout of up to a million mapped pages. The walk is four dependent loads with a present-bit test at each level, compiled in its own unit so that the compiler cannot fold it away.

**The three ways to copy.** One set of pages was mapped three times: a *tenant* mapping with 4 KiB pages; an *alias*, a second 4 KiB mapping, standing for the supervisor alias; and a *direct map* on 2 MiB pages, standing for Linux's. A "tenant" touch of a random page in a working set of W pages was followed by a copy of L bytes out of that page, by each route.

| | W = 64 pages | W = 4,096 | W = 262,144 |
|---|---|---|---|
| a walk alone, random page | 3.6 ns (15 cycles) | 4.7 to 5.3 ns | 13.3 ns (55 cycles) |
| a walk alone, its four entries flushed from cache first | about 300 ns | about 300 ns | about 300 ns |
| copy 16 B: direct / alias / **walk + direct map** | 2.6 / 4.7 / **12.1** ns | 2.6 / 8.4 / **13.2** ns | 2.6 / 17.4 / **23.7** ns |
| copy 4 KiB: direct / alias / **walk + direct map** | 81 / 81 / **89** ns | 245 / 266 / **265** ns | 289 / 373 / **362** ns |

The copy figures have the measurement harness's own 9 ns subtracted. The alias took one more hardware page walk per operation than the direct route (*measured* with the processor's TLB-miss counter); the walk-plus-direct-map route took 0.15 more. For scale, one `getppid` system call on the same machine, with its speculative-execution mitigations, took 479 to 497 ns.

**What this decided.** The walk adds about 10 ns to a small copy and nothing measurable to a large one, so the walk stays as designed. The supervisor alias would save about 7 ns on small copies with a small working set, and nothing with a large one, because the second set of 4 KiB translations it needs costs TLB misses that the 2 MiB direct map does not. That does not pay for writing page-table entries Linux believes it owns, so the alias is [rejected](alternatives.md). An 8-entry cache of recent leaf tables in front of the walk recovered about 5 ns in the small case, and with a realistic address-space layout and a larger working set its hit rate fell below 10 percent and it was slower than the plain walk. It was left out.

**What this does not show.** It is a model in user space on one machine, not vOSTD in a kernel. The 256-byte cells at the largest working set varied by a factor of two between runs on this shared machine and are not quoted. The sources, the commands and every table are in the prototype's `kernelet-linux/bench/`, with the raw output in `RESULTS.md`.

## Earlier experiments {#earlier}

Nine experiments were run while the design was being worked out: six numbered ones on the mechanisms this design uses, fault recovery, and two on designs that were [rejected](alternatives.md). Two machines were involved, and their numbers are never compared with each other. The **build host** is the development machine above, where a bare system call costs about 485 ns. The **guest** is Linux 6.12.0 built from `tinyconfig` plus what a virtual machine and loadable modules need, booted under hardware virtualization on that host; a bare system call there costs 44 ns, because the configuration has none of the mitigations. Read the differences, not the ratios: a floor ten times lower flatters every ratio.

**1. Two instances of one image.** The same module source, built under two names and loaded together: each copy had its own data at its own address and ran its own initializer. Linux refuses a second copy of a module by name only. *Shows:* relocating one image twice gives two independent sets of globals.

**2. One physical text page, four instances.** One page of position-independent machine code (`mov rax, [rip+d]; ret`), mapped four times with `vmap()`, each mapping followed by a different data page:

```
picdemo: one text page, pfn 0x76d
picdemo: instance 0 image at ffffc9000001d000  data at ffffc9000001e000  text pfn 0x76d
picdemo: instance 1 image at ffffc90000015000  data at ffffc90000016000  text pfn 0x76d
picdemo: instance 2 image at ffffc90000025000  data at ffffc90000026000  text pfn 0x76d
picdemo: instance 3 image at ffffc9000002d000  data at ffffc9000002e000  text pfn 0x76d
picdemo: call through instance 0 returned 0xda7a0000 (want 0xda7a0000) ok
picdemo: call through instance 1 returned 0xda7a0001 (want 0xda7a0001) ok
picdemo: call through instance 2 returned 0xda7a0002 (want 0xda7a0002) ok
picdemo: call through instance 3 returned 0xda7a0003 (want 0xda7a0003) ok
picdemo: RESULT PASS
```

*Shows:* the whole of the [loading scheme](builds-and-images.md#sharing). It needed `set_memory_rox()` to be exported: the first attempt executed from a plain `vmap()` range and Linux reported `kernel tried to execute NX-protected page`. With the export, the text's page-table entry read `W=0 X=1`.

**3. The cost of a gate.** One program, in the guest, timed with the cycle counter; medians of five runs.

| path | median | over a bare call |
|---|---|---|
| a call Linux services itself (`getppid`) | 44 ns | — |
| Syscall User Dispatch, no kernel change | ≥ 929 ns | + 885 ns |
| a per-task hook that returns a constant | 39 ns | below the noise |

The hook measured below `getppid` because its servicer did no work. What the row shows is that a hook adds nothing measurable to Linux's entry path. That hook sat in x86-64's own entry code; the [gate](virtualizing-ostd/user-mode.md) is in the generic layer one call further in, and its cost with the stack switch and the seat is **not measured**. On the build host, seccomp user notification cost 5,721 ns per call and `ptrace` 8,221 ns, against 1,887 ns for dispatch there.

**4. Where relocations land.** A Rust static library with the shapes a kernel image is full of, linked as a kernelet image would be: `.text` 274,307 bytes and `.rodata` 64,914 bytes with no relocations; 181 in `.data.rel.ro` and 119 in `.got`, all of the one base-relative type. *Shows:* 98 percent of the read-only material can be shared. It is a synthetic library, not the kernelet image.

**5. Indirect-branch markers.** With the compiler flag that asks for them, the library's entry function began with the landing marker that hardware control-flow enforcement checks. *Shows:* the compiler half of [assumption A19](builds-and-images.md#audit). The kernel half is open.

**6. Can kernel-mode code touch a tenant address?** Six cases, in the guest, with SMAP on, each in a forked child so that a fatal case does not end the run:

```
smap_test: the cell holds 5a5a5a5a5a5a5a5a at 0x4c70f0
  bare read        -> child KILLED by signal 9
  module-bracketed -> OK
  copy_from_user   -> OK
  direct-map alias -> OK
  bracketed, bad   -> child KILLED by signal 9
  bad, with fixup  -> OK          (smapdemo: recovered from the fault, fail=-14)
```

*Shows:* a bare kernel-mode read of a mapped, present tenant page ends the task; the same frame read through the direct map needs nothing; and setting the processor flag around the access works for a good address and is fatal for a bad one unless Linux finds a recovery entry, which it does only for its own code and for modules. This is the experiment behind [never dereferencing a tenant address](virtualizing-ostd/memory.md#copies).

**Fault recovery by die notifier.** In the same series, a module registered a die notifier that recognized a fault in its own text, moved the saved instruction pointer, and answered `NOTIFY_STOP`; the faulting process survived, twice, and a full oops was printed each time. A divide error in kernel mode was survived the same way. *Shows:* the mechanism of [fault containment](faults-and-reclamation.md#fault), and its price.

**Two rejected designs, measured.** A supervisor-only alias of a tenant's page table was built: a bare kernel-mode read through it succeeded where the same read through the tenant's address was fatal, at 0.92 cycles per access against 1.08 for an ordinary kernel address and 38 for Linux's bracketed `get_user`; its cost in TLB entries is what [the model above](#walk) measured later. And one Linux task was made to carry two tenant threads, swapping register file, thread pointer and floating-point state 400,000 times: a switch cost 291 ns more than a serviced call that stays on one thread, 91 ns of it floating-point state. A third figure quoted among the alternatives comes from the same series: code run in kernel mode of a hardware-virtualized guest, on the build host, paid 792 ns (3,002 cycles) for an exit and resume, and its second level of address translation cost nothing measurable while translations were resident and a factor of 1.20 on memory access at a working set too large for the TLB.

The programs and unedited transcripts of these experiments were in the book's notes until this chapter replaced them; they are in the repository's history.

## The second-level scheduler {#sched}

The fourth phase rebuilt the prototype on the architecture of the [Scheduling](virtualizing-ostd/scheduling.md) page and ran four experiments against it, one per property the design promises. Everything below is *measured on the booted prototype*, on the same two-processor Linux 6.12 guest, with the sandbox in a control group of its own.

**What changed underneath.** A carrier now carries a virtual CPU; the endovisor has no `task_spawn`, no seats and no job queue. vOSTD's task layer, scheduler interface, context switch (`switch.S` byte for byte), spin locks, wait queues and preemption guards are OSTD's own at `ab9a4cfdc`, ported verbatim; what was written new is the virtual CPU: the record, the upcall stub, the mirror, `vcpu_idle`, `vcpu_kick`, the kernelet-stack pool, the two floating-point services, and `kernelet_switch_mm()`. The pre-linked image still has zero undefined symbols. Of the three OSTD prerequisites the design names, the prototype implemented the two preemption points and not the next-expiry hook, so an idle virtual CPU wakes at every tick.

**The test kernel.** `sched/`, 1,100 lines of safe Rust with `#![deny(unsafe_code)]`, injects a scheduler of its own through OSTD's `inject_scheduler`: strict priority in three levels, round robin within a level on a five-tick slice, a run queue per virtual CPU, and wake-ups that follow the waker unless the task is pinned. It is deliberately not the FIFO scheduler OSTD ships, so that a policy Linux cannot express is what the trace has to show. The three older kernels, `hello/src/lib.rs` still byte-identical to the tree's, pass unchanged on the new architecture; `make yield`'s checksum is still bit-identical after 3,936 upcalls.

### The policy is obeyed, and it preempts

Two virtual CPUs, six kernel tasks and two tenant threads, three seconds: 1,842 task switches and 49 cross-CPU wake-ups, every one of which the kernelet's own scheduler decided and Linux never saw. The trace is checked by a reference model in user space (`bench/sched-model.py`) that asserts *legality*, not one particular interleaving: a task runs only where it is pinned; never while a more urgent task is runnable on that virtual CPU; never twice in a row while a peer waits; never past its slice plus one tick; and a task woken into a more urgent level takes the processor within a tick and slack. No violation in three consecutive runs.

One rule had to be restated on the way (*found by the prototype*): the slice is exact in the virtual CPU's *own* ticks and elastic in wall clock. A five-tick slice measured seven milliseconds once, because Linux had the carrier off the processor for two of them, and a virtual CPU that is off the processor gets no ticks. That is the honest shape of a second-level guarantee, and the [Scheduling](virtualizing-ostd/scheduling.md) page now says so.

### Cooperative: no preemption inside a critical section

Four kernel tasks, nothing pinned, hammering one spin lock for ten seconds while two competitors in a sibling control group on the same two processors make Linux preempt the carriers constantly. The endovisor counts, exactly, every time Linux switches a carrier out involuntarily while the kernelet's contribution to Linux's preemption count is provably in place.

| | mirror on | mirror off | overstayer, mirror on |
|---|---:|---:|---:|
| involuntary deschedules inside a critical section | **0** | **1,467** | **0** |
| spin-lock acquisitions | 267 M | 270 M | 88 M |
| forced yields | 0 | 0 | **1,301** |
| longest deferral of a Linux preemption | — | — | **2,205 µs** |
| the sibling group's processor time over the run | 10.02 s | 9.97 s | 9.96 s |

A repeat gave 0 against 1,068. The mirror is exactly what makes the difference, and it costs the neighbor nothing. The *overstayer* is a task that holds a preemption guard for 10 ms in a loop: it is forced to yield 1,301 times, no stay exceeds 2.2 ms, and its sandbox's own processor time falls from 9.87 s to 9.12 s, which is what "borrowed, not stolen" has to mean. The count is zero with nothing pinned, so the carriers were preempted and migrated between the two processors throughout, which is the case the first watch-timer rule got wrong. (This run predates the review that made the bound independent of the record; the prototype forces the yield on the second tick that finds a critical section with Linux waiting.)

### Fair: the neighbor's share does not move

The sandbox's group and a sibling with equal weight, both confined to two processors. The sibling runs busy loops; the sandbox runs one processor-bound kernelet task, then fifty at mixed priorities, then fifty plus the overstayer.

| | 1 task | 50 tasks | 50 + overstayer |
|---|---:|---:|---:|
| the sibling's share, in processors | **0.988** | **0.998** | **0.987** |
| runnable tasks of the sandbox Linux ever saw at once | 3 | **3** | **3** |
| kernelet stacks the pool made | 4 | 53 | 54 |
| forced yields | 0 | 0 | 460 |

The spread is 1.19 %. Three is the whole argument: two carriers and the root carrier, whether the kernelet has one runnable task or fifty. The first version of this check compared absolute processor seconds across runs of different length and reported a 29 % spread that was not unfairness (*found by the prototype*: strict priority makes the fifty-task run four seconds longer, and both groups get proportionally more); shares are what may be compared.

### Efficient: what a kernelet task switch costs

Five boots, several thousand samples each, medians of the per-boot medians, on a 3.79 GHz counter.

| measurement | median | p99 |
|---|---:|---:|
| two kernelet kernel tasks yielding to each other on one virtual CPU, round trip | **2,056 cycles, 542 ns** | 993 ns |
| a blocked high-priority kernelet task woken by a low-priority one on the same virtual CPU, until it runs | **1,558 cycles, 411 ns** | 840 ns |
| two Linux processes on one processor calling `sched_yield()`, round trip | 3,504 cycles, 924 ns | 1,809 ns |
| a Linux pipe write waking a process on the other processor | 41,386 cycles, 10.9 µs | 4.4 ms |

The first two rows are the design's numbers: a kernelet task switch, scheduler decision included, costs about 1,000 cycles, on a virtual CPU that Linux is meanwhile scheduling normally. The Linux rows are a scale, not a baseline: the Linux pair makes two system calls and changes address space, which the kernelet pair does not, so 542 against 924 ns is not a speed-up claim; and the pipe row's p99 is the cost of competing with the sandbox's own carriers for two processors. Two measurements the design asked for produced no samples in the kept runs and are **[unverified]**: a wake-up *across* virtual CPUs (the mechanism is exercised 49 times in the policy run, and the model asserts each was served within a tick, but no cycle count), and a switch between tenant threads of different processes (exercised 1,267 times below, without a cycle count). The floating-point swap: two tenant threads in two address spaces on one virtual CPU, each keeping four values in vector registers across every trip through the kernel, 518,501 check-ins, 1,037,002 save-load pairs and 1,267 address-space adoptions, not one value lost and, past four times the processor count, no leak of concurrency ids.

### What phase 4 found

Building it changed the design in these places, each folded into the page named.

1. **The mirror must be idempotent, because OSTD's guard lifetimes cross a context switch.** `switch_to_task` takes a guard and the *next* task releases it, so the pairing of increment and decrement holds across tasks and virtual CPUs, not within one function; a first build decremented once too often, took Linux's count to −1, and was caught by the endovisor's range check. The flag `mirrored` in the record makes both operations idempotent, set *after* the increment and cleared *before* the decrement so that the flag never claims more than the count holds; with the flag on the other side of the instruction, the cooperative experiment counted the two-instruction window itself and reported 165 "failures" that vanished when the store moved ([Scheduling](virtualizing-ostd/scheduling.md#cooperative)).
2. **The watch timer's per-processor pointer was a hole**, found in review and confirmed here: a carrier that Linux preempts inside kernelet code resumes without passing any way in. Preemption notifiers replace it ([Tasks](virtualizing-ostd/tasks.md#watch)).
3. **`kernelet_switch_mm()` leaks a concurrency id per adoption unless it does what `exec` does around its swap**, and the leak is invisible on a kernel without restartable sequences, which the first guest was. With the brackets in place, 1,267 adoptions past four times the processor count leaked nothing. The helper also has to consume the caller's reference and return the old address space, because a task's exit drops one unconditionally. It is 29 lines of code, and the patch grew from 103 to 169 added lines ([The endovisor](endovisor.md#patch)).
4. **`masked` became two fields**, a guard count and a virtual interrupt flag, because a single one would have meant a kernelet holding a spin lock received no ticks ([Scheduling](virtualizing-ostd/scheduling.md#upcall)).
5. **The floating-point save had to move.** The first build saved a tenant thread's state where the kernel proper's own hooks place it, before a task switch, and lost values on the first check-in; saving at the return from user mode and loading before the return to it fixed it. The prototype attributes the loss to kernelet code using vector registers, which the kernelet target (`x86_64-unknown-none`, soft float) should preclude, so the cause is **[unverified]** and the design keeps the kernel proper's placement, which `CpuSync` chooses; the prototype's deviation stands until it is explained.
6. **A model's area is created under a lock of its own**, since the fault path takes the model's semaphore inside Linux's address-space lock and `vm_mmap()` takes that lock inside the caller's ([the design it belonged to](alternatives.md#memory)); the design that replaced it creates the area at `vm_create`, on a fresh address space, and needs no such lock. The fill/flush semaphore was contended by two virtual CPUs for the first time and held; the case it exists for, a flush concurrent with a fill, is still only argued.
7. **`vcpu_idle` has a protocol and there is no `timer_arm`**, since OSTD has only a periodic tick ([Interrupts and time](virtualizing-ostd/interrupts-and-time.md)). The prototype raises TICK on every wake from idle.
8. **`user_run` marks the carrier as leaving before it reads the pending word** ([User mode](virtualizing-ostd/user-mode.md#user-run)).
9. **The upcall stub needs a section of its own**, or the linker places it below the image's text and the load-time check refuses the module, which is what the check is for.
10. **`enable_preemption_on_cpu()` must be the last thing `main` does**, and an injected `enqueue` must not ask to preempt a virtual CPU that merely has no current task, since the bootstrap context has none either; both are OSTD's shape, not Linux's.

**Where the prototype still differs from the design.** It keeps the interrupted instruction pointer in the record rather than on the interrupted stack, and reads `stack_limit` for the 32 KiB rule, both of which the review changed afterwards; it re-normalizes and warns on a bad excess where the design kills; it sends the kick's cross-processor call synchronously, which is legal there because nothing raises a line from a hardware interrupt; `vcpu_on_spin` never fired, since no critical section here lasts a thousand failed spins, so that path and `yield_to()` are untested; the next-expiry hook is not implemented; more than two virtual CPUs and more than one sandbox at a time are not exercised; and the `sched` kernel's own scheduler briefly offered one task to two virtual CPUs a handful of times, which OSTD's context switch refused as it is written to, so the invariant held but that scheduler is not proved free of it.

## Tenant memory on Linux's own page tables {#phase5}

The fifth phase rebuilt the prototype's tenant memory on the design of the [Memory](virtualizing-ostd/memory.md) page and ran against it the eleven experiments that design set, each with its pass criterion, and a twelfth that review asked for, the memory controller's limit. Everything below is *measured on the booted prototype*, on the fourth phase's two-processor Linux 6.12 guest, given 512 MiB and, for these experiments, `CONFIG_DEBUG_ATOMIC_SLEEP`, swap on a virtio block device, the multi-generation LRU, compaction, transparent huge pages (always) and Linux's VM event counters. Faults are counted in the area's `fault` and `pfn_mkwrite` handlers (in the model build, in its fault handler), and the test kernel reads the counts as differences around each step.

**What changed underneath.** vOSTD's model page table and the endovisor's fill-on-fault handler are gone from the default build. A new part of the endovisor, `space.c` (974 lines), gives each space a Linux address space from `kernelet_mm_alloc()`, a file with an inode of its own, one area over `[0x10000, TASK_SIZE_MAX)` with the design's flags, a `fault` and a `pfn_mkwrite` that only record the fault and return `VM_FAULT_NOPAGE`, and a shared record holding the root's physical address and sixteen lock words; it implements the nine services of the design, `vm_create` to `vm_wake`, and a tenth, `vm_map_batch`, as an experiment only ([below](#phase5-found)). vOSTD gained the range lock, the withheld set, the two cursors, activation and the retire list; one walk of the four-level table, used by both builds; the tree's `TlbFlusher` and `TlbFlushOp`; reference-counted frames and `FrameRef`; the copies; a minimal RCU; and R2's count in its spin lock. It is now 7,717 lines of Rust in 51 files, and the endovisor 5,541 lines of C. The test kernel, `mem/`, is 1,417 lines of safe Rust under `#![deny(unsafe_code)]`, with a 147-line tenant program that runs the commands its kernel hands it in registers. Its fault-around maps the 64 KiB window under one cursor, as the tree's does, and its `fork` is the tree's `cow_copy_pt` line for line (`kernel/core/src/vm/vmar/vmar_impls/fork.rs:76-130` at `ab9a4cfdc`).

**The patch** is 205 added lines in 9 files, none removed, 36 more than the fourth phase's 169. The ninth file is `arch/x86/mm/tlb.c`, for the export of `flush_tlb_mm_range`; in `kernel/fork.c`, `kernelet_mm_alloc()` takes the place of the export of `mm_alloc`, and `kernelet_switch_mm()` loses one line, its per-switch `lru_gen_add_mm()`. The ledger is two helpers and five exports, counted as [the endovisor page](endovisor.md#patch) counts them.

**One tree, two designs.** A build switch keeps the design this phase replaced, so that the two can be measured on one guest: `VMSPACE=native`, the default, or `VMSPACE=model`, which selects the model in vOSTD and in the endovisor together; vOSTD refuses to start if the two halves disagree. The model build was completed so that it could run the same test kernel (`unmap`, `protect_next`, `query` and the flusher over the model), and it was given every fix below that applies to it, among them a private inode per model and the root carrier as each address space's owner, the [two findings against it](alternatives.md#memory). The comparison is therefore between the two designs, not between this phase and the last.

**The runs.** Every target checks the serial log against its own criteria.

| target | what it runs | result |
|---|---|---|
| `make mem-func` | E2, E3, E4 and E10, on both builds | pass on both |
| `make mem-r2` | E5, R2's assertion | pass |
| `make mem-e4ctl` | E4's control: the mirror left in place across the sleep | pass: Linux catches the sleep |
| `make mem-conc` | E8 and E11's dropped space, on two virtual CPUs; and a control with the grace period off | pass; the control reported |
| `make mem-kill` | E11's carrier killed under its sibling | pass |
| `make mem-pressure` | E7, the entries under reclaim | pass |
| `make mem-perf` | E6 and E9, five boots of each build | pass, 10 boots of 10 |
| `make mem-memmax` | `memory.max` at a page-table charge | reported, [below](#phase5-results) |
| `make hello`, `make probe` | E1, on both builds | pass on both |
| `make sched-policy`, `make sched-coop` | E1 | pass |
| `make sched-fair`, `make sched-perf` | E1 | flaky on both builds: `sched-fair` failed 2 runs of 4 and `sched-perf` 1, and each passed on re-run |
| `make sched-fpu` | E1 | model build 5 runs of 5; native build 4 of 6: **open**, [below](#phase5-open) |

### What the experiments showed {#phase5-results}

**E1, functional.** The 100-line kernel, its source still byte-identical to the tree's, now takes no fault at all: its program's pages are in Linux's table from the moment the kernel maps them, where the fourth phase took one cache fill; the model build still takes that fill and passes. The probe kernel's demand fault is one Linux fault, where the fourth phase took three (the text fill, the miss, the fill after the map); its illegal instruction and its eviction behave as before. `sched-policy` made 1,836 switches, all legal, and `sched-coop` counted 0 involuntary deschedules inside a critical section with the mirror on against 32 with it off, with 1,071 forced yields and a longest deferral of 2,051 µs.

| experiment | result |
|---|---|
| **E2**, no fill faults | 512 pages mapped ahead and then touched: 0 Linux faults, where the model build takes 512. Fault-around: 1 Linux fault for 16 pages, where the model build takes 17 |
| **E3**, protect in place | 64 pages read after a protect to read-only: 0 faults, where the model build takes 65 (its 64-page flush becomes a flush of everything, which evicts the text page too). The write: exactly one `pfn_mkwrite` report, which the kernel proper answers by making the page writable again |
| **E4**, sleeping under a preemption guard | a `map` into a fresh 2 MiB region, so that Linux allocates a page-table page, inside `disable_preempt()`: the endovisor saw the mirrored count and lifted it, `CONFIG_DEBUG_ATOMIC_SLEEP` reported nothing, the tenant read the frame back, and the kernelet's scheduler switched no task during the section. The control, an experiment hook that keeps the mirror across `vm_map`, is caught at once, `BUG: sleeping function called from invalid context`, at `mmap_read_lock()`, before any page-table allocation |
| **E5**, R2's assertion | `vm_map` under a kernelet spin lock: vOSTD stops the kernelet before the call crosses, and Linux reports nothing |
| **E7**, P1 under pressure | with 512 pages mapped and the tenant waiting 30 s in user mode, a host process allocated and touched 588 MiB of the 512 MiB guest: 30,335 pages swapped out, by background and direct reclaim (28 allocation stalls), with compaction scanning and the multi-generation LRU on, and no out-of-memory kill. Afterwards all 512 entries were present and mapped the same frames, the tenant read every page with 0 faults, and every page's contents were intact |
| **E8**, concurrency | virtual CPU 0 mapped and unmapped 64 pages 72,008 times in 3 s while virtual CPU 1 copied them out 54 million times: 0 torn copies, no frame that was not the pool's, 4.6 million ownership checks in `Frame::from_raw`, none failed; 3,101 of the 72,008 grace periods had a copy to wait for. Two mutable cursors on one region, the holder asleep in `vm_map` (an experiment hook: four 20 ms sleeps, standing in for reclaim): the second slept once in `vm_wait`, for 102 ms, took the lock after the release with 0 compare-and-swap retries, and neither virtual CPU switched a kernelet task. With the grace period off, 1 torn copy in one of two runs: the control shows the hazard and does not measure it |
| **E10**, `PROT_NONE` | at 512 pages, since the 4 MiB grant cannot hold 1,024 beside the rest: `mprotect(PROT_NONE)` moved every page into the withheld set (512 `vm_unmap`s); a read faulted as an absent page and the kernel proper's own check delivered the signal; `PROT_READ` moved them back (512 `vm_map`s), and the tenant read every page intact with 0 faults. The model build passes through the same set |
| **E11**, a dropped space | a process's memory unmapped while virtual CPU 1 copies into it: the 64 frames come back from the allocator to a new owner uncorrupted; the last reference, dropped with interrupts off, put the space on the retire list without a crossing, and the next `vm_create` drained it. Then the carrier of virtual CPU 1, on which the space was created, was killed from outside while virtual CPU 0's thread kept demand-faulting in it: 661,336 faults, no host warning, a clean unload |
| **E12**, `memory.max` | one page into each of 2,048 fresh 2 MiB regions, each needing a page-table page charged to the sandbox's group, under a `memory.max` of 4 MiB. Both outcomes, in this order: at the charge for region 769 the memory controller's out-of-memory killer killed the virtual CPU's carrier; that dying carrier's allocation then failed, its `vm_map` returned `-KLET_LIMIT`, and vOSTD panicked the kernelet. The sandbox was torn down cleanly, with no frame or module reference leaked. The kill is the mechanism; the panic is its echo in the dying carrier |

**E6 and E9, the costs.** Five boots of each build on one guest, on the Xeon E3-1270 v6 of [the walk](#walk) (3.8 GHz nominal, up to 4.2 GHz with turbo, frequency not pinned). Times are cycles of the guest's time-stamp counter at 3,791,992 kHz, so one cycle is 0.2637 ns whatever the core clock was; each boot reports its own median, and the table gives the median of the five. The fork rows are the tree's `fork` of a parent with 16,384 pages mapped, 64 MiB over 256 frames each mapped 64 times because the grant is 4 MiB, every page touched first so that the model's cache is full; three forks per boot.

| *measured on the booted prototype* | this design | the model and cache |
|---|---:|---:|
| first touch of a mapped page | 82 cycles (22 ns), 0 faults | 2,194 cycles (579 ns), 1 fault |
| `map`, one page per call | 770 cycles (203 ns), one crossing | 126 cycles (33 ns), none |
| `map` of 512 pages under one cursor, per page | 827 cycles (218 ns) | 159 cycles (42 ns) |
| protect 2 MiB read-only, with its flush | 418,160 cycles (110 µs) | 174,568 cycles (46 µs) |
| a read of each page after that protect | 80 cycles (21 ns), 0 faults | 2,188 cycles (577 ns), 1 fault |
| copy 64 B out of tenant memory | 188 cycles (50 ns) | 194 cycles (51 ns) |
| copy 4 KiB out of tenant memory | 400 cycles (105 ns) | 402 cycles (106 ns) |
| `fork`, 64 MiB mapped | 10.0 ms | 2.9 ms |
| the child's first pass over the 64 MiB | 0.51 ms | 21.1 ms |
| `fork` and the child's pass | 10.6 ms | 24.1 ms |
| the parent's re-read after the fork | 0.53 ms | 10.1 ms |
| `fork` and both passes (the two rows above, summed) | 11.1 ms | 34.2 ms |
| `munmap` of the 64 MiB | 0.51 ms | 0.66 ms |
| *experiment*: `map`, batched, per page | 479 cycles (126 ns) | — |
| *experiment*: `fork` with the child's maps batched | 6.8 ms | — |

The counters beside the times agree in every boot. A native `fork` makes 16,388 `vm_map`s, 16,384 `vm_protect`s and **one** flush of the whole space, and afterwards neither the child's reads nor the parent's take a fault; a model `fork` makes no crossing and one shootdown, and then the child takes 16,385 fills and the parent 16,385 refaults. The batched variant makes 258 calls where the plain one makes 16,388. E9's criterion, one `for_all` flush per fork, holds. The worst 99th percentiles are single samples in single boots (the native fork's is 16.8 ms): the guest shares its host with other work, and a crossing that sleeps can be descheduled. The full table, with the percentiles, is in the prototype's `bench/RESULTS.md`.

**What the numbers say.** A `map` costs about six times a write into the model, 770 cycles against 126, and it is paid once, where the model paid a fault of about 2,190 cycles at the page's first touch; so this design wins on every page that is touched, and loses only on a page mapped and never touched. A protect costs a crossing per page, about 820 cycles (the 2 MiB protect divided by its 512 pages), so the 2 MiB protect costs more than twice the model's, and it saves the refault of every page after it. Copies cost the same in both, since the walk is the same code over a table of the same shape.

**`fork` is the trade-off.** Alone, it is 3.4 times slower than under the model and cache, 10.0 ms against 2.9 ms for 64 MiB, because each page costs two crossings, a protect of the parent and a map into the child. Everything after it is faster, because the model was cheap at fork only by deferring to faults what this design pays at fork, and the faults cost more: the fork and the child's first pass take 10.6 ms against 24.1 ms, 2.3 times faster, and with the parent's re-read, which the model's flush of everything forces to refault, 11.1 ms against 34.2 ms. Batching the child's maps, built as an experiment only, brings the fork to 6.8 ms; batching the protects would take the other half, and was not built. Neither is the design: the [Memory](virtualizing-ostd/memory.md#ops) page records the two as the extension.

### What phase 5 found {#phase5-found}

Most of what building this phase changed was found by the reviews that ran beside it, and is on the [Memory](virtualizing-ostd/memory.md) page already: the private inode, the root carrier as owner, the retire list in place of a reaper task, the lock words taken in ascending index, `zap_vma_ptes()` for `vm_unmap` and `apply_to_page_range()` for a single entry, the check after the insert. What the prototype itself found, each folded into the page named:

1. **`protect_next` crosses only when the bits change.** `fork`'s removal of write permission from a page that is already read-only would otherwise cost a `vm_protect` that changes nothing, one per text page per fork ([Memory](virtualizing-ostd/memory.md#ops)).
2. **A copy from tenant memory must be opaque to the compiler.** With a plain copy from memory that another virtual CPU writes, the optimizer may serve a later load of the destination by loading the source again, after the RCU read section has ended; E8 once counted a torn copy whose differing word read back equal. The copy is now one `rep movsb` in inline assembly, as OSTD's own user copy is assembly, and no copy has been torn in the ten runs with the grace period on since ([Memory](virtualizing-ostd/memory.md#reads)).
3. **vOSTD needs an RCU.** The design's copies run inside a kernelet RCU read section, and a frame an `unmap` removed waits for a kernelet grace period, the tree's `RcuDrop` ([Memory](virtualizing-ostd/memory.md#reads)). The prototype's vOSTD, written by hand, had none, and gained a minimal one: a per-virtual-CPU sequence word, odd inside a read section, which holds a preemption guard, and a synchronous grace period that waits for every other virtual CPU that was inside one.
4. **A five-level host is refused.** The boot arguments carry the paging depth and vOSTD asserts four; the first version on the [Memory](virtualizing-ostd/memory.md#reads) page refuses such a host in its runtime. Permissions come from the leaf only.
5. **The batched map is kept as an experiment**, in both halves, used only when a test turns it on: it gave E6 its batched row and E9 its 6.8 ms. A batch is issued before any other operation of its cursor, so `query`, `find_next` and `jump` never see a page that has not been issued, which is the rule the [Memory](virtualizing-ostd/memory.md#ops) page records for the extension.
6. **The export of `flush_tlb_mm_range` is not enough on its own.** Linux declares the function only for code that is not a module ([`asm/tlbflush.h`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/tlbflush.h#L247), inside the `#ifndef MODULE` that [opens at line 60](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/tlbflush.h#L60)), keeping its TLB internals from modules on purpose, so the module repeats the declaration; a patch meant for review would put it in `linux/kernelet.h`, one line more ([The endovisor](endovisor.md#patch)).

**Where the prototype still differs from the design.** R2's check is always on, not only in debug builds as the [contract](virtualizing-ostd/scheduling.md#contract) has it, because the image is a release build, and since this vOSTD has no `RwLock` it counts spin locks only. The flusher dispatches, when it is dropped, whatever was issued and never dispatched, where the tree's would drop the operations and their frames. E8's reader is a kernel task's copy, not a tenant thread, and its contended holder sleeps by an experiment hook. E10 ran at 512 pages and E9 over 256 frames mapped 64 times each, both because the grant is 4 MiB.

### What phase 5 corrected in the earlier phases

Five bugs in code the earlier phases built, which their runs passed over, and one stated cause that does not hold. The page named for each states the right behavior.

1. **A tenant could return to user mode in the wrong address space** (fourth phase). Only the resume hook adopted the address space its virtual CPU had activated, so when the kernelet handled a system call, switched spaces (another process's thread, or a kernel running a child) and returned through the syscall hook, the tenant resumed in the previous one. It went unnoticed because the threads involved mapped the same text: E9's parent ran in its child's address space until the child was reaped. In the design `user_run` adopts before it returns by either hook ([User mode](virtualizing-ostd/user-mode.md#user-run)), and now so does the prototype. By every sign this was also the cause of intermittent `Frame::from_raw` panics and a double free seen earlier in the phase; none recurred in the ten boots of the cost runs, four boots with the watch timer off, or the final batch after the fix.
2. **An exception taken in user mode re-entered the tenant with the registers of its last system call** (first phase), **and so did a virtual interrupt** (fourth phase). The prototype applied finding 9, [below](#findings), only to exceptions that arrive as signals: a page fault reported by the fault handler resumed the tenant at its last system call's return, and every earlier test happened to be idempotent from there (found by E10's signal, which stepped the instruction pointer from the wrong place). A virtual interrupt taken in user mode did the same, so a tenant in a loop longer than a tick restarted every tick, and E7's 30-second wait never ended. The resume hook now copies the saved registers whenever it enters the kernelet for an exception or a virtual interrupt, as [User mode](virtualizing-ostd/user-mode.md#exceptions) says for an exception and [for a virtual interrupt](virtualizing-ostd/user-mode.md#tick).
3. **Copies ignored the page offset** (first phase): the walk resolved a tenant address to its frame's base. The probe kernel stores the same eight bytes at offsets 0 and 8, so its read-back of `PROBE-OK` from `0x900008`, [above](#probe), passed regardless. A copy goes through the frame's alias at the address's own offset ([Memory](virtualizing-ostd/memory.md#copies)).
4. **A carrier killed in user mode leaked** (fourth phase). It dies without the gate running again, so it never detached: its blank address space, and with it a reference on the module held through the endovisor's program loader, its kernelet stack and its floating-point staging page stayed, and the module could not be unloaded. Found by E11's kill. The lifeline's release now frees what the detach did not; in the design the blank address space goes at the first adoption ([User mode](virtualizing-ostd/user-mode.md#adopt)), and a dead carrier's stack and records are [destroy's](faults-and-reclamation.md#destroy).
5. **The fourth phase's helper added the address space to the multi-generation LRU's list at every adoption**, a double insertion and a leaked control-group reference from the second adoption on. It was invisible because that phase's guest had `CONFIG_LRU_GEN` off, which this phase's turns on for E7, so the fourth phase's third finding, [above](#sched), which counted 1,267 adoptions that leaked nothing, counted concurrency ids on a guest that could not show this leak. The patch now adds the address space once, in `kernelet_mm_alloc()` ([The endovisor](endovisor.md#patch)).
6. **The fourth phase's fifth finding gives a cause that is not this image's.** It attributes the lost floating-point values to kernelet code that uses vector registers, and this image has none (`objdump` finds no `xmm`, `ymm` or `zmm` instruction; the target is soft-float). The boundary it found is real; its cause stays **[unverified]**, as [User mode](virtualizing-ostd/user-mode.md#fpu) says.

### What remains open {#phase5-open}

**A lost virtual-interrupt mask.** `make sched-fpu` fails in 2 of 6 runs on the native build and in none of 5 on the model build. Each failure is a panic at the first check-in, by vOSTD's own check that a function *might break atomic mode (guards = 0, interrupts masked = true)*, from `Task::yield_now()` in the test kernel's system-call handler. Instrumentation left in the build says the last virtual-interrupt mask was taken by the injected scheduler's run-queue lock, in task context: an unmask was lost. Every mask and unmask pairing in the code is on one virtual CPU and in one task, and the cause has not been found. It matters, because a lost mask stops a virtual CPU's virtual interrupts. The experiment creates no `VmSpace`, but the failure appeared with this phase, and until it is explained the phase is passed with that exception.

**Two flaky tests.** `sched-fair` and `sched-perf` are flaky on both builds, in another way: `sched-fair` failed 2 runs of 4 in this phase, and `sched-perf` failed once, because a native-Linux reference measurement produced no samples. Each passed on re-run, neither creates a `VmSpace`, the host is shared with other work, and no cause has been found in this phase's code.

**What remains untested.** Whether the mirror earns its keep now that services sleep (R4), since every memory experiment ran with it on; `vm_wait`'s wake by a kill and its `-KLET_STATE` refusal, built and never reached, and the freezer, never used; large pages, `map_iomem`, a five-level host, a host with page-table isolation (the guest has none, so taking permissions from the leaf only is argued, not tested) and a security module that refuses the anonymous inode; P1's other threats one by one (`ptrace` and `/proc/pid/mem` against the area, same-page merging, NUMA balancing, the out-of-memory reaper, `process_madvise`, khugepaged), since E7 exercised reclaim, compaction scanning and the multi-generation LRU only, and did not observe whether that LRU's walker visited a space's address space; the grace period's necessity, shown once and not measured; more than two virtual CPUs, more than one sandbox, and a space destroyed while another carrier still has it adopted; batched protects; and what the fourth phase left untested that this one did not touch.

**Reproducing it.** In `kernelet-linux/`: `make mem-func mem-r2 mem-e4ctl mem-conc mem-kill mem-pressure` for E2 to E5, E7, E8, E10 and E11; `make mem-memmax` for the limit; `make mem-perf` for E6 and E9; and `make hello probe sched sched-fpu` for E1, with `VMSPACE=model` for the old design. `REPORT.md` there lists this phase's deviations and surprises, numbers 82 to 114.

## Findings {#findings}

Building the prototype changed the design in ten places. Each finding below has been folded into the page named, so the design and the prototype agree; the last column of the table after it records where the *prototype* still differs from the design, which are its shortcuts and not open questions.

1. **OSTD's address-space activation is per CPU, and the 100-line kernel relies on it.** Its `main` activates the address space, and a *different* task enters user mode. With activation recorded per task, which is what both hosts need, that task would have no address space. A new task therefore inherits its creator's activation, weakly, until it activates one of its own ([Memory](virtualizing-ostd/memory.md#space)). The finding applies to the Asterinas host too.
2. **`Task::yield_now()` from the boot context must not return while tasks exist.** On a machine it never does. The first prototype returned at once, and the start-up code that follows `main` powered the kernelet off before its task had run ([The rest](virtualizing-ostd/the-rest.md)).
3. **Linux grants write permission behind a fault handler's back.** A write to a cached read-only translation does not call the area's `fault` function; without a `pfn_mkwrite` callback Linux simply makes the entry writable. The endovisor must supply that callback and consult the model in it; in the design that replaced the model, the callback reports the write to the kernel proper ([Memory](virtualizing-ostd/memory.md#faults)).
4. **A read or fetch that violates a cached translation's permissions never reaches the area at all.** Linux raises `SIGSEGV` directly. The design already turns forced signals back into exceptions in the resume hook, so the case is covered, by that path and not by the fault handler ([Memory](virtualizing-ostd/memory.md#faults)).
5. **Inserting a translation over an existing one silently does nothing.** So the handler must always install the model leaf's *full* permissions, never only what the faulting access needed, or the next wider access would fault forever.
6. **Module lifetime is not quite automatic.** Linux's per-address-space reference on the program loader's module is dropped before the last reference to the area's file is, and the file's release ran in unloaded code, once. The file operations must name the module as their owner ([Tasks](virtualizing-ostd/tasks.md#root)).
7. **Every carrier inherits the root carrier's saved registers**, including its segment selectors and flags, because a clone that starts in a kernel function copies its parent's register file. The root carrier must therefore initialize its own as a 64-bit user task even though it never reaches user mode ([Tasks](virtualizing-ostd/tasks.md#root)).
8. **A Rust image for Linux's module loader needs `-Z plt=yes`.** Without it the compiler reaches its own helper functions through a global offset table, 1,593 relocations of a kind the loader rejects. This affects the prototype's build only; the design's [position-independent image](builds-and-images.md) is loaded by the endovisor, not by Linux's module loader.

9. **An exception that arrives as a signal finds a stale context.** On a model miss the fault handler runs with the user registers already saved by Linux, and so does the resume hook; but the *context* the kernelet sees was last written on the previous trip. The hook must copy the saved registers into it before resuming the kernelet, exactly as the syscall hook does ([User mode](virtualizing-ostd/user-mode.md#exceptions)).
10. **A cloned carrier inherits its parent's lifeline.** A clone without shared descriptors gets a *copy* of the root carrier's table, so the root's lifeline would stay open until the last carrier died. Each new carrier closes the inherited copy first ([Tasks](virtualizing-ostd/tasks.md#death)).

| OSTD item the kernel uses | service | Linux facility | prototype differs from the design in |
|---|---|---|---|
| `#[ostd::main]` | — | the tree's own macro crate, unchanged; entered on the boot carrier | — |
| `println!` | `log_write` | `printk` | the design's per-sandbox log ring |
| `power::poweroff` | `stop` | `SIGKILL` to every carrier; the calling kernelet stack is abandoned | — |
| `Box`, `Vec`, `Arc` | two heap services | `kmalloc` | the design's heap lives in the grant and needs no service |
| `FrameAllocOptions`, `Frame`, `Segment`, `UFrame` | — | a bitmap over one 4 MiB run from `alloc_pages()` | one fixed run; no `grains_request`, no accounting flag, no owner array beyond one range check |
| `VmSpace::new`, `cursor_mut`, `CursorMut::map` | — | none: a hardware-format page table in grant frames | the whole row: the design has no model, and these are `vm_create` and `vm_map` over Linux's page table ([Memory](virtualizing-ostd/memory.md#ops)) |
| `VmSpace::activate` | `pt_activate` | a field in the carrier record | the service is `vm_activate`, and names a space rather than a root |
| `VmSpace::reader`, `VmReader::read_fallible` | `pt_current` | none: a walk of the model in vOSTD, then Linux's direct map | `pt_current` is an extra service; in the design vOSTD remembers the activation itself, and walks Linux's page table |
| `disable_preempt` | — | a counter | — |
| `TaskOptions`, `Task::run` | `task_spawn` | a request to the root carrier; `kernel_clone()` with a start function | no priority or affinity |
| `Task::yield_now` (boot context) | an idle wait | give up the seat; sleep until no task is left | — |
| `UserMode::execute`, `UserContext` | `user_run` | the stack switch, `pt_regs`, the gate's two hooks | no FS base, no floating-point state |
| (the cache) | `tlb_shootdown` | one file per model; one shared, fixed raw-frame-number area per carrier; `fault` and `pfn_mkwrite` walk the model under the model's lock; `unmap_mapping_range()` | the whole row: the design has no cache; its flush is `vm_flush` over `flush_tlb_mm_range()`, and `vm_unmap`'s zap ([Memory](virtualizing-ostd/memory.md#flush)) |
| (a carrier's death) | — | a lifeline file in each carrier's descriptor table | — |
| (forced termination) | — | per-processor `hrtimer` sweep, `get_irq_regs()`, an exit stub | one kernelet, so no per-instance text ranges |

**Reproducing it.** In the worktree, `cd kernelet-linux`, then `make hello`, `make probe` and `make yield`. From a clean checkout the first run downloads the Linux 6.12 release tarball, unpacks it under `.build/`, applies the patch and builds the kernel. `REPORT.md` there lists every deviation and surprise met on the way, 114 in all after the fifth phase, most of them engineering.
