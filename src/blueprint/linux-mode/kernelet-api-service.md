# The kernelet API: service half

*The complete list of what a running kernelet can ask of its host, given as the C interface it is, and what each request becomes on Linux. This table is the boundary: what is not on it, a kernelet cannot do.*

## What the service half is

A kernelet image has no undefined symbols, so it cannot call Linux. When the endovisor enters an image it passes a pointer to the **service table**: a C structure of twenty function pointers, the same for every kernelet on the machine. vOSTD calls through it whenever a [virtualized item](virtualizing-ostd/README.md) needs something only the host can give. Each such call is a **crossing**: an indirect function call at the same privilege, with no trap and no address-space switch. *Measured on the booted prototype* of the Asterinas host, a crossing cost 35 cycles against 34 for a plain call; on Linux a crossing also changes stacks ([below](#depth)), which *measured on the booted prototype* of this design at under ten cycles for the pair of switches.

The image exports the mirror image, an **entry table**, which is how the endovisor enters the image. The two tables, the shared pages and the constants are the **image ABI**. It is defined once, in a Rust module that vOSTD compiles, and the C header below is generated from that module; the endovisor's build fails if any size or offset disagrees.

The ABI has two parts. The services for memory, devices, output and the end of a kernelet's life are the same whichever kernel is the host. The services that stand for *the processor* are this host's own: on Linux a kernelet is given [virtual CPUs](virtualizing-ostd/tasks.md) and runs its own tasks on them, so where the Asterinas host's table has calls to create, park and wake host threads, this one has calls to start, idle and kick virtual CPUs.

Three rules shape every function in the table.

- **Arguments are integers**: virtual CPU numbers, device numbers, physical addresses, lengths. A physical address is checked against the kernelet's grant before it is used.
- **No function returns a result through a pointer**, with two exceptions that are named below and range-checked. Results come back in registers. A host that wrote through a kernelet-supplied pointer would be writing wherever a buggy vOSTD told it to.
- **The host keeps no pointer into a kernelet after a call returns.** Its tables hold identifiers and physical addresses.

## The image ABI {#abi}

```c
/* kernelet_abi.h: generated from vOSTD's ABI module, Linux host */

/* Negative returns. 1 is unused: a dying kernelet's call never returns. */
#define KLET_NOT_OWNED  2                /* a physical address outside the grant */
#define KLET_INVALID    3                /* a bad virtual CPU, device, width, range or pointer */
#define KLET_LIMIT      4                /* a configured ceiling reached, or Linux had no memory */
#define KLET_STATE      5                /* a call that gives up the processor, made with guards held */

struct klet_user_ctx {                   /* vOSTD's own layout, which it fills from OSTD's UserContext, FsBase and GsBase */
        uint64_t rax, rbx, rcx, rdx, rsi, rdi, rbp, rsp;
        uint64_t r8, r9, r10, r11, r12, r13, r14, r15;
        uint64_t rip, rflags, fsbase, gsbase;
        uint64_t trap_num, error_code, fault_addr;   /* filled in when user_run returns 1 */
};

/* Bits of a virtual CPU's pending word; set by the endovisor, taken by vOSTD */
#define KLET_VIRQ_TICK   (1ull << 0)
#define KLET_VIRQ_KICK   (1ull << 1)
#define KLET_VIRQ_LINES  (1ull << 2)     /* some bit of pending_lines[] is set: device lines 32..255 */

struct klet_vcpu_rec {                   /* one per virtual CPU, on a shared page; see "The shared pages" */
        _Atomic uint64_t pending;        /* endovisor sets bits; vOSTD swaps to zero */
        _Atomic uint64_t pending_lines[4];
        uint32_t guards;                 /* vOSTD: the count of preemption guards and preemption-disabling spin locks held
                                            (the count only; an interrupt-disabling lock raises irq_off instead) */
        uint32_t irq_off;                /* the virtual interrupt flag, inverted. vOSTD sets and restores it for OSTD's
                                            interrupts-off guards; the endovisor sets it when it redirects to virq_entry (having pushed the interrupted ip onto the interrupted stack),
                                            as a processor turns interrupts off when it takes one; vOSTD clears it when
                                            the upcall ends. Linux's preemption count is changed only by vOSTD, never at a redirect */
        uint32_t vcpu;                   /* this virtual CPU's number, behind OSTD's CpuId */
        uint32_t reserved;
        uint64_t stack_limit;            /* vOSTD: the current task's kernelet-stack limit, for the entry check */
        uint64_t cpu_local_base;         /* endovisor, once: this virtual CPU's copy of the per-CPU section */
        uint64_t stolen_ns;              /* endovisor: total time the carrier was runnable but off a processor */
        uint32_t mirrored;               /* vOSTD: 1 while vOSTD's increment of Linux's count is outstanding */
        uint32_t reserved2;
        /* Only pending and pending_lines are written from other processors. vcpu and cpu_local_base
           are written once, before the carrier runs. Every other field is written and read by this
           virtual CPU's own carrier, and by the endovisor on the same processor, in the watch
           timer's callback and the carrier's preemption notifier. */
};

#define KLET_KSTACK_BYTES (256 * 1024)   /* every kernelet stack; the kernelet build sets OSTD's stack size to match */

#define KLET_STOP_EXIT   0
#define KLET_STOP_PANIC  1
#define KLET_STOP_STACK  2

struct klet_mmio_result { int64_t status; uint64_t value; };   /* returned in two registers */

struct klet_service_table {
        uint64_t size;                                           /* of this structure, for versioning */
        /* memory */
        int64_t  (*grains_request)(uint32_t count, uint32_t contiguous);   /* >= 0: grains granted, possibly fewer than count;
                                                                              contiguous: how many of them must form one run (0 or 1: no constraint) */
        int64_t  (*pt_root_register)(uint64_t root_paddr);
        int64_t  (*pt_root_unregister)(uint64_t root_paddr);
        int64_t  (*pt_activate)(uint64_t root_paddr);            /* 0 means "no tenant address space" */
        int64_t  (*tlb_shootdown)(uint64_t root_paddr, uint64_t start, uint64_t len);   /* len == UINT64_MAX: everything */
        uint64_t (*kstack_alloc)(void);                          /* the lowest address of a stack of KLET_KSTACK_BYTES; 0: none (ceiling, or no memory) */
        int64_t  (*kstack_free)(uint64_t vaddr);
        /* virtual CPUs */
        int64_t  (*vcpu_boot)(uint32_t vcpu);                    /* let that carrier enter the image */
        int64_t  (*vcpu_idle)(uint64_t deadline_ns);             /* sleep until something is pending; UINT64_MAX: no deadline */
        int64_t  (*vcpu_kick)(uint32_t vcpu);
        void     (*vcpu_yield)(void);                            /* Linux wants this processor: reschedule now */
        void     (*vcpu_on_spin)(void);                          /* I am spinning on a lock: run a sibling that is not running */
        /* user mode */
        int64_t  (*user_run)(struct klet_user_ctx *ctx);         /* 0 system call, 1 exception, 2 look at pending */
        int64_t  (*fpu_save)(void *area, uint32_t len);          /* the live user floating-point state -> area; len <= the size in the boot arguments */
        int64_t  (*fpu_load)(const void *area, uint32_t len);
        /* devices */
        struct klet_mmio_result (*mmio_read)(uint32_t dev, uint32_t offset, uint32_t width);
        int64_t  (*mmio_write)(uint32_t dev, uint32_t offset, uint32_t width, uint64_t value);
        /* output and end of life */
        int64_t  (*log_write)(uint32_t level, const char *module, uint32_t module_len, const char *text, uint32_t text_len);
        int64_t  (*oops)(const char *msg, uint32_t len);
        void     (*stop)(uint32_t kind, uint32_t code, const char *msg, uint32_t len);   /* does not return */
};

struct klet_entry_table {                /* in the image, 4 KiB from its base; read-only */
        uint64_t size;
        void     (*vcpu_entry)(uint32_t vcpu);                   /* where a secondary virtual CPU enters */
        uint64_t virq_entry;                                     /* the upcall stub: an address, never called as a function */
        uint64_t cpu_local_start, cpu_local_end;                 /* the section to copy once per virtual CPU */
        uint8_t  source_hash[32];
};
/* Virtual CPU 0 enters at the ELF entry point:
   void _kernelet_entry(const struct klet_service_table *, const struct klet_boot_args *) */
```

The endovisor never sees a closure or a task of the kernelet. It transfers control into an image at three addresses only: the ELF entry point, the entry table's `vcpu_entry`, and, by [redirecting a virtual CPU that is already running kernelet code](virtualizing-ostd/scheduling.md#upcall), the entry table's `virq_entry`. It checks at registration that all three lie in the image's text. Every other arrival in kernelet code is a *return*: from a service call, from the [yield stub](virtualizing-ostd/tasks.md#yield), or from user mode into the `user_run` that left.

## Who is calling {#depth}

No service takes a "which kernelet" argument, because a kernelet must not be able to claim to be another. The endovisor finds the caller from Linux: the current task's [gate pointer](virtualizing-ostd/user-mode.md#gate) leads to its **carrier record**, which names the kernelet and the virtual CPU, and holds the **service-call depth** and the saved stack pointers of the [stack switch](virtualizing-ostd/tasks.md#stacks). The carrier record is endovisor memory, which nothing in the kernel proper can name. (The virtual CPU's *shared* record, above, is a different object: it is what the two sides use to talk, and nothing in it is believed for a decision that protects the host, except as stated under [The shared pages](#pages).)

Every service function is wrapped in the same prologue and epilogue.

**On the way in**: find the carrier record; if the kernelet is marked dying, do not return to it at all but [leave the kernelet for good](faults-and-reclamation.md#leaving) (`stop` is exempt); for a service that may sleep, take out of Linux's preemption count whatever the kernelet's guards have added to it ([why they add to it](virtualizing-ostd/scheduling.md#cooperative)), and remember how much; switch to the carrier's Linux stack; set the depth to 1; count the call for the sandbox's statistics.

**On the way out**: set the depth to 0; if the kernelet was marked dying meanwhile, leave for good instead of returning; re-arm the [watch timer](virtualizing-ostd/tasks.md#watch) if needed; switch back to the kernelet stack; put the remembered amount back into Linux's count.

How much the guards have added is not something the endovisor asks the kernelet. Whenever a carrier passes from the endovisor's code into kernelet code (from its start function, from a gate hook, on return from a service or from the yield stub), it does so in task context with Linux's count at a value the endovisor records. The upcall is not such a passage: the carrier is in kernelet code before and after the redirect, and nothing is recorded. In task context, whatever the count exceeds the recorded value by is the kernelet's doing, and by vOSTD's rule it is 0 or 1 ([one increment for being in a critical section](virtualizing-ostd/scheduling.md#cooperative), however deep the nesting). Recorded, compared and restored is the count proper, as Linux's `preempt_count()` reads it, with the folded "reschedule wanted" bit masked off, and through Linux's own add and subtract; the recorded value is always zero, since Linux's entry code and its fork-return path both arrive with the count at zero, and the endovisor asserts it. Every place that takes a carrier out of kernelet code, a service prologue, the [yield stub](virtualizing-ostd/tasks.md#yield), the [exit stub](faults-and-reclamation.md#leaving) and the die notifier on any carrier's oops, whether or not it routes to the exit stub, restores the recorded value by that arithmetic, so a kernelet that miscounts cannot leave Linux's count wrong behind it; an excess other than 0 or 1 is a bug in vOSTD, and the endovisor kills the kernelet for it.

The depth is 1 exactly while the carrier, having come from kernelet code, is inside endovisor or Linux code and may hold their locks. It is one of the two tests that make [eviction](faults-and-reclamation.md#eviction) safe.

**Which calls give up the processor.** `vcpu_idle`, `vcpu_yield`, `vcpu_on_spin` and `user_run` exist to let something else run: in the first three Linux may run another task on this processor, and in the last the tenant runs. Two of them are for long absences, and OSTD makes them with its guards in a known state: `execute` turns interrupts off on purpose and holds no preemption guard, and `halt_cpu` holds nothing (its bare-metal body turns interrupts off and back on around the halt; vOSTD's body does not). A `vcpu_idle` or a `user_run` that arrives with a preemption guard held is refused with `-KLET_STATE`, before its prologue has touched anything, and vOSTD treats the refusal as what it is, a bug in its own use of the interface, and panics. That check reads the record's `guards`, which the kernelet writes, so it protects the kernelet from its own bugs and not the host from the kernelet. The other two are brief and are legal in any state: `vcpu_yield` is called as the outermost guard drops, and `vcpu_on_spin` from the slow path of a spin lock, which OSTD enters with a guard already held. `user_run` can also block in Linux before it leaves, when it has to create the [memory area](virtualizing-ostd/memory.md#cache) of an address space it is the first to run in.

**Which calls can block inside Linux.** `grains_request`, `kstack_alloc`, `pt_root_register` and `pt_root_unregister` while Linux allocates or frees memory, and `tlb_shootdown` while it waits for fault handlers to finish; with the four that give up the processor, those are the services whose prologue removes the kernelet's increment. The others (`pt_activate`, `kstack_free`, `vcpu_boot`, `vcpu_kick`, `fpu_save`, `fpu_load`, `mmio_read`, `mmio_write`, `log_write`, `oops`) never sleep and run with the increment in place, which is what lets an [upcall handler](virtualizing-ostd/scheduling.md#upcall) call them while the interrupted task holds a lock; an upcall handler must call no other, as an interrupt handler on a machine must not sleep. The virtual CPU simply does not run meanwhile, as a processor that waits for a TLB flush does not. `tlb_shootdown` cannot be allowed to fail, so it is never refused.

A carrier inside a service call cannot be [evicted](faults-and-reclamation.md#eviction), so the endovisor owes a bound on every one of them. The rule is that **every sleep inside a service is killable**: the waits use Linux's killable forms, and the sweep's `SIGKILL` ends them. The remaining services do bounded work without sleeping.

**Stack.** A service call does not run on a kernelet stack. Each entry in the table is a short stub in the endovisor that switches to the carrier's Linux stack, runs the prologue, the service and the epilogue there, and switches back. Linux's code then runs on the stack it was written for, one that Linux's overflow detection and backtraces know about, and how deep a service goes into Linux no longer depends on how deep the kernelet was when it called. The Linux stack is nearly empty at that moment: it holds only the frames of the gate hook or start function that entered the kernelet.

## What each service becomes on Linux

| service | on Linux |
|---|---|
| `grains_request` | page allocator, charged to the sandbox's control group; zero; record in the owner array, then publish in the grant table ([Memory](virtualizing-ostd/memory.md)). Grants fewer than asked when Linux has less; `-KLET_LIMIT` at the ceiling and during the interval after a failure; `-KLET_INVALID` for a contiguous run longer than the allocator's largest block with no reserved pool |
| `pt_root_register` | check that the root frame is in the grant; create the model's record, its file object, and a Linux address space for it; `-KLET_LIMIT` at the configured maximum of models, and refused for a short interval after Linux failed to allocate one |
| `pt_root_unregister` | looked up among this kernelet's models; refused while a virtual CPU has the model activated; otherwise empty its cache and release its Linux address space. vOSTD calls it only when the last task has let go of the address space, and must not reuse the root frame until it has returned |
| `pt_activate` | look the root up among *this kernelet's* registered models, and refuse (`-KLET_INVALID`) one that is not there; record the model as this virtual CPU's current tenant address space; the carrier adopts the corresponding Linux address space at the next `user_run` |
| `tlb_shootdown` | look the root up among this kernelet's models, as `pt_activate` does; wait out fault handlers on this model, then `unmap_mapping_range()` on the model's file ([Memory](virtualizing-ostd/memory.md#interlock)); 0 for a root that names none of them, since nothing is cached for it |
| `kstack_alloc`, `kstack_free` | take a stack from, or return it to, the sandbox's [pool of kernelet stacks](virtualizing-ostd/tasks.md#stacks); the pool grows by `__vmalloc` with the accounting flag, charged to the sandbox, up to the configured maximum of tasks (0 beyond), and gives nothing back to Linux before destroy. `kstack_free` of an address that is not a stack in use, or of a boot stack, or of the stack that a virtual CPU's last saved kernelet stack pointer lies in (the check the endovisor can make; a kernelet that frees the stack it is running on is its own victim), is `-KLET_INVALID`. The kernelet build sets OSTD's stack size to match (`OSTD_TASK_STACK_SIZE_IN_PAGES=64`) |
| `vcpu_boot` | wake the carrier of that virtual CPU, which enters the image at `vcpu_entry`; `-KLET_INVALID` for a number out of range, for virtual CPU 0, and for one already started |
| `vcpu_idle` | killable, freezable sleep until a bit is pending, the deadline (rounded up to 50 µs from now) passes, or the kernelet is dying ([Interrupts and time](virtualizing-ostd/interrupts-and-time.md)); returns 0 |
| `vcpu_kick` | set KICK in the target's record and do the least that will get it seen ([Scheduling](virtualizing-ostd/scheduling.md#upcall)); nothing at all if KICK was already set, so a storm of kicks costs one delivery; `-KLET_INVALID` for a number out of range; kicking oneself only sets the bit |
| `vcpu_yield` | `schedule()` if Linux wants the processor (`need_resched()`); nothing otherwise |
| `vcpu_on_spin` | at most once per millisecond per virtual CPU: [`yield_to()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/sched/syscalls.c#L1468), without the option that preempts the target's processor, a carrier *of the same sandbox* that is runnable and not running, if there is one; otherwise as `vcpu_yield` |
| `user_run` | adopt the address space, switch to the Linux stack and return to user mode through the gate ([User mode](virtualizing-ostd/user-mode.md#user-run)) |
| `fpu_save`, `fpu_load` | under Linux's `fpregs_lock()`, which holds off bottom halves and may run the ones that became pending when it is released, on the carrier and charged to it, make the user floating-point registers live if they are not, then save them to, or load them from, a staging buffer in the carrier record; the kernelet's buffer is copied to or from the staging buffer outside the lock, and what is loaded is validated in the staging copy ([User mode](virtualizing-ostd/user-mode.md#fpu)) |
| `mmio_read`, `mmio_write` | check device, offset and width (1, 2, 4 or 8); call the device model ([Devices](virtualizing-ostd/devices.md)) |
| `log_write` | copy at most 1 KiB into the sandbox's log ring, rate-limited |
| `oops` | count a caught panic against the oops budget |
| `stop` | mark dying with the kind, code and message; the calling carrier then [leaves for good](faults-and-reclamation.md#leaving) from inside the service, which is why the call never returns |

**Pointers.** Six services take one. `user_run`'s context must lie on the current kernelet stack, and the endovisor copies it rather than using it in place. `fpu_save` and `fpu_load` name a buffer in kernelet memory; together with `user_run`'s context they are the only places where the host writes into a kernelet, and it does so with Linux's non-faulting kernel copy after checking that the range lies in *this* instance's data, in a stack currently handed out from *this* sandbox's pool, or in *this* kernelet's grant; any other range, another sandbox's included, is `-KLET_INVALID`. The endovisor never acts on such memory in place, since a frame of the grant may also be mapped into a tenant that is rewriting it: it copies once, and validates and uses the copy. The text arguments of `log_write`, `oops` and `stop` are read the same way ([`copy_from_kernel_nofault()`](https://elixir.bootlin.com/linux/v6.12/source/mm/maccess.c#L24)), so a bad pointer is `-KLET_INVALID` and never a host fault.

## The shared pages {#pages}

Some information is cheaper to read than to ask for. The endovisor maps five kinds of page after each instance's data; all are read-only to the kernelet except the last.

| page | contents | written |
|---|---|---|
| **boot arguments** | identity, number of virtual CPUs, direct-map base, metadata base, the bounds of the user range a model may cover, the timestamp counter's frequency, the size of the floating-point state, where the other pages are, device list, command line; and four numbers by which vOSTD finds its way from Linux's per-processor data: the `%gs`-relative offsets of Linux's current-task pointer and of its preemption count, the offset of the gate pointer in a task, and the offset of the record pointer in a carrier record; on a Linux built without a preemption count the count's offset is given as −1, and vOSTD leaves the mirror out | once, before entry |
| **grant table** | the base and length of each run | appended by the endovisor when it grants |
| **info page** | the number of runs in the grant table, the grant's ceiling | by the endovisor |
| **clock page** | coarse ticks and monotonic nanoseconds; one page for the whole machine | by one machine-wide timer |
| **virtual CPU records** | `struct klet_vcpu_rec`, one per virtual CPU | by both, as its comments say |

vOSTD always reaches Linux's two per-processor words by a single `%gs`-relative instruction and never through a computed address, because a carrier may be on a different processor the next time it looks.

What the endovisor believes from a virtual CPU's record is limited and deliberate. It believes `irq_off` for whether to redirect to the upcall now or leave the bit pending; it believes `guards` for whether a `vcpu_idle` or `user_run` is legal, and, on a Linux that does not preempt kernel code, together with `irq_off`, for whether this is a polite moment for the [yield stub](virtualizing-ostd/tasks.md#yield). Both affect only the kernelet, and the second is overruled after [2 ms of run time](virtualizing-ostd/scheduling.md#cooperative) without a real scheduling event, which the endovisor measures without reading the record. For nothing that protects the host does it read the record; in particular it decides how much stack an upcall has from its own pool records, not from `stack_limit`, which only vOSTD's entry check reads. The interrupted instruction pointer that an upcall returns to is not in the record at all; it goes onto the interrupted kernelet stack, which no tenant can reach. The service-call depth, the dying mark, Linux's preemption count and everything else that protects the host are in endovisor memory or in Linux's. The header above gives the tables and the record in full; the byte layouts of the other pages are fixed by the same generated header and are not reproduced here.

## What is not offered

There is no service to read or write host memory, to map anything, to allocate a frame by address, to disable interrupts, to send an interrupt to a processor, to load a page table into the processor, to change a carrier's Linux priority, class or affinity, or to call a host function by address. Their absence from the table is what makes them absent from a kernelet. The image cannot name what the table does not contain.

## What this page decides

- **The service table's memory, device and output groups are the same on both hosts; its processor group is this host's own: virtual CPUs in place of host threads** (register D116; D2 and D65 kept). A kernelet image for Linux is built from the same source, with OSTD's own task layer in place of the virtualized one.
- **Service calls run on the carrier's Linux stack, with the kernelet's guard depth taken out of Linux's preemption count for their duration** (register D112, D119).
- **The caller is identified through Linux's current task, and the depth and dying state live in endovisor memory** (register D7 and D8, adapted). The alternative, state in shared pages, would let a kernelet make itself unstoppable.
