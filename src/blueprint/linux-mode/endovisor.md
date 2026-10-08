# The endovisor

*The one component that knows both worlds: a Linux kernel module that loads kernelet images, implements the service table over Linux, carries out the life cycle, and offers the kernelet runtime a device to drive it all. This page also holds the complete list of what the design asks of Linux.*

## What it is

The **endovisor** is a loadable Linux kernel module, `kernelet.ko`, written in C. It is trusted exactly as the rest of Linux is. It is *endo-* because it lives inside the host kernel, beside the kernelets it manages and at their privilege level, where a hypervisor would sit beneath its guests. The relationship it has with user space is the one KVM has with a virtual machine monitor, with one difference: the device models, which for KVM live in the monitor, live in the endovisor, a function call away from the kernelets they serve.

It has nine parts, and each is specified on the page that needs it.

| part | what it does | specified on |
|---|---|---|
| **loader** | registers kinds; maps an instance's shared text and private data; relocates | [Builds and images](builds-and-images.md) |
| **program loader and root carrier** | claims the sandbox file when it is executed; clones one carrier per virtual CPU | [Tasks](virtualizing-ostd/tasks.md#root) |
| **gate operations** | the syscall hook and the resume hook; the stack switch; `user_run` and the adoption of address spaces | [User mode](virtualizing-ostd/user-mode.md) |
| **virtual CPUs** | the watch timer; delivery of virtual interrupts; the yield stubs and the 2 ms bound | [Scheduling](virtualizing-ostd/scheduling.md), [Tasks](virtualizing-ostd/tasks.md#watch) |
| **service half** | the twenty-five services over Linux, with the depth and preemption-count bookkeeping | [service half](kernelet-api-service.md) |
| **memory** | grains, the owner array, the memory services that edit a tenant process's Linux page table, the fault handler that reports a tenant's faults | [Memory](virtualizing-ostd/memory.md) |
| **containment** | the dying mark, eviction, the die notifier, the exit stub, destroy | [Faults](faults-and-reclamation.md) |
| **device models and the channel switch** | virtio register files, device threads, vsock | [Devices](virtualizing-ostd/devices.md), [Channels](channels.md) |
| **the device node** | the interface to the runtime | below |

## What it asks of Linux {#patch}

This is the whole ledger. Everything else the endovisor uses is already exported to modules in Linux v6.12, and each use is linked where it is described.

**A patch to the generic entry layer: the gate.** A pointer in the task structure, one bit in the syscall-work mask, a call after seccomp in `syscall_trace_enter()`, and a call at the top of `exit_to_user_mode_loop()`. The generic entry layer is shared by x86-64, RISC-V, s390 and LoongArch, so the gate is not an x86 patch.

**Two helper functions: `kernelet_switch_mm()` and `kernelet_mm_alloc()`.** The first replaces the calling task's address space with another that the caller holds a reference on, and returns the old one, for a task that is *not* a kernel thread. A carrier uses it to [adopt the Linux address space](virtualizing-ostd/user-mode.md#adopt) of the tenant process it is about to run. Linux has the operation twice already, for a kernel thread that borrows a user address space ([`kthread_use_mm()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/kthread.c#L1439)) and for `exec` ([`exec_mmap()`](https://elixir.bootlin.com/linux/v6.12/source/fs/exec.c#L958)), and the helper does what the two have in common and what each does for its own case: take the task lock, switch with interrupts off through [`switch_mm_irqs_off()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/tlb.c#L498), which is not exported, setting the task's address space and its *active* address space together as `exec_mmap()` does for a task that had one (a user task holds no lazy-TLB reference, so none is taken or dropped), and keep the per-address-space bookkeeping in step: the membarrier state, the multi-generation LRU's notion of which address space is in use (the address space is added to the LRU's list once, when the endovisor creates it, never per adoption), and the concurrency ids of restartable sequences, released for the old address space before the swap and acquired for the new one after it, by the two functions `exec` calls around `exec_mmap()`, which take the run-queue lock themselves ([User mode](virtualizing-ostd/user-mode.md#adopt) says why that one matters). Two things `exec_mmap()` does the helper must *not* do, because a tenant address space is shared by several carriers where `exec`'s is fresh: reinitialize the concurrency-id table, or reset the membarrier state; it updates the latter for the current task as `kthread_use_mm()` does. Per switch, then, it does what `kthread_use_mm()` does except take a lazy-TLB reference, which a user task must not, plus the concurrency-id brackets, the membarrier update, whose full barrier on x86 comes from the page-table load itself ([`switch_mm_irqs_off()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/tlb.c#L545)), a dependency the helper states in a comment since it sits in generic code, and `lru_gen_use_mm()`; and it **never** calls `lru_gen_add_mm()`, which `exec_mmap()` calls once for a fresh address space and which corrupts the list if the address space is on it already ([`lru_gen_add_mm()`](https://elixir.bootlin.com/linux/v6.12/source/mm/vmscan.c#L2811); *found in review*: the prototype's fourth-phase helper called it on every switch). The caller supplies a reference on the new address space, and the helper returns the old one with its reference for the caller to drop. It cannot be written in a module because everything it touches is private to the core kernel. *Measured on the booted prototype*, fourth phase: 29 lines of code, plus its comment and declaration; its size with the per-switch duties above is to be measured by the prototype's [fifth phase](prototype.md#phase5).

The second, `kernelet_mm_alloc(owner)`, creates the empty Linux address space of a [tenant process](virtualizing-ostd/memory.md#space): it calls [`mm_alloc()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/fork.c#L1327), which Linux exports only to its own unit tests, sets the new address space's owner to the task it is given, which is the sandbox's root carrier, and then adds the address space to the multi-generation LRU's list, once, in that order, so that the list is the one of the owner's control group. Left to `mm_alloc()`, the owner would be the creating carrier, held without a reference, and a fault in the space after that carrier's death would read a freed task (*found in review*). It takes the place of the export of `mm_alloc()` that the fourth phase used, and cannot be a module's either, since `lru_gen_add_mm()` is not exported. Each carrier also registers a *preemption notifier* on itself ([Tasks](virtualizing-ostd/tasks.md#watch)), which needs no export but an option that is off unless a user selects it (`PREEMPT_NOTIFIERS`); the patch's configuration entry selects it, and the module turns the notifiers on at load with the exported [`preempt_notifier_inc()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/sched/core.c#L4850).

**Five exports.**

| symbol | why | what happens without it |
|---|---|---|
| `kernel_clone` | create a carrier: a task that starts in the endovisor and can enter user mode | no virtual CPU can run a tenant |
| `set_memory_rox` | make an instance's text executable | no kernelet can run at all |
| `set_memory_rw` | undo the side effect of the above on the direct map when a kind is unregistered and its text frames are freed | an unregistered kind leaks read-only frames into Linux's allocator |
| `set_memory_ro` | protect an instance's relocated tables | hardening only |
| `flush_tlb_mm_range` | `vm_flush`: flush a range of a tenant address space on the processors where it is loaded, leaving its entries in place ([Memory](virtualizing-ostd/memory.md#flush)) | a flush must go through `unmap_mapping_range()`, which removes the entries too, so after every `fork` or `mprotect` even reads fault again |

**The patch as built** for the first three phases of the [prototype](prototype.md#hello), against Linux v6.12: 103 added lines in 8 files and none removed (*measured on the booted prototype*). Of those, 38 are a new header and 12 a configuration option; the code in Linux's paths is the two hunks below, and everything is inert for a task whose pointer is null. With the helper, the export of `mm_alloc` and the one-line selection of preemption notifiers that the second-level scheduler added, the patch is 169 added lines in the same 8 files, none removed (*measured on the booted prototype*, fourth phase). The design of tenant memory on Linux's own page tables replaces that export with the second helper and adds the export of `flush_tlb_mm_range`, in `arch/x86/mm/tlb.c`; the patch's size with them is to be measured by the prototype's [fifth phase](prototype.md#phase5).

```c
/* include/linux/kernelet.h (new) */
struct kernelet_gate_ops {
	bool (*syscall)(struct pt_regs *regs);	/* true: serviced; skip Linux's dispatch */
	void (*resume)(struct pt_regs *regs);	/* on the way out, while exit work is pending */
};
struct kernelet_gate { const struct kernelet_gate_ops *ops; };

/* include/linux/sched.h: struct task_struct */
	struct kernelet_gate		*kernelet;	/* inherited by fork */

/* include/linux/thread_info.h, include/linux/entry-common.h */
	SYSCALL_WORK_BIT_KERNELET,			/* and its mask, added to SYSCALL_WORK_ENTER */

/* kernel/entry/common.c: syscall_trace_enter(), after the seccomp block */
	if (work & SYSCALL_WORK_KERNELET) {
		if (current->kernelet->ops->syscall(regs))
			return -1L;
	}

/* kernel/entry/common.c: exit_to_user_mode_loop(), after interrupts are enabled, before signals */
		if (unlikely(current->kernelet))
			current->kernelet->ops->resume(regs);

/* kernel/fork.c, arch/x86/mm/pat/set_memory.c */
EXPORT_SYMBOL_GPL(kernel_clone);
EXPORT_SYMBOL_GPL(set_memory_rox);
EXPORT_SYMBOL_GPL(set_memory_rw);
EXPORT_SYMBOL_GPL(set_memory_ro);

/* kernel/fork.c, added by the second-level scheduler and the design of tenant memory */
struct mm_struct *kernelet_switch_mm(struct mm_struct *new);   /* see the first helper above */
struct mm_struct *kernelet_mm_alloc(struct task_struct *owner);   /* see the second helper above */

/* arch/x86/mm/tlb.c, added by the design of tenant memory */
EXPORT_SYMBOL_GPL(flush_tlb_mm_range);
```

**Host settings, which the runtime checks.** The machine must not be set to panic, or to capture a crash dump, on an oops (`kernel.panic_on_oops` off; [why](faults-and-reclamation.md#fault)). Linux's per-fault log lines must be off (`debug.exception-trace`), or a tenant could write to the operator's log ([why](virtualizing-ostd/user-mode.md#exceptions)). And `fs.suid_dumpable` must be 0, its default ([why](#abi)). `vm.mmap_min_addr` must be at most `0x10000`, since every tenant space's area starts there and Linux refuses a mapping below that setting ([`security_mmap_addr()`](https://elixir.bootlin.com/linux/v6.12/source/mm/mmap.c#L920)); and no security module may refuse the creation of an anonymous inode, which [`anon_inode_create_getfile()`](https://elixir.bootlin.com/linux/v6.12/source/fs/anon_inodes.c#L70) asks the security modules about, since each space's file needs one of its own ([why](virtualizing-ostd/memory.md#space)). The kernel must not be a real-time one, and processors that carry latency-critical host work should be kept apart from sandboxes by `cpuset`, because a kernelet in a critical section may hold a processor for about 2 ms, two watch periods and a delivery, ([why](virtualizing-ostd/scheduling.md#cooperative)). High-resolution timers must be active, or the watch timer's period stretches to the tick's. And a host built with entry-path debugging (`DEBUG_ENTRY`) prints one warning the first time Linux preempts a carrier on a kernelet stack, which it does not recognize as a task's; the warning is harmless and fires once.

**One build configuration still open.** A Linux built with type-checked indirect branches ([assumption A19](builds-and-images.md#audit)).

**Three interactions with the rest of Linux that an operator should know.** A host security module sees a carrier create, for each tenant process, a shared, writable, executable mapping of an endovisor file, and its policy must allow that to the sandbox's user. Live patching decides that a task is safe to patch by unwinding its stack; a carrier that is preempted while on its kernelet stack cannot be unwound, so a patch transition waits until that carrier next runs a service or returns to user mode, and waits indefinitely on a runaway kernelet until it is killed. **[unverified]**: neither has been tried. And suspending the machine needs every task to stop at a point Linux considers safe. The carriers' sleeps are marked freezable as well as killable, which covers a carrier that is waiting, and the service prologue and the resume hook give Linux its chance for a carrier that is running; a kernelet that stays in its own code for longer than Linux's freezing timeout, twenty seconds by default, makes that suspend attempt fail.

**Everything a sandbox costs the host is charged to its control group**, by one of two routes, and the list is the same list that [destroy](faults-and-reclamation.md#destroy) walks: the grant, the instance's private pages, its shared pages and metadata region, carrier records, kernelet stacks, log ring, device inboxes and channel queues are allocated by a member of the group with Linux's accounting flag; carriers and device threads are members, so their processor time, their Linux task structures and stacks, the page tables of the address spaces they map into and their number (`pids.max`) are the group's. The number of kernelet stacks is bounded by the sandbox's task limit, and the number of Linux address spaces by its configured maximum of tenant address spaces, beyond which `vm_create` fails; the address space itself, its area and its page-table pages are allocated by Linux from caches and with flags that charge the allocating task's group (`SLAB_ACCOUNT`, `GFP_PGTABLE_USER`), so they count against `memory.max` like the grant. (A few hundred bytes of Linux's own bookkeeping per `vmalloc` range and per address space are not charged; that is the residue of "everything".)

What the design does *not* ask for is as important to an operator: no boot parameter, no particular preemption model (where Linux does not preempt kernel code, the endovisor [reschedules kernelet code itself](virtualizing-ostd/tasks.md#yield)), no scheduler class, hook or BPF program in Linux's scheduler ([Scheduling](virtualizing-ostd/scheduling.md) says why not), no virtualization support in the kernel, and no change to what Linux does for tasks that are not carriers, with two small exceptions the module brings while it is loaded: preemption notifiers are enabled machine-wide, which adds a test of an empty list to every context switch, and a processor that a carrier has just left may take one more watch-timer interrupt before the timer stops. A Linux with the patch applied and the module not loaded behaves as it did.

**Would the patch be accepted upstream?** Not in this form, and the design does not depend on it. A version with a chance would present the gate as the in-kernel generalization of Syscall User Dispatch, behind a configuration option, with an in-tree user. The export of `kernel_clone` would be the contentious part, and the fallback is a narrower helper that creates only "a child of the current task that starts in a given function", which is what the endovisor uses it for. `kernelet_switch_mm()` would be the second: letting a user task change address spaces outside `exec` is something Linux's memory-management maintainers have so far allowed only to kernel threads. **[unverified]**: a judgment, not tested on a mailing list.

## The device node {#abi}

The endovisor offers user space one character device, `/dev/kernelet`, root-only by default. Opening it and creating a sandbox yields a **sandbox descriptor**; every later operation is an `ioctl` on that descriptor, and closing the last copy of it kills and destroys the sandbox, so a crashed runtime cannot leak one.

| operation | effect |
|---|---|
| `KERNELET_REGISTER_KIND` | hand over an image file; the endovisor checks it and keeps its text for sharing |
| `KERNELET_CREATE` | create a sandbox of a kind, with its number of virtual CPUs, memory limits, policy and command line; returns the sandbox descriptor |
| `KERNELET_EXEC_FD` | returns the sandbox's **exec descriptor**, used once, to start it |
| `KERNELET_GRANT` | raise the sandbox's memory ceiling, or push grains to it; the kernelet finds pushed memory in its grant table |
| `KERNELET_ATTACH` | attach a virtual device, passing the descriptor of the file, block device or TAP interface behind it |
| `KERNELET_ENDPOINT` | obtain a stream descriptor for the sandbox's log or console |
| `KERNELET_CONNECT`, `KERNELET_LISTEN` | the host's end of a [channel](channels.md) |
| `KERNELET_KILL` | mark the kernelet dying, with a reason |
| `KERNELET_WAIT`, `poll` | learn that it has exited, and why |
| `KERNELET_STATS` | memory granted, service calls, interrupts raised, log records dropped, caught panics |
| `KERNELET_DESTROY` | reclaim everything; legal once the kernelet has exited |
| `KERNELET_UNREGISTER_KIND` | drop a kind with no instances, and return its text frames to Linux |

**Starting is not an `ioctl`.** A sandbox starts when a process executes its **sandbox file**. The file is not on any file system: it is a small in-memory file that the endovisor itself creates (with Linux's exported [`shmem_file_setup()`](https://elixir.bootlin.com/linux/v6.12/source/mm/shmem.c#L5265)), containing a magic number, and the runtime receives it only as the exec descriptor, which it executes with `execveat()`. The endovisor makes the file execute-only and owned by root (the helper creates it readable and writable by all, so the endovisor resets the mode itself). That is deliberate: `exec` resets the *dumpable* attribute that guards a process against `ptrace` and `/proc` inspection by others of the same user, so clearing it beforehand would not last, but Linux's own rule is that a process which executes a file it cannot read is *not* dumpable, and every carrier inherits the root carrier's setting. The rule depends on the host's `fs.suid_dumpable` setting being 0, its default, which the runtime checks along with the other host settings. **[unverified]**: read from `begin_new_exec()`, not tried. The endovisor's [program loader](virtualizing-ostd/tasks.md#root) recognizes the magic and identifies the sandbox *by the file object*, which it made, not by a secret that could leak. It then checks that the sandbox is in the *created* state, that the executing process has *no new privileges* set and is not gaining privileges by this `exec`, and that its control group is not the root group; marks the descriptor used; and turns the process into the root carrier. Starting by `exec` is what lets the runtime prepare that process with ordinary Linux tools first, because everything it sets up is inherited by every carrier: the control group that bounds and accounts the sandbox, the user and the namespaces it runs as, and the seccomp filter.

## The life of a sandbox, in order

1. The runtime registers the kind (once per machine), creates the sandbox and attaches its devices.
2. The runtime forks a child, which enters the sandbox's control group, drops to the sandbox's credentials, installs the [seccomp filter](virtualizing-ostd/user-mode.md), and executes the sandbox file.
3. The endovisor loads the instance, grants its initial memory (now charged to that control group), and makes the child the root carrier, which clones one carrier per virtual CPU.
4. vOSTD initializes on virtual CPU 0 and starts the others; the kernel proper injects its scheduler, boots, mounts its root file system from the virtio disk, and starts the tenant's `init`, whose `VmSpace` is the first tenant address space, made by `vm_create`.
5. The sandbox runs. The kernelet's scheduler decides which of its tasks run on its virtual CPUs, and Linux decides when the virtual CPUs run; every system call goes through the gate to the kernelet; every page of tenant memory enters Linux's page tables through the endovisor's `vm_map`, checked against the grant.
6. The kernel proper powers off, or the runtime kills the sandbox. The endovisor marks it dying, stops every carrier, reports the exit, and on `KERNELET_DESTROY` returns the memory.

## Size

**[unverified]**: the endovisor has not been written. The prototype's module, which implements the gate operations, the program loader and root carrier, carriers, the stack switch, the memory areas with the model walk, lifelines, eviction, the yield stub, and the services three small kernels need, for one kernelet, is 2,675 lines of C (*measured on the booted prototype*). The device models are the largest remaining part; their Rust equivalents for the other host are *estimated* at a few thousand lines.

## What this page decides

- **The endovisor is one C module, and Linux mode requires a patched Linux** ([register D79](../../notes/design-register.md#decisions), kept, and D80, revised; D120, D125, D127): the gate, two helpers and five exports. An unpatched Linux is ruled out by function, not speed: without the gate, a tenant's second process makes its system calls to Linux.
- **A sandbox starts by `exec` of a sandbox file and is owned by a descriptor** ([register D105](../../notes/design-register.md#decisions)). The alternative, a start `ioctl` that turns the calling thread into the root carrier, leaves the runtime's own address space in the root carrier, to be copied into every clone.
