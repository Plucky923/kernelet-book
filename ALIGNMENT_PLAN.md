# Bringing the two host designs into line

*A plan, not a revision. Nothing under `src/` is changed by the commit that adds this file. It answers three questions: how the two design chapters compare today, what shape the Blueprint should take, and in what order to get there. Written 2026-09-22 against `main` at `a2188d0`, then revised against three independent reviews of the plan itself (§9), and again on 2026-09-27 when the owner decided the Paper question of §0.*

---

## 0. The Paper: decided, and recorded as a debt

**Decision, 2026-09-27: the Paper waits.** The owner was asked and chose to hold the line on scope. This section records what that costs, precisely enough that the debt can be paid later without rediscovering it — and carves out the two sentences that are *not* in the Paper and therefore not deferred.

The reasoning that made this a question in the first place follows.

`AGENTS.md`: *"where a Note disagrees with the Blueprint, the Blueprint wins, and where either disagrees with the Paper, the **Paper** wins."*

The Paper is not merely decorated with the old task model; it is built on it:

| where | what it says | after the back-port | in scope? |
|---|---|---|---|
| `paper/introduction.md` | "Its threads are threads of the host kernel, scheduled by the host" | false | **deferred** |
| `paper/introduction.md` | machine virtualization is indicted because "a second scheduler runs beneath the guest's" | the indictment now also fits a kernelet | **deferred** |
| `paper/api-virtualization.md` | "there is **one scheduler**", and Table 1's `| Schedulers | two | one | one |` | false | **deferred** |
| `overview/api-virtualization.md` | the same table row, `| Schedulers | two | one | one |`, third column API virtualization | **false** | **kept** — the Overview is in the Blueprint |
| `overview/challenges.md` | "every deferred piece of work must run on the kernelet's own **worker task** and be charged to it" | **false**: the worker task is deleted | **kept** |
| `overview/goals.md` | "A second scheduler runs beneath the guest's, so the guest's decisions about which thread to run are made twice" | still true *of machine virtualization*, but the contrast it draws collapses | **kept**, one clause |

The back-port installs a second scheduler on both hosts and deletes the worker task. Two Overview sentences therefore become plainly false, and the Overview is inside the Blueprint and inside the stated scope, so they are fixed in pass 3 (§6). The four Paper sentences wait.

**What the debt is, exactly.** After pass 2 the Blueprint will be internally consistent and say *two schedulers*; the Paper will say *one*; and `AGENTS.md` says that where the two disagree, **the Paper wins**. A reader following the book's own precedence rule will therefore get the wrong answer about the central mechanism until the debt is paid. That is sharper than leaving both out of line, not softer, and it is the price of the decision.

**When it should be paid.** Before `paper/design.md` and `paper/implementation.md` are written. Both are empty stubs today, so the fix is four sentences and a table cell; written against "one scheduler" first, they become a rewrite. That is the trigger to watch, not a date.

**What the fix will be, when it comes**: a decision in writing about what "one scheduler" now claims — most likely *one scheduler of processors, and no hardware exit beneath it*, which stays true after the back-port and is still the argument against machine virtualization — and the four sentences edited to match.

One thing worth knowing while the debt stands: the Paper is *already* out of line with the Blueprint independently of this plan. `paper/api-virtualization.md` still describes the Linux host with "per-CPU data is selected by a **seat**" and "every kernelet task is carried for life by a Linux task", both retired by D88 and D116 in the last run. Whoever pays this debt should sweep those at the same time.

---

## 1. What was compared

| | Design (Asterinas as host) | Design for Linux |
|---|---:|---:|
| pages | 17 | 22 |
| words, raw | 56,366 | 65,195 |
| words, prose without figures or code | 48,876 | 56,572 |
| booted evidence | none | four prototype phases |

Both chapters were read at the level of their mechanisms; the two task pages, the two service halves, the two interrupt pages and the two boundary pages line by line. The Overview (four pages), the design register (122 decisions, 34 assumptions) and the OSTD API inventory (135 rows) were read for what they already promise about the relationship between the chapters.

The finding that organizes everything below: **the two chapters do not differ because their hosts differ. They differ because one of them was revised later.**

---

## 2. Status: where the two chapters stand

### 2.1 Where Linux is now better

Several of these are one finding seen from several sides: on Asterinas *every kernelet task is a host thread* (D61); on Linux a host task carries *a virtual CPU* (D116).

| mechanism | Design (Asterinas) | Design for Linux | verdict |
|---|---|---|---|
| what a host task is | one per kernelet task, with a 512 KiB host kernel stack each (*measured on the tree*), charged to the sandbox and bounded by `max_tasks` (D64) | one per virtual CPU, fixed at sandbox creation | **Linux** |
| the kernel proper's scheduler | **inert** (D15): `inject_scheduler` stores a reference nothing calls | decides which task runs, exactly (D116) | **Linux** |
| proportional share between sandboxes | none: a sandbox's share is "the sum of its threads' shares, as a user's is on Linux without cgroups"; a tenant buys share by running more threads | the carrier count fixes it; *measured*: a neighbor's share moved 1.2 % between 1 and 50 runnable kernelet tasks | **Linux** |
| a task switch inside a sandbox | `task_park` + `task_unpark`: two crossings and two host scheduler operations | OSTD's own context switch, no crossing; *measured*: 542 ns round trip | **Linux** |
| virtual interrupts | a job fetched by a per-virtual-CPU **worker thread** (D9, D11): a wakeup and a context switch per interrupt | a pending bit and an **upcall** on the interrupted task (D117) | **Linux** |
| kernel-mode preemption | the host runs OSTD's switch protocol from the trap-return path (D16), resting on **A8, unverified** | the kernelet switches its own tasks inside the upcall (D118) | **Linux** |
| user FPU and TLS | the host saves an XSAVE area and the FS/GS bases on every involuntary switch (D17) | per tenant thread, through OSTD's own `FpuContext` (D121) | **Linux** |
| RCU | per-virtual-CPU, with an extended quiescent state the **host** sets (D32), resting on **A9, unverified** | the kernelet's own, at its own switch points | **Linux** |
| idling | no idle threads; two `cfg` lines in the kernel proper (D67) | the kernel proper's own idle task calls `halt_cpu` (D116, D122) | **Linux** |
| alternatives | scattered through "what this page decides" | a page of their own, with the four criteria a design must meet | **Linux** |
| background for an outside reader | none | two pages | **Linux** |
| evidence | every claim *measured on the tree*, *estimated* or *argued* | a prototype that boots, with four phases of measurement | **Linux** |

Two rows are worth stating precisely, because an earlier draft of this plan overstated both.

**Proportional share, not "fairness", is what Asterinas lacks.** `overview/goals.md` defines fairness as keeping one tenant's consumption from starving another, *charged to it and bounded*; D62's quota is a hard ceiling and delivers that. What is missing is that two sandboxes of equal weight get equal share regardless of how many threads each runs. The Design chapter says so itself: "a sandbox's share of the machine is the sum of its threads' shares". Proportional share is what `terminology.md` says is still "to be stated precisely"; the Linux chapter now delivers it and the Asterinas chapter does not.

**The Linux chapter's Alternatives page already judges the Asterinas design.** Its section *For tasks* lists "a Linux task for every kernelet task" among the losers, on three counts: the kernel proper's scheduler is inert, a sandbox costs the host a task structure and a stack per tenant thread, and the threads of one process do not share a page table. The first two are Chapter 12's design. (The third is Linux-specific and does not apply.) The book argues against itself, in one direction only.

### 2.2 Where Asterinas is better

| mechanism | Design (Asterinas) | Design for Linux | verdict |
|---|---|---|---|
| tenant page tables | the kernelet's own, walked by the processor; `pt_activate` writes CR3 | a **model** the kernelet writes and a **cache** in Linux filled by a fault handler | **Asterinas** |
| copies to tenant memory | an ordinary dereference | a software walk of the model, *measured* at 12.1 ns for a small copy | **Asterinas** |
| the control half | a Rust API with typed hooks, 5,384 words | `ioctl`s on a device node, 1,322 words | **Asterinas** |
| zero-copy I/O | the full lending design: rings, entries, the lend count, block and network | what Linux offers a lent frame | **Asterinas** |
| channels | the switch, credit, the ownership-transfer extension | the shape only | **Asterinas** |
| the OSTD item classification | 72 rows, item by item | **links to the Asterinas chapter for it** | **Asterinas** |
| the tick | counted into a shared record, no timer armed per processor (D66) | a per-processor watch timer, one hard-interrupt callback per millisecond | **Asterinas**, partly (§4.1) |

Three further advantages of today's Asterinas design are not mechanisms but consequences, and the back-port gives them up. They belong in the argument, not in a footnote:

- **The host can see inside a sandbox.** Every tenant thread is a host thread today, so the host's scheduler load-balances *within* a sandbox's CPU set, per-thread CPU charging is the host's own, and host-side tooling can see what a sandbox is doing. After the back-port a sandbox is as opaque to the host as a virtual machine is to a hypervisor.
- **The kernel proper's scheduler becomes load-bearing.** Today it is dead code inside a kernelet; after the back-port it places every tenant thread. The Linux evidence for "the policy is obeyed" was gathered with a purpose-written 1,100-line test scheduler, and the register marks A33 **[unverified]** for the real kernel proper. The back-port promotes a piece of the kernel proper from unused to critical without evidence that it is good.
- **On Asterinas, "a kernelet task is a host thread" is natural, not a workaround.** The host and the kernelet are the same codebase; sharing the task abstraction is the obvious design, and D61's rejected alternative ("OSTD creating bare host `Task`s") was rejected for a real reason — the host's class scheduler only enqueues tasks that are `Thread`s.

### 2.3 What the two chapters already share

Read side by side, these are the same text written twice, differing in wording and in which host's facility is named: the parties and their trust; the four interfaces and what a crossing is; the threat model, the three properties and invariants I1–I8; the image's shape and the audit; the rules of the service table, the depth, the prologue and epilogue; identity by generation-stamped ids; the life cycle and the drain list; the grant, grains, runs, the owner array and metadata; devices as virtio over function calls; channels as vsock through a switch; the lending device model; and "what a tenant can never reach".

That is most of both chapters' scaffolding. The duplication is deliberate — `AGENTS.md` requires the Linux chapter to be readable alone — and it is now the main reason the two drift. The register documents the drift in its own voice: D88's row says the seat abstraction was "not adopted for Asterinas mode here, because that would change `CpuId`'s meaning in **a chapter this exploration did not review**".

### 2.4 The verdict

Chapter 12 is not inferior in workmanship; it is inferior in *design*, in one place, and that place has consequences all over it. Its task model costs it proportional share, flexibility, efficiency and two unverified assumptions, and it forces machinery (workers, the trap-return switch, host-saved FPU state, host-set RCU quiescence, no-idle-threads `cfg` lines, eight of its twenty-one services) that the newer model does not need. It buys, in exchange, host visibility and a simpler story about what a task is.

So the answer to "should we extract the common part into a new chapter" is: **yes, but not first.** Extracting first would freeze the divergence into a chapter that says "on Asterinas tasks are host threads, on Linux they are OSTD tasks". Back-porting first makes tasks, scheduling and interrupts common, and then the extraction has something worth extracting.

---

## 3. The shape proposed

### 3.1 Three chapters, named symmetrically

```
The Blueprint
├── Overview                    goals, API virtualization, terminology, why it is hard
├── Design                      NEW — the host-independent design: the contract, and
│                                     everything a kernelet is on any host
├── Design for Asterinas        what the Asterinas host does to meet it   (today's "Design")
└── Design for Linux            what Linux does to meet it                (today's Chapter 13)
```

**Directories.** `src/blueprint/asterinas-mode/` is today the Asterinas chapter, so the names must be settled before pass 3 starts. Following the precedent of `linux-mode/`:

| chapter | directory |
|---|---|
| Design (common) | `src/blueprint/asterinas-mode/` — reused, its meaning widened |
| Design for Asterinas | `src/blueprint/asterinas-mode/` — today's `design/`, moved with `git mv` |
| Design for Linux | `src/blueprint/linux-mode/` — unchanged |

*Counted*: the move costs 120 link rewrites (96 in the design register, 17 in `SUMMARY.md`, 4 from the Linux chapter, 3 from the Executive Summary and the Notes), all mechanical; the 40 relative links inside the chapter survive a directory rename untouched, and the register rows are being rewritten in the same pass anyway.

**Index pages.** `AGENTS.md` requires one `index.md` per chapter directory and `make check` reports an orphan without it. Three do not exist today: the common chapter's, its `virtualizing-ostd/` subtree's if it keeps one, and a new `asterinas-mode/virtualizing-ostd/index.md`, because today's `design/virtualizing-ostd/index.md` moves to the common chapter whole (it *is* the item classification). *Checked in a scratch copy*: deleting that file without replacing it makes the summary parser re-parent its six children under the preceding chapter and misnumber them silently, while `make build` still exits 0.

### 3.2 The editorial test

> **Does the sentence name a facility of a particular host?** If it does — a Linux `mm_struct`, an Asterinas `Thread`, a cgroup, a hook — it belongs to that host's chapter. If it does not, it belongs to **Design**.

> **Design states the contract and the kernelet's own side of it.** Where a host must provide something, Design says *what* and *with what obligations*; the host chapters say *how*. Design never says "the host does X somehow".

Two cases the test alone does not decide, settled here because the review showed it must be:

- **Invariants.** Design owns each invariant's claim and the obligation it puts on a host, plus one 8×2 **standing table** (invariant × host) whose cells link into the host chapters. Each host chapter owns the *body* of its own standing: I2's Asterinas body names the linear map, `guest_memory`, the owner array and the window; its Linux body names the fault handler's grant check. Nothing else is duplicated.
- **Comparative prose.** Sentences that compare the two hosts live in **Design only** — including today's I7 sentence, "the one in this list that a Linux host cannot hold by the same means". A host chapter never explains the other host.

### 3.3 What Design holds

| page | what it carries | from |
|---|---|---|
| index | the architecture figure (redrawn, host-independent), the four questions, **what a kernelet needs from any host** | `design/index.md` + `linux-mode/kernelets-in-brief.md` §*What a kernelet needs…* |
| Boundaries and trust | parties, the four interfaces, what a crossing is, the threat model, the three properties, invariants I1–I8 with the standing table of §3.2 | both `principles.md`, merged |
| Builds and images | two builds from one source, the image, position independence, shared text, the data template, the entry point and the two tables, the audit | both `builds-and-images.md`, merged |
| The kernelet API | the image ABI's *shape*: the two tables, what the shared pages are *for*, identity and naming, error codes, the depth, the prologue and epilogue as a contract, the service groups identical on both hosts | both `kernelet-api-service.md`; **substantially new prose** (§A.4) |
| The life cycle | create → running → dying → exited → destroying → destroyed; the configuration fields; the drain list as a principle | both `kernelet-api-control.md`; **substantially new prose** |
| Virtualizing OSTD | the three kinds, and the **item-by-item classification** (72 rows) | `design/virtualizing-ostd/index.md` |
| Memory | the grant: grains, runs, the owner array, metadata by section, allocation and exhaustion, what must be undone | both `memory.md`, common parts |
| Tasks and virtual CPUs | **after the back-port**: carriers, virtual CPUs, kernelet stacks, per-CPU data, starting a secondary virtual CPU | `linux-mode/…/tasks.md`, generalized |
| Scheduling | **after the back-port**: two levels, the four criteria, virtual interrupts and the upcall, the cooperation contract and its bound. **Not** the fairness argument (§4.4) | `linux-mode/…/scheduling.md`, generalized |
| Interrupts and time | virtual lines, the tick, the clock page, deadlines, the idle rule | both `interrupts-and-time.md`, merged |
| User mode | the contract: `execute` is a call that enters user mode and returns; what the tenant can never reach | both `user-mode.md`, common parts |
| Devices | virtio over function calls, a model's two halves, buffers checked against the grant | both `devices.md`, merged |
| Channels | vsock through a switch, addresses, credit, deny by default | both `channels.md`, merged |
| Zero-copy I/O | the lending model, the rings, what enforces lending, the argument | `design/zero-copy-io.md`, minus both hosts' plumbing (§A.4) |
| Faults, termination, and reclamation | the shape of a death, the depth rule, the drain list, what a death does not do | both `faults-and-reclamation.md`, common parts |
| Boot, power, panic, and the rest | the contracts for boot, power, the log, panic, and what a kernelet may still touch | both `the-rest.md`, merged |
| The kernelet runtime | the OCI verbs, the agent, the bundle, what an operator sees | both `kernelet-runtime.md`, merged |
| Alternatives considered | the host-independent alternatives: API virtualization against machine and OS virtualization, whole-mode alternatives | `linux-mode/alternatives.md`, its host-independent half |

Seventeen pages plus an index.

### 3.4 Scale, counted rather than guessed

The first draft of this plan said "35,000–40,000 words, most of it moved rather than written". The review checked the arithmetic and it did not close. Corrected:

- The Linux chapter's **wholly-Linux** pages alone are 29,283 words, before any Linux half of its thirteen split pages and before its new opening page. *Design for Linux* will be 35,000–40,000 words, not 20,000–25,000.
- Word counts in Appendix A are raw `wc -w`, inflated by inline SVG: `design/index.md` is 1,092 raw but about 300 of prose. Excluding figures and code fences the two chapters are 48,876 and 56,572.
- Genuinely **new** prose — not moved — is *estimated* at 8,000–12,000 words: Design's kernelet-API page (§A.4a), its life-cycle page (§A.4d), the merged invariants page and its standing table, three or four index pages, two *What this chapter assumes* pages, and the generalization of the Linux tasks and scheduling pages (10,267 prose words today) with every mirror, notifier and cgroup sentence stripped out.

### 3.5 The reading rule changes

`AGENTS.md` today says the Linux chapter "is a standalone chapter that … defines its own terms". That becomes:

> **Design** is the host-independent design; **Design for Asterinas** and **Design for Linux** are how each host meets it. A host chapter may link into Design for anything host-independent and must not restate it. Each host chapter opens with a short *What this chapter assumes* page listing the Design pages it builds on. Terms are defined once, in Design.

Two things follow that the first draft missed. The promise is not only in `AGENTS.md`: `linux-mode/index.md` opens "This chapter is the complete design, **written to be read alone**", and that sentence must be edited in the same commit as the rule, which `AGENTS.md` requires. And the mitigation "Design plus one host chapter is that document" is weak, because mdBook's `print.html` is the whole book and there is no two-chapter artifact to hand anyone. If handing the Linux chapter to an outside reader still matters more than the duplication, §7 states the fallback.

---

## 4. The back-port: what Design for Asterinas becomes

A specification sketch, not prose for the book.

### 4.1 The model

Today: a kernelet task is a host `Thread`, created through the `spawn_task` hook, with a 512 KiB host stack; the host's scheduler picks among them; the kernel proper's scheduler is inert.

After: **a carrier is a host kernel thread that carries one virtual CPU** of a kernelet, for the sandbox's life. A sandbox has *N* carriers, created once at start. A kernelet task is OSTD's own `Task` with its own kernelet stack, and the kernel proper's injected `Scheduler` decides which runs on each virtual CPU. This is D116 with the host's name for a task changed.

| the contract | how Asterinas meets it |
|---|---|
| a carrier per virtual CPU | a host kernel thread created by the endovisor at `start`. **Not pinned** to one host CPU unless the operator asks: the first draft pinned each carrier, which stops the host balancing them and makes a sandbox's share per-CPU; Linux deliberately does not pin |
| a way into the image | `_kernelet_entry` on virtual CPU 0, the entry table's `vcpu_entry` on the others |
| **three** redirect targets from the trap-return path | see §4.1.1 — the upcall stub, a yield stub and an exit stub. The first draft had only the upcall, which replaces one of the three jobs D16 does today |
| virtual interrupts | bits in a per-virtual-CPU record. (There are two record kinds today: a per-task one carrying the preemption count and flags, and a per-virtual-CPU one carrying `tick_pending` and `quiescent`. The first is deleted; the second is extended) |
| the tick | the host tick still adds to the virtual CPU's `tick_pending` and **no timer is armed per processor** — that much of D66 survives. What does *not* survive is "consumed at the kernelet's next tick point": once the kernelet's own scheduler ends time slices, a compute-bound task reaches no tick point, so the tick must be delivered as an upcall. The *idle* half of D66 (`JOB_TICK` at `idle_tick_hz` on a worker) and `JOB_GRANT` also die with the workers, and need somewhere to go — §4.6(3) |
| idling a virtual CPU | `vcpu_idle(deadline)`: the carrier parks; the host wakes it when a bit is set or the deadline passes. The deadline is not optional here: with the workers gone, an all-idle kernelet's timer wheel would otherwise stop. D122's next-expiry hook is a prerequisite, not an extension |
| kicking another virtual CPU | `vcpu_kick`: set the bit, wake the carrier, or send a reschedule interrupt so that its trap-return path delivers the upcall |
| kernelet stacks | `kstack_alloc` / `kstack_free` over the host's allocator, from a per-sandbox pool, charged to the kernelet — with the prerequisites of §4.1.2, which are heavier on Asterinas than on Linux |
| a tenant thread's floating-point state, and the kernelet's page table, across a host preemption | **D17 is kept, not retired** — §4.1.3 |

#### 4.1.1 The redirect: three targets, and how it is actually written

D16 does three things today: it delivers preemption to the kernelet, it gives the host CPU to another host task, and it terminates a task of a dying kernelet. **The upcall replaces only the first.** Asterinas therefore needs the same three stubs the Linux design has: `virq_entry` for virtual interrupts, a **yield stub** that reaches the host's own voluntary switch in task context at depth 0, and an **exit stub** for termination. Without the yield stub the host can never take a processor back from a carrier that computes, and D62's throttle loses its mechanism as well (it parks each *task* in its service epilogue today, and there are no per-task parks after the back-port).

The mechanism is not the two stores the first draft claimed. *Checked on the tree at `ab9a4cfdc`*: the kernel-mode path `_trap_from_kernel` saves every general register and returns with `iretq` (`ostd/src/arch/x86/trap/trap.S`), and `trap_handler(f: &mut TrapFrame)` (`ostd/src/arch/x86/trap/mod.rs:151`) can rewrite `f.rip` and `f.rsp`. But on a kernel-mode trap the processor pushes its five-word frame immediately below the interrupted stack pointer and switches no stack — *checked*: no interrupt-stack-table index is ever set in `ostd/src/arch/x86/trap/idt.rs`, and `trap/gdt.rs` installs a bare `TaskStateSegment::new()`. So the words just below the interrupted stack pointer are the `SS` and `RSP` that the `iretq` must still pop, and "pushing the saved instruction pointer there" corrupts the return. It has to be written *below* the hardware frame, with the frame's saved `rsp` pointed at it. The first estimate of that hole, 48 bytes below the interrupted stack pointer, is **wrong** and would corrupt the return in half of all cases; §4.7(7) gives the arithmetic that works and the four further conditions the stub is subject to.

And the Linux prototype does not discharge this. Its phase-4 report records the deviation: "It keeps the interrupted instruction pointer in the record rather than on the interrupted stack." So the variant this plan back-ports is unexercised on **both** hosts, and §4.3 says so.

#### 4.1.2 Kernelet stacks need their prerequisites, and they are heavier here

With no interrupt-stack table (above), every host trap — the tick, a page fault, a double fault — runs to completion on whatever stack it interrupted, which after the back-port is a kernelet stack. Linux moves hard interrupts to a per-processor interrupt stack and *still* reserves 16 KiB. The back-port therefore needs the Asterinas counterparts of the Linux chapter's D84/D112 (kernelet code on the task's stack, host code on the carrier's), the reserve, the function-entry stack check, and the `gs:`/TSS handling that user entry uses. None of that is in the Asterinas chapter today, and one sentence there is already false of the tree: `faults-and-reclamation.md` says the double-fault handler "runs on its interrupt stack", which the bare TSS contradicts.

#### 4.1.3 D17 is kept, and it is the counterpart of address-space adoption

A carrier is a host **kernel** thread, and *checked on the tree*: both of the host's schedule handlers return early for a task with no thread-local state — `pre_schedule_handler` saves the FPU and the FS/GS bases only through `as_thread_local()`, and `post_schedule_handler` activates a `vm_space` only if a `vmar` exists (`kernel/core/src/thread/mod.rs`). On Linux the carrier is a *user* task, so Linux itself saves the floating-point state and switches the address space. On Asterinas nobody would: after a host preemption of a carrier the kernelet would resume with whatever page-table root the intervening host thread left, and a tenant dereference would read another process's memory.

So **D17 survives, as a per-carrier save and restore**: the tenant's floating-point state, the FS and GS bases, and the kernelet's page-table root. That is what Linux gets for free from `kernel_switch_mm()` and its own FPU handling. §4.7(3) narrows this twice: the page-table root is handled by the host kernel's *existing* injected handler once a carrier is a kernel thread that owns an address space, which upstream added after the pinned commit, so D17 reduces to the floating-point and segment-base half; and the hook cannot be a third injected handler, because both slots are taken.

### 4.2 What is simpler on Asterinas, and what is not

The first draft claimed three simplifications. One survives, one is partial, and one was wrong.

1. **Survives: no watch timer and no preemption notifiers.** The host's own tick already fires on every processor and already knows which kernelet task it interrupted, which is where `preempt_off_ticks` is enforced today. Linux needs a pinned high-resolution timer per processor and a notifier per carrier to get the same thing.
2. **Partial: no mirrored preemption count — but the record still needs two fields.** The host does read the kernelet's guard count directly, so nothing has to be mirrored into a host counter. But a single count is not enough, and the Linux prototype found out why: "`masked` became two fields, a guard count and a virtual interrupt flag, because a single one would have meant a kernelet holding a spin lock received no ticks." Asterinas starts from a worse place, because its `interrupts-and-time.md` *aliases* `irq::disable_local` and `DisabledLocalIrqGuard` to the preemption count, justified explicitly by "a kernelet has no handlers: its handlers are jobs on the worker task". **Upcalls give it handlers**, so that decision must be re-taken along with three consequences the reviewer traced: `iretq` restores the interrupt flag from the interrupted frame, so without an `irq_off` field the host can redirect again inside the stub (nested upcalls, and `InterruptLevel` would report L2); `CpuLocalCell`'s read-modify-write on a replica raises the count to exclude a *migration*, and would no longer exclude a same-virtual-CPU handler touching the same cell, where the tree relies on one `gs:`-relative instruction; and `halt_cpu` takes `disable_local()` before halting, so `vcpu_idle` would arrive as a sleeping service call under a nonzero count, which the service epilogue refuses. The fix is to back-port the Linux record's two fields *and* the four `arch::irq` primitives, and to rewrite the invariant sentence "no kernelet code ever runs in interrupt context".
3. **Wrong: "no address-space adoption".** §4.1.3. There is a counterpart, it is D17, and it must be kept.

So the honest summary is that the back-port is **comparable in size to the Linux one**, not smaller. What Asterinas genuinely avoids is the timer and notifier machinery; what it gains instead is a schedule hook and the stack prerequisites of §4.1.2. The sentence "What Asterinas must add that Linux did not: nothing" is withdrawn.

### 4.3 What the back-port deletes

| deleted | register | why it goes |
|---|---|---|
| `task_spawn`, `task_exit`, `task_destroy`, `task_yield`, `task_park`, `task_unpark`, `task_set_nice`, `task_set_vcpus` — 8 of 21 services | D61, D31 | kernelet tasks are no longer host tasks |
| `job_wait` and the per-virtual-CPU **worker threads** | D9, D11 | virtual interrupts arrive by upcall on the interrupted task |
| the `spawn_task` hook and `KerneletTaskBody` | D61 | nothing spawns host threads per task |
| `RUNNING`, `BODIES`, the `TaskName` space | D7 | there are no names to exchange |
| the per-task `TaskRecord` | D8 | the record becomes per virtual CPU |
| `preempt_switch` as a *task switch* from the trap-return path | D16 | the host redirects to one of three stubs; the kernelet switches its own tasks. **D16 is rewritten, not deleted**, and **A8 stays** for the yield stub, which still reaches the host's own switch |
| the no-idle-threads `cfg` lines | D67 | a kernelet has idle tasks again, as on a machine |
| `nice`/affinity builders on `TaskOptions` and the kernel proper's `cfg` lines for them | D31 | the kernel proper's own scheduler honors them now |

**What happens to the unverified assumptions.** Two drafts of this plan got this wrong in two different ways. The first said A8 and A9 are "retired, not carried"; the second said they are replaced by weaker assumptions. Corrected against the tree:

- **A8 stays.** The upcall replaces one of D16's three jobs (§4.1.1). The yield stub still hands the host CPU to another host task from the trap-return path, which is what A8 is about — and nothing else in OSTD preempts a host kernel thread that computes (*checked*: `might_preempt()` is reached only from `halt_cpu`, the user-mode return and after an enqueue). What *is* new and weaker is the redirect itself, and it needs an assumption of its own on each host.
- **The redirect is unexercised on both hosts.** Linux's A32 measured a redirect from a *timer callback*, and the Linux prototype kept the interrupted instruction pointer *in the record* rather than on the stack. The stack-push variant this plan back-ports has never run anywhere.
- **A9 changes hands rather than retiring.** *Checked*: `finish_grace_period` is called only from `switch_to_task`, and a period completes only when the mask is full (`ostd/src/sync/rcu/monitor.rs`). A virtual CPU asleep in `vcpu_idle` passes no switch point, so a period begun while it sleeps never completes and deferred frees never run. "The kernelet notes its own quiescence" is not enough: the state must persist across the sleep and the monitor's restart must count a sleeping virtual CPU as quiescent — the same extended quiescent state as D32, with the kernelet as its writer instead of the host.
- Appendix B adds three further Asterinas assumptions.

So: the *risk* drops on one path and the *count of unverified claims goes up*, not down. §5 says what would discharge them.

### 4.4 What it buys

- **A tenant's thread count stops buying machine share.** The currency changes from tenant-chosen (how many threads it runs) to operator-chosen (how many virtual CPUs it was given). That is the gain, and it is real. It is *not* proportional share: the Linux argument's second step is "**the control group** bounds the N tasks", and Asterinas has no group scheduler — *checked on the tree*: `kernel/core/src/sched/sched_class/fair.rs` carries a per-*thread* weight of 1024·1.25<sup>−nice</sup> and there is no group entity anywhere under `kernel/core/src/sched/`. A sandbox's share is the sum over its host threads, so two sandboxes at the same `nice` with *N* = 2 and *M* = 8 get one share against four.

  **Decided (§8.3): a group scheduler in the host is a named Asterinas prerequisite**, recorded beside the OSTD ones (D118, D122), and until it exists the Asterinas chapter states plainly that **proportional share is not held on this host**. The alternatives — a per-carrier weight of about *W*/*N* off the forty-step geometric ladder, or leaning on D62's quota as the equalizer — are recorded as what a first version could do, not as the design. **The fairness argument therefore stays in each host chapter, not in Design**, because it has no Asterinas leg to stand on until the prerequisite is met.
- **The tenant's kernel schedules the tenant's threads**: real-time policies, `nice` within the sandbox, `/proc/loadavg` and `sched_getscheduler` become true instead of inert.
- **A sandbox stops costing the host two task objects and a 512 KiB stack per tenant thread.** A thousand tenant threads cost *N* carriers and a thousand kernelet stacks from the sandbox's own accounted pool.
- **A task switch stops crossing**: ~1,000 cycles, *measured on the booted prototype of the Linux design*, **[unverified]** on Asterinas.
- **Chapter 12 gets smaller**: eight services, one hook, two shared-page structures, three mechanisms and two assumptions leave it.

### 4.5 What it costs

- The OSTD prerequisites the Linux design names (D118, D122) become prerequisites on **both** hosts — which is where they belonged, since they are additions to OSTD, not to a host.
- **The host loses its view inside a sandbox** (§2.2): no load balancing within the sandbox's CPU set, no per-thread charging, no host-side visibility of what a tenant runs. `times(2)` becomes the kernelet's own business, as on a machine.
- **The kernel proper's scheduler becomes load-bearing** without evidence that it is good (§2.2, A33).
- **Neighbor latency.** The Linux chapter calls the cooperation contract "the one thing this design costs a host that a container does not": about 2 ms on every processor a sandbox may touch, and "a real-time Linux should not host kernelets at all". The Asterinas equivalent must be stated the same way — and note that Asterinas's present bound **kills** the kernelet where Linux's forces a yield (§4.6(4)).
- **A prerequisite the host does not meet**: proportional share needs a group scheduler Asterinas has not got (§4.4, §8.3), so the back-port leaves that property unheld on this host and says so.
- **Three mechanisms Asterinas must gain that it does not have today**: the two-stack rule with its reserve and function-entry check (§4.1.2), a per-carrier schedule hook for tenant state and the page-table root (§4.1.3), and the second record field with the four `arch::irq` primitives behind it (§4.2). The first draft of this plan said the back-port adds nothing; it adds these.
- **Dead code left behind, to be swept in the same pass**: D56's hand-off of a dead kernelet task's reference to the reaper in `after_switching_to` has nothing to hand off, and *Exited*'s definition ("no stack of the kernelet is in use anywhere") must be restated as "no carrier is in kernelet text".
- Every back-ported claim rests on evidence from the other host until §5 is done.

### 4.6 What the prototype must settle

Four questions this plan does not answer. **Decided (§8.5): the prototype settles them**, because it has to make each choice to boot at all, and reports each as a finding the way the Linux prototype's phase-4 deviations were reported. They are listed here so that the prototype's remit is explicit and none is decided by accident.

1. **Does Asterinas need the FPU *services*?** The per-carrier save of §4.1.3 is the host's, and it is required. Whether the *kernelet* also needs `fpu_save`/`fpu_load` to move state between its own tenant threads depends on where the kernel proper's own save sits — and the Linux prototype's unexplained deviation about exactly that must be understood before either host's text is settled.
2. **What enforces the quota once `task_park`/`task_unpark` are deleted?** D62's throttle parks each *task* at its next quiescent point, and there are no per-task parks after the back-port. Candidates: park the carriers, or refuse to schedule them. This one is load-bearing twice over, because D62 is also the interim answer to share (§4.4) until the group scheduler of §8.3 exists.
3. **Where do the idle tick and the grant notice go?** Both are worker jobs (`JOB_TICK` at `idle_tick_hz`, `JOB_GRANT`), and the workers are deleted. The expected answers are `vcpu_idle`'s deadline (D122, a prerequisite rather than an extension) and a bit in the virtual CPU's record; the prototype confirms or refutes them.
4. **Does the bound kill or yield?** Asterinas's today kills (`preempt_off_ticks` → `PreemptOffTooLong`); Linux's forces a yield and counts it. Whichever the prototype implements, the bound must be counted by the host and not read from the kernelet's record.

### 4.7 What the soundness pass established

A source-reading pass over Asterinas at `ab9a4cfdc` checked the eight load-bearing claims of §4.1 to §4.3 against the tree and answered §4.6's four questions as far as the source decides them. Everything below is *checked on the tree* — read from source, not measured, and not run. It is the specification pass 2 writes from, and where it contradicts §4.1 to §4.6 above, it wins.

**One claim holds, five break, and two are confirmed.**

1. **The hook stack and `catch_unwind`: holds, under two conditions.** The unwind never crosses the stack switch, because `kernelet-api-service.md` nests as `on_hook_stack(|| catch_unwind(|| f(…)))`, so the landing pad is itself on the hook stack and phase-1 unwinding finds it before reaching the asm shim; the shim needs no unwind information. Exclusivity holds too, but *only because the redirect is gated on depth 0*: while a hook runs the depth is at least 1, no upcall is delivered, and no second hook can reach the same per-processor stack. The two conditions: the panic entry must actually unwind, which on the tree it does not (`PANIC_ON_OOPS` is initialized `true` and a hook panic powers the machine off), so the `__handler_entry` change in `the-rest.md` is a **prerequisite, not a description**; and A3's 64 KiB reserve now has to be re-estimated against *upcall* depth, because the stack it is measured on may already carry a hardware trap frame plus the whole stub and its handlers.
2. **The reaper's hand-off is dead code, but reaping is still needed.** `after_switching_to` never sees a kernelet task, because the kernelet's own replica of `switch_to_task` does the switching, so D56 has nothing to hand off — confirming §4.5's sweep item, and the consequence is benign: the replica frees its own stacks by the same previous-task mechanism, on its own time. What still needs a reaper, because it cannot be done in the detecting context: `on_dying`/`on_exited`, clearing armed deadlines, waking carriers parked in `vcpu_idle`, and running the exit stub on each carrier. *Exited* must be redefined as "no carrier is in kernelet text".
3. **Nobody restores the page-table root, and the hook cannot be injected — half of which upstream has since fixed.** At the pinned commit the finding holds exactly as §4.1.3 needs it: a carrier is a *kernel* thread, kernel threads carry `data` but never `local_data`, so `as_thread_local()` is `None` and neither the floating-point save nor `vm_space().activate()` runs; an intervening user thread activates its own space, and on return to the carrier the root is that process's, so a tenant dereference reads another process's memory. `VmSpace`'s own documentation states the contract the injected post-schedule handler is meant to discharge, and both handler slots are `Once` and already claimed by the kernel proper at boot, so the endovisor cannot inject a third.

   **But the tree has moved 180 commits past the pin, and one of them is exactly this.** Upstream `d76b4dcc0`, *Allow kernel threads to use a process VMAR*, gives `KernelThread` a `vmar`, and `post_schedule_handler` now carries an `else if` branch that activates a kernel thread's `vm_space` after every switch (*checked on the tree at `e01871f0c`*, and *checked* absent at `ab9a4cfdc`, so the difference is real and not a misreading). **A carrier can therefore be a kernel thread that owns an address space, and the existing injected handler restores its page-table root with no kernelet branch and no second OSTD slot.** That is the mechanism the back-port needed, already upstream.

   What still stands: `pre_schedule_handler` early-returns unchanged at both commits, so **nothing saves the tenant's floating-point state or the FS and GS bases across a host preemption of a carrier**. D17 survives for that half and only that half, which is smaller than §4.1.3 claimed. The placement finding also stands — pre-schedule runs with interrupts off, post-schedule before they are re-enabled.
4. **`InterruptLevel` is not carried across a redirect, and D18's aliasing is unsound.** The level is incremented and decremented around the host's own handler, is `pub(super)`, and is the *host's* cell; by the time the `iretq` runs it is back to its pre-trap value, and the kernelet's replica reads 0. So `InterruptLevel::current()` reports task level inside the stub and the first delivered virtual interrupt hits `unreachable!("this function must have been call in interrupt context")` in the bottom half. Four consequences: **the stub must enter the level itself**; the *privilege* cannot be derived there, because the redirect only ever rewrites kernel-mode frames, so a tick that interrupted the tenant in user mode must have its privilege **sampled by the host into the record**; the L1 bottom half calls `disable_preempt()` and then `arch::irq::enable_local()`, two distinct facilities, so **D18's aliasing of `disable_local` to the preemption count is unsound once upcalls exist** — independently of the two-field record — and must be replaced by the four real primitives over the record's `irq_off`; and because `iretq` restores the interrupt flag from the frame, the host must clear it in `f.rflags` **and** set the record's mask before returning, since the mask alone leaves an interruptible window in which a second redirect would overwrite the first return address.
5. **The double-fault claim is false, and the real path is worse than sharing a stack.** No interrupt-stack-table index is set anywhere and the task-state segment is bare, so every host trap runs on the interrupted stack. `faults-and-reclamation.md`'s "on its own stack" (A6) and "runs on its interrupt stack" are false of the tree. The failure is not a corrupted neighbor frame but a **triple fault and machine reset**: a kernel stack overflow hits the guard page, the fault handler panics *further down* the exhausted stack into the guard, the double fault's own frame push faults again. Required before either the reserve or the function-entry check can be claimed to catch anything: an interrupt-stack-table index for vector 8 and a double-fault stack in the segment. The function-entry check is then the **primary** mechanism and A6 the backstop, not the reverse.
6. **The RCU monitor is not usable unchanged, and the change is an OSTD fix.** A grace period completes only when every processor of the machine's cardinality has switched, and `restart` seeds an *empty* mask, so a period begun while a virtual CPU sleeps in `vcpu_idle` is stuck from birth and deferred frees accumulate for as long as a sandbox is partly idle. The smallest change is a per-virtual-CPU quiescent set: entering `vcpu_idle` finishes the period for itself and sets its bit, `restart` seeds from that set, leaving clears it. That is A9 in substance with the kernelet as writer, as §4.3 says — and the same hole exists on the tree for a physically idle processor, so it is arguably an OSTD fix rather than a kernelet-only one.
7. **The redirect, written out.** The five hardware-pushed words at the end of `TrapFrame` are exactly what the `iretq` consumes, and kernel builds carry `-C no-redzone=y`, so the words below the interrupted stack pointer are free — **which makes the same flag a stated build requirement for the kernelet image, and an audit item**. The sequence: gate on kernel-mode frame, `rip` in kernelet text, record depth 0, guard count 0, and `rip` outside the stub's own range; take `hw_low = (f.rsp & !0xF) - 40`, the lowest live byte of the hardware frame; place the hand-off at `(hw_low - 16) & !0xF`; store the interrupted `rip` there and the sampled privilege beside it or in the record; point `f.rsp` at it and `f.rip` at `virq_entry`; leave `cs` and `ss` alone; clear the interrupt flag and set the record's mask. **Why 48 bytes was wrong**: when the interrupted stack pointer is 8 modulo 16 the hardware frame's lowest byte *is* `f.rsp - 48`, so a write there clobbers the saved `rip` the `iretq` pops. The offset must be computed from `f.rsp & !0xF`; the live region below `f.rsp` is 40 bytes plus up to 15 of padding. Three further conditions: the stub must preserve **all fifteen** general registers, because the interruption point is arbitrary and not a call boundary; `virq_entry` must be **naked assembly**, since it is entered at a 16-aligned stack pointer and a compiled function assumes the opposite alignment; and the hand-off plus the stub's own frames come out of the interrupted kernelet task's stack, a direct charge against the reserve of (1).
8. **`might_preempt` and the guard drop: confirmed, and it is two files each.** `might_preempt()` has exactly three callers and none of them preempts a host kernel thread that computes, so **A8 stays**, as §4.3 says. Neither guard's `Drop` calls it today. Two prerequisites, each in two files: the preempt guard's and the interrupt guard's `Drop` must call `might_preempt()` when the count reaches zero, **gated** on task context and interrupts being enabled, because the switch runs `might_sleep()`, which panics on a nonzero guard count or interrupts off, and the L1 bottom half drops a preempt guard with interrupts deliberately off; and because a context switch forgets its guard while the next task re-enables with the bare primitive, the guard `Drop` alone cannot see every section end — **the four `arch::irq` primitives must carry the check too**. The second prerequisite is a preemption check on interrupt return to kernel code, after the callbacks return, when the interrupted privilege was kernel and the frame's interrupt flag was set, with interrupts re-enabled first; and in the other two architectures' trap paths for parity.

**§4.6's four questions, as far as the source decides them.** Each still has to survive the prototype, which is what decision §8.5 asks; the source narrows three of them to one shape.

1. **The FPU services are not needed.** The kernel proper's tenant floating-point and FS/GS save is lazy and driven by its *own* hooks, from the pre-schedule handler and from the pre-user-run hook, using plain instructions. Inside a kernelet those hooks fire at the kernelet's own switch points, so moving state between two tenant threads needs no `fpu_save`/`fpu_load` service at all. The host owes only the per-carrier save of §4.1.3, the control-register and extended-state setup, and the extended-state size from CPUID — and **the kernelet must never execute `xsetbv`**. This also explains the Linux prototype's unexplained deviation, which was the same fact seen from the other host.
2. **The quota parks the carrier.** Nothing in OSTD throttles a computing host kernel thread from outside, and the run queue has no throttled state; but `park_current`/`unpark_target` act on a host task and a carrier *is* one. So **D62 survives with the carrier as its object** instead of the kernelet task, enforced at the host tick plus the yield stub. The alternative, refusing to enqueue, would have to live in the class scheduler under `kernel/core/src/sched/`, not in OSTD — which is also where the group scheduler of §8.3 would go.
3. **The idle tick and the grant notice have only one shape available.** `halt_cpu()` is the only idle primitive and takes no deadline, so **D122 is a genuine OSTD addition**, not an extension; and the existing per-processor timer registration is per *host* processor, the very thing D66 declines to arm, so nothing on the tree can carry either. A deadline on `vcpu_idle` for the tick, and a record bit plus `vcpu_kick` for the grant, is what the source leaves.
4. **The bound yields.** The L1 bottom half disables preemption for its whole duration and the kernel proper's work there is not bounded by construction, so a killing bound would kill *correct* kernelets. The yield stub has to exist anyway (§4.1.1) and is reachable exactly at that point, so yielding costs no new mechanism. Counted on the host side, as §4.6(4) requires.

**A provenance problem this pass turned up, and an owner decision it needs.** `AGENTS.md` pins every *measured on the tree* claim to Asterinas at `ab9a4cfdc`. The working tree is at `e01871f0c`, **180 commits later**, and the eight findings above were re-checked against *both*: seven are unchanged, and one half of one is overtaken by upstream (3). Two commits in that range touch this design's ground — `d76b4dcc0`, which supplies the carrier's address space, and `6b048be7c`, which wraps the x86 task-state segment in a cell without giving it a double-fault stack, so finding (5) stands. Nothing else in the range bears on the back-port (*checked*: the range's subjects filtered for trap, interrupt, scheduling, stack, preemption, VMAR, RCU and floating-point work).

So the pin is stale but not wrong, and the design is *better off* at the newer commit than at the pinned one. **The decision the owner owns**: re-pin the book to a current commit, which means re-verifying every "measured on the tree" claim in both host chapters, or keep `ab9a4cfdc` and note the one divergence where it bears on the design. This plan does the second, because the first is a pass of its own and the divergence is favorable; pass 2 states the carrier's address space as *checked at `e01871f0c`* and says so, rather than quietly citing the pin for something the pin does not contain.

**What this does to the plan's arithmetic.** The back-port's cost list in §4.5 gains three items: an interrupt-stack-table entry and a double-fault stack (5); the level-entering, privilege-sampling, register-preserving naked stub of (7) with `-C no-redzone` as an image build requirement; and the `might_preempt` call in two `Drop` implementations, the four interrupt primitives and three architectures' trap paths (8). It loses one: the FPU services, which are not needed on either host. D18 moves from "must be re-taken" to **"is unsound and is replaced"**, and A6 moves from an assumption to a **prerequisite that the tree does not meet**.

### 4.8 What the prototype settled

**Decision §8.4's gate is met.** Experiments 1–3 ran in a worktree off `ab9a4cfdc` at `~/Workspace/kernelet-in-asterinas-poc/kernelet-asterinas/`, nothing committed; `make all` exits 0 with `hello: PASS`, `sched: PASS`, and `virq: PASS: 3895 upcalls across 4 chunks, every chunk bit-identical to the undisturbed checksum 0xf705414f5604b292`. The full write-up is `REPORT.md` in that worktree, with the host patch saved as a patch file. *Measured on the booted Asterinas prototype* is now a provenance this chapter may use — **except about share**, for the reason at the end.

The three results: the tree's own unmodified 100-line kernel runs as a kernelet on the real path, with a tenant page table built from grant frames and the host grafting its upper half in; two carriers run nine kernelet tasks under a strict-priority scheduler injected in `#![deny(unsafe_code)]` Rust, **178 context switches performed by the kernelet's own scheduler**, strict priority holding on all 54 progress records; and the redirect delivers 3,895 upcalls with every checksum chunk bit-identical to the undisturbed run. **The host patch is 50 added lines in 3 files**, against 169 in 8 for Linux's gate — which is the quantitative form of §4.2's claim, now measured rather than asserted.

**Where the prototype overrides this plan.** Each of these wins over §4.1 to §4.7 above.

- **The two-stack rule is not needed** (finding F1). Kernelet code and host service code share one stack and it works, because `syscall_return` stores `rsp` into `gs:4` itself. §4.1.2's demand for the Asterinas counterparts of D84 and D112 is **withdrawn**; what survives of that section is the reserve and the function-entry check, which are about *how much* stack, not *whose*.
- **The redirect's gate needs a condition no one named** (F2). Depth 0 is insufficient: a nested interrupt's bottom half runs on the kernelet stack at depth 0. `InterruptLevel::current().is_task_context()` is required, and belongs in the back-ported D16 beside the other four conditions of §4.7(7).
- **The hand-off arithmetic is `align_down(rsp, 16) − 56`, and it is 8-aligned, not 16** — correcting §4.7(7), which said to align the slot down to 16, as well as the original 48. Eight is not an accident: it is what the calling convention wants at a function's entry, so `virq_entry` need not be naked assembly after all. The prototype reaches the slot by taking `&f.trap_num` rather than computing an offset, which is what makes it right at both alignments.
- **The kernelet's spin locks must take an interrupt guard, not a preemption guard** (F7), found by deadlock: the scheduler held its run-queue lock, the tick arrived as an upcall *on that very task*, the handler re-entered the run queue, and the virtual CPU hung against itself. This is the third consequence §4.2(2) predicted, now observed, and it is the strongest argument that D18's aliasing must go.
- **There is no mirror to build, and five services go** (F3): `pt_unregister`, `vcpu_yield`, `fpu_save`, `fpu_load` and the seat semaphore, five of Linux's eighteen. §4.2(2) was right that the record needs two fields and wrong that a *mirror* is needed. The price is the other half of A8: with no mirror there is no way to reclaim a processor from a carrier that will not yield.
- **A6 is confirmed false of the tree** (F5), independently of §4.7(5): bare task-state segment, no stack index. An interrupt-stack-table entry for vector 8 is a prerequisite.
- **`TaskOptions::spawn` ends in `might_preempt()`** (F6), so a bootstrap context is abandoned mid-setup on both sides. It cost the prototype two boots and belongs in the text as a trap for the implementer.

**The four open mechanisms of §4.6, now settled.** All four land where §4.7 predicted from source, and two are settled *in code* rather than by argument: the FPU services are **not needed** (the kernelet does its own `fxsave64`/`fxrstor64`), and the idle tick and grant notice are **`vcpu_idle`'s deadline plus a pending bit**, with the carrier halting rather than parking, which costs millisecond granularity. The other two are chosen with reasons but unimplemented, and the chapter must say so: the quota **parks the carriers**, and the 2 ms bound **yields and is counted by the host** — the latter unexercisable here until the yield stub exists.

**One gap the prototype leaves open, and one deviation that bounds every number.** The host's per-carrier save is implemented and ran, but **does not save the FS and GS bases** — the half of D17 that §4.7(3) said was all that remained is therefore itself only half done. And deviation D2: the prototype's host is a minimal OSTD kernel, not `kernel/core`, so **nothing in it is evidence about share or fairness**. Experiment 4's pair, which is what §8.3's prerequisite rests on, is still owed. Eleven deviations are numbered in `REPORT.md`; D1, that the image is linked in rather than loaded, is the other large one.

## 5. Evidence: what an Asterinas prototype must show

The back-port's claims would otherwise rest on a prototype of the *other* host. The Linux prototype is good evidence that the mechanism works at all, and no evidence about Asterinas's trap path, its scheduler or its accounting.

A prototype in the Asterinas tree (`~/Workspace/asterinas`), in the spirit of the Linux one: a minimal endovisor, a minimal vOSTD, and the tree's own 100-line example kernel unchanged.

**The gate, and it is now the first work of the project.** *Decided (§8.4)*: items 1–3 run **before pass 2 writes a word** of the back-port. They are also §7's soundness checks, so one run discharges the design risk and the evidence gap together. The prototype additionally settles the four mechanism questions of §4.6 and reports each as a finding.

What this buys, and what it costs: the back-ported pages can then state their mechanisms as design rather than as a proposal, and no chapter carries a large **[unverified]** surface for months — at the price of beginning the project with code rather than prose.

1. **Hello World on the real path.** The 100-line kernel, source byte-identical, as a kernelet on an Asterinas host: one carrier, the entry table, the service table, a tenant address space, `user_run`, two system calls.
2. **A virtual CPU that is a carrier.** *N* = 2 carriers; the kernel proper's injected scheduler running its own tasks on them; a trace checked against a reference model, as the Linux phase-4 test does.
3. **The upcall from the trap-return path.** The host rewrites `f.rip`/`f.rsp` in `trap_handler` so that an interrupted kernelet task at depth 0 enters `virq_entry` and resumes intact. Assertion: a checksum computed across thousands of upcalls is bit-identical to one computed undisturbed.

Then:

4. **Share by carrier count — and the evidence for the prerequisite.** Two experiments, reported together. (a) Two sandboxes on the same host CPUs with equal configuration, one running 1 busy kernelet task and the other 50: neither share may move with its task count. Today's design fails this by construction, and it is the experiment that justifies the back-port. (b) The same two sandboxes with *N* = 2 against *M* = 8 carriers: the share is expected to come out near 1:4, and that result is the **evidence for the group-scheduler prerequisite of §8.3** — it is what "proportional share is not held on this host" looks like when measured. Note that the Linux prototype measured a sandbox against a *sibling control group* and never ran two sandboxes, so both halves are new work.
5. **Cooperation and its bound.** A kernelet holding a spin lock across the host's preemption point: zero involuntary switches inside a critical section, and a bounded stay for a deliberate overstayer — with the kill-or-yield decision of §4.5 exercised.
6. **What was deleted is really gone.** A sandbox with 50 tenant threads must cost *N* carriers plus its device threads, not 50-something, and no 512 KiB host stack per tenant thread.
7. **Terminating a carrier that spins in kernelet text.** The kill half of D16 disappears with the task-switch half; the exit stub of §4.1.1 replaces it, and nothing has shown that it works on Asterinas.
8. **A host trap on a kernelet stack.** With no interrupt-stack table (§4.1.2), the tick, a page fault and a double fault all land on whatever kernelet stack was current. Show that the reserve holds and that a deliberate overflow is caught rather than resetting the machine.
9. **Tenant state across a host preemption.** The counterpart of the Linux prototype's floating-point experiment, which needed 518,501 check-ins to find its bug: a tenant thread's vector registers, FS/GS bases and page-table root must survive the host preempting its carrier and running something else (§4.1.3).
10. **A grace period with an idle virtual CPU.** Begin one while a virtual CPU sleeps in `vcpu_idle`, and show it completes and the deferred frees run (§4.3).

Two practical notes. The share experiments (4) and the new N-vs-M one belong together: measure two sandboxes with equal *N*, then with *N* = 2 against *M* = 8, and report both, because the second is the one that shows what carrier count buys. And unlike the Linux prototype, which loaded a module, an Asterinas prototype must build one crate twice under the `kernelet` feature and combine both into one boot image — a build problem the Linux side never had.

Not in scope for a first Asterinas prototype: devices, channels, zero-copy I/O, the runtime, more than one kind, the window's full layout.

---

## 6. The passes, in order

Revised after review: the original pass order would have failed `make check` at three of its six commits. The rule that fixes it is **no commit may leave a link pointing at material that has moved**, which means page moves and their repoints — register included — happen together.

| # | pass | what it does | why here |
|---|---|---|---|
| **1** ✓ | **The Asterinas prototype, experiments 1–3** — *done; see §4.7 and §4.8* | A minimal endovisor, a minimal vOSTD and the tree's own 100-line kernel, in a worktree of `~/Workspace/asterinas`: Hello World on the real path, two carriers running the kernel proper's own scheduler, and the trap-return redirect with a bit-identical checksum. Settle §4.6's four questions in code and report each as a finding. Run §7's soundness list against the tree first; if one fails, revise the plan rather than force it. | *Decided (§8.4)*: this gates the prose. It is also the only way §4.1.1's redirect gets exercised at all, since neither host has run it. |
| **2** | **The back-port, as one branch** (twelve files, not eight: **Appendix C** counts them) | Rewrite `virtualizing-ostd/tasks.md` (splitting it into *Tasks and virtual CPUs* + *Scheduling*), `interrupts-and-time.md`, the processor group of `kernelet-api-service.md`, the `spawn_task` hook and task-name space in `kernelet-api-control.md`, the entry rule and I6/I7 in `principles.md`, `faults-and-reclamation.md`, `the-rest.md`, and `index.md` including its figure. Fold in pass 1's findings and the group-scheduler prerequisite of §8.3. Revise D7, D8, D9, D11, D15, D16, D17, D31, D32, D61, D62, D66, D67; keep A8 and A9 in their new forms; add the Asterinas side of D116–D122 and the four new assumptions. Sweep the dead code of §4.5. | These pages quote each other's model; split across two commits the chapter states two incompatible things in between, and `make check` tests links, not sense. |
| **3** | **The Overview and the terminology** | The three Overview sentences of §0 and the reconciliation of `overview/terminology.md` (it still defines "endovisor ABI" as the image's table, and has no *carrier* or *virtual CPU*). Nothing under `src/paper/`. | Small; may be folded into pass 2's branch. The Paper stays a recorded debt (§0). |
| **4** | **Create Design, page by page** | For each page in §3.3: create it, move the text, repoint **every** inbound link in the same commit (the register's 167 included), update `SUMMARY.md`, run `make renumber`, check. Rename `design/` → `asterinas-mode/` first, as one mechanical commit. Add the three missing `index.md` files. | Moving one page at a time keeps every commit green, which the all-at-once order could not. |
| **5** | **Trim the host chapters** | Delete from both host chapters what Design now holds; add each chapter's *What this chapter assumes* page; edit `linux-mode/index.md`'s "written to be read alone" and `AGENTS.md`'s reading rule **in this commit**, as `AGENTS.md` requires of a reversed decision. | *Decided (§8.6)*: extraction as planned, so the promise is retired deliberately rather than by drift. |
| **6** | **Figures** | Redraw the architecture figure (it says "a kernelet's threads are host threads", "entry table: start a thread", "service table: 21 C-ABI calls" — all three die in pass 2), the Executive Summary's two, and the seven Linux figures that sit on split pages. `make render` each and look at it. | Figures cannot be moved mechanically and no other pass owns them. |
| **7** | **Register and conventions** | Finish the scope column (Appendix B); `AGENTS.md`'s structure and vocabulary sections; the index child lists, which are hand-written and unchecked. | What is left after pass 4 did the link work. |
| **8** | **The rest of the prototype** | Experiments 4–10 of §5, including the share pair that is the evidence for §8.3's prerequisite; fold the numbers and deviations back into Design and Design for Asterinas, as the Linux prototype's were. | Not a gate, so it follows the prose rather than blocking it. |

**Execution status.** Passes 1, 2 and 3 are **done**. Pass 1: the soundness list (§4.7) and the prototype's experiments 1–3 (§4.8), which met decision §8.4's gate. Pass 2: all twelve files of Appendix C back-ported, the chapter's figure redrawn, and the register's retired and revised rows marked — D8, D9, D11, D15, D16, D17, D18, D19, D31, D32, D56, D57, D61, D64, D66, D67, A3, A6, A8, A9 — with D123 and D124 added. Pass 3: the Overview's three false sentences. Pass 4 has the common chapter's first two pages; passes 5–8 remain. Pass 3's terminology half is done, and pass 4 has produced the common chapter with its first two pages, [Channels](src/blueprint/design/channels.md) and [The kernelet runtime](src/blueprint/design/kernelet-runtime.md) — both chosen because the back-port does not touch them, so they could be extracted while the prototype gate was still open. The directory rename of pass 4 is done. Pass 2 waits on the prototype, as decision §8.4 requires.

**Exit criteria, every pass**: `make renumber` when `SUMMARY.md` or a heading changed; `make check` prints `check: OK`; `make build` exits 0; every changed figure rendered and looked at; external links verified by hand against the local v6.12 tree or the Asterinas tree at `ab9a4cfdc` (no target does this — `make check` skips `http(s)` entirely); no stale vocabulary; the commit message says what changed, what was verified with the actual output, and what the next pass should attack.

**A checklist the tooling cannot replace.** `make check` validates link targets, anchor ids, orphans and bare `§`s — but a link that still resolves to a file whose *material* has left is invisible to it, and 514 of the 818 internal links into these two chapters are unanchored. Each page move in pass 3 therefore carries a before/after diff of that page's `##` headings, and every link into the page is checked against it by hand. Two further tool limits worth knowing: `make renumber` rewrites only the *text* of `[§…](href)` links and never an href, and it numbers `##` headings only — so no `§` link may point at a `###` heading (today none does; keep it that way by giving every linked heading `##` level or an explicit `{#id}`).

---

## 7. How this plan could be wrong

- ~~**The back-port could be unsound on Asterinas for a reason not visible from Chapter 12.**~~ **Answered: all six candidates were checked against the tree, and five of the six broke at the pinned commit — with half of one since fixed upstream.** The hook stack holds under two conditions; the reaper's hand-off is dead code; the page-table root is restored by nobody *and* the hook cannot be injected; `InterruptLevel` is not carried and D18's aliasing is unsound; the double-fault path triple-faults; and the RCU monitor needs an extended quiescent set. §4.7 gives each, with what it adds to the back-port. None of them makes the back-port unsound — each names a prerequisite — but the plan's claim that Asterinas needs to add nothing is now wrong four times over.
- **Proportional share may not be reachable on Asterinas at all** without a group scheduler (§4.4). If it is not, the back-port's headline benefit shrinks to flexibility, efficiency and simplicity, and the plan should say so rather than claim the property.
- **The common chapter could become a place where nothing is concrete.** The guard is the second editorial rule: Design states the contract *and the kernelet's own side of it*.
- **Losing the Linux chapter's self-containedness may cost more than it saves.** The fallback, to be taken deliberately rather than by drift: keep Design as a **reference** chapter that both host chapters may restate from, with a rule that Design is authoritative where they disagree. That keeps `linux-mode/index.md`'s promise and accepts the duplication, at the cost of the drift this plan exists to stop.
- **Renumbering churn**, and the 120 link rewrites of the directory rename.
- **Pass 0 may reopen the Paper's central claim.** If "one scheduler" cannot be restated truthfully, the back-port is in tension with the book's thesis, not just with five sentences — and that is worth knowing before pass 1, not after.

---

## Appendix A: where every page and section goes

`D` = Design (common), `A` = Design for Asterinas, `L` = Design for Linux, `—` = deleted. Word counts are raw `wc -w`, inflated by inline SVG (§3.4).

### A.1 Today's Design chapter (Asterinas)

| page | words | → | notes |
|---|---:|---|---|
| `index.md` | 1,092 | split | the figure (redrawn, pass 5) and the four questions → **D**; Asterinas framing → **A** |
| `principles.md` | 2,754 | split | parties, interfaces, crossing, threat model, invariant claims, comparative prose → **D**; each invariant's Asterinas body → **A**; the standing note on Terminology → resolved in pass 3, not carried |
| `builds-and-images.md` | 3,816 | split | two builds, the image, the audit, the entry point and tables → **D**; the window (`KW_*`), embedding and registration in a boot image → **A** |
| `kernelet-api-control.md` | 5,384 | split | the life-cycle state machine and the configuration fields → **D** *as new prose* (§A.4d); the Rust hook API, the hook stack, `guest_memory` → **A**; `spawn_task` → **—** |
| `kernelet-api-service.md` | 5,343 | split | see §A.4a and §A.4b — less moves than it looks |
| `virtualizing-ostd/index.md` | 3,882 | **D** | the classification is the common chapter's centerpiece; **A** needs a new `virtualizing-ostd/index.md` |
| `virtualizing-ostd/memory.md` | 3,713 | split | the grant model → **D**; the linear map, the metadata window, CR3, direct dereference → **A** |
| `virtualizing-ostd/tasks.md` | 3,592 | rewritten | pass 1 rewrites it; the result splits into **D** (*Tasks and virtual CPUs*, *Scheduling*) and **A** (carriers, the trap-return upcall, the tick, the quota's new mechanism) |
| `virtualizing-ostd/interrupts-and-time.md` | 2,798 | rewritten | workers and the job loop → **—**; virtual lines, the tick contract, time → **D**; the host's tick accounting → **A** |
| `virtualizing-ostd/user-mode.md` | 3,017 | split | the round-trip contract, what the tenant cannot reach → **D**; `user_run`'s implementation, the exception table, fault fixups → **A** |
| `virtualizing-ostd/devices.md` | 3,004 | split | enumeration, `IoMem`, a model's halves, buffer checks → **D**; backends and `DmaStream` → **A** |
| `virtualizing-ostd/the-rest.md` | 2,484 | split | boot, power, panic, log as contracts → **D** (its own page, §3.3); the Asterinas bodies → **A** |
| `faults-and-reclamation.md` | 3,341 | split | the three tiers, marking, the drain list, what a death does not do → **D**; stopping a carrier on Asterinas, the reaper → **A** |
| `channels.md` | 2,146 | split | the device, the switch, credit, what crosses → **D**; the host's endpoints → **A** |
| `zero-copy-io.md` | 5,061 | split | see §A.4c |
| `endovisor.md` | 2,684 | **A** | entirely host-specific |
| `kernelet-runtime.md` | 2,255 | split | the OCI verbs, the agent, the bundle → **D**; Asterinas specifics → **A** |

### A.2 Today's Design for Linux chapter

| page | words | → | notes |
|---|---:|---|---|
| `index.md` | 2,105 | **L** | including the "read alone" sentence, edited in pass 4 |
| `kernelets-in-brief.md` | 965 | split | §*What a kernelet needs from any host* → **D**'s index; the rest duplicates the Overview and is deleted, not moved |
| `background.md` | 2,257 | **L** | |
| `principles.md` | 1,890 | split | merged into **D**; each invariant's Linux body stays **L** |
| `builds-and-images.md` | 2,236 | split | `vmap` loading, `set_memory_*`, Linux's shared pages → **L** |
| `kernelet-api-control.md` | 1,322 | split | the endovisor's records stay **L** |
| `kernelet-api-service.md` | 3,891 | split | the C header's processor group, "who is calling" on Linux, what each service becomes → **L** |
| `virtualizing-ostd/index.md` | 1,333 | split | the map figure → **L**; the three kinds → **D** |
| `virtualizing-ostd/memory.md` | 6,006 | **L** | model-and-cache and the walk are Linux's answer to a common contract |
| `virtualizing-ostd/tasks.md` | 4,632 | split | carriers, virtual CPUs, kernelet stacks, per-CPU data → **D** (generalized); the root carrier, `kernel_clone`, binfmt, the watch timer, lifelines → **L** |
| `virtualizing-ostd/scheduling.md` | 7,749 | split | two levels, the four criteria, the upcall, the cooperation contract → **D**; the mirrored count, notifiers, `yield_to`, cgroups **and the fairness argument** → **L** |
| `virtualizing-ostd/interrupts-and-time.md` | 1,484 | split | the bits and the contract → **D**; Linux's timers and the watch timer → **L** |
| `virtualizing-ostd/user-mode.md` | 5,843 | **L** | |
| `virtualizing-ostd/devices.md` | 1,406 | split | the two halves → **D**; `vhost_task` and the Linux objects → **L** |
| `virtualizing-ostd/the-rest.md` | 1,103 | split | the contracts → **D**; the Linux bodies → **L** |
| `faults-and-reclamation.md` | 4,992 | split | the death sequence and the depth rule → **D**; eviction, the die notifier, the stack check, operator requirements → **L** |
| `channels.md` | 870 | split | the host's end stays **L** |
| `zero-copy-io.md` | 1,266 | **L** | |
| `endovisor.md` | 2,897 | **L** | |
| `kernelet-runtime.md` | 1,493 | **L** | |
| `alternatives.md` | 2,039 | split | host-independent alternatives → **D**; Linux's stay **L** |
| `prototype.md` | 7,416 | **L** | evidence gathered on Linux, labeled as such |

### A.3 Elsewhere

| file | → | notes |
|---|---|---|
| `src/paper/introduction.md`, `src/paper/api-virtualization.md` | **deferred** | four sentences and Table 1; a recorded debt (§0), not this series |
| `src/blueprint/overview/api-virtualization.md` | edit | pass 3: the `Schedulers` table row, which becomes false |
| `src/blueprint/overview/challenges.md` | edit | pass 3: C4's worker-task clause, which becomes false |
| `src/blueprint/overview/goals.md` | edit | pass 3: one clause, where the second-scheduler contrast collapses |
| `src/blueprint/overview/terminology.md` | edit | pass 3: "endovisor ABI"; add *carrier*, *virtual CPU*; its forward pointer to "the API Virtualization chapter" is a page, not a chapter |
| `src/blueprint/index.md` | edit | three chapters; and "Nothing in it has run" is already false — the Linux prototype has |
| `src/SUMMARY.md` | edit | the new chapter and the renamed directory; then `make renumber` |
| `src/notes/design-register.md` | edit | in pass 4 for links, pass 7 for the scope column |
| `src/notes/ostd-api-inventory.md`, `io-microbenchmarks.md` | edit | 3 links |
| `src/executive-summary.md` | edit | 1 link, and two figures in pass 6 |
| `AGENTS.md` | edit | pass 5 (reading rule) and pass 7 (structure, vocabulary) |

### A.4 The four splits that are rewrites, not moves

The review checked these four and found the page does not survive being cut in two. Budget new prose, not a move.

**(a) The service half's prologue and epilogue → D.** The listing is host code line by line: `cpu_slot()`, `current_task().kernelet_state()`, `k.task_record(st.name).preempt_count` (a structure pass 1 deletes), `throttle_park` (a mechanism pass 1 replaces), the hook stack and `catch_unwind`. Nothing moves; **D** needs a new statement of the *contract* — what the depth means, what the prologue must check, what the epilogue must guarantee — and each host chapter keeps its own listing.

**(b) The shared pages.** `design/kernelet-api-service.md`'s `## The shared pages` (about 110 lines of `#[repr(C)]` structures) has no destination in the first draft's table, and the layouts are per-host: Linux's are five kinds in a C header, Asterinas's are six in Rust. **D** carries what each page is *for* and the rule that both sides read a generated header; each host chapter carries its own layout.

**(c) Zero-copy I/O.** The first draft assigned 4 of 11 sections. "Where the time goes", "Between kernelets", "Costs" and "What this page decides" were unassigned; worse, "The argument" (→ **D**) rests on tier-1/tier-2 numbers and assumption A14 that live in "Block" and "Network" (→ **A**), and `#lending`'s host half is the same drain list as `faults-and-reclamation.md`, itself split. Either move the whole page to **D** and let it name both hosts' plumbing in two short subsections, or split it after the other pages, when the drain list has settled. Recommend the former.

**(d) The control half's life cycle.** The `Lifecycle` section *is* the Rust API, its doc comments naming `KW_TEXT`, the owner array, `spawn_task`, worker threads and `JOB_GRANT`. The state machine survives the move; the page does not. **D** gets a new life-cycle page written from both chapters' state machines.

---

## Appendix B: the register, re-scoped

One table, with a new **scope** column: *common*, *Asterinas*, *Linux*.

| id | today | after |
|---|---|---|
| D7, D8 | the CPU slot and task records | **Asterinas**, rewritten: the record is per virtual CPU |
| D9, D11 | virtual interrupts as jobs on workers | **retired on both hosts**; D117's upcall becomes **common** |
| D15 | the kernel proper's scheduler is inert | **retired**; D116 replaces it |
| D16 | kernel-mode preemption by `preempt_switch` | **Asterinas**, rewritten: the trap-return path *redirects to the upcall stub* and no longer switches tasks |
| D17 | the host saves user TLS and FPU on involuntary switches | **retired**; D121 takes its place, scope per §4.6(1) |
| D31 | `nice` and affinity builders | **retired** |
| D32 | per-virtual-CPU RCU with a host-set extended quiescent state | **retired** with A9; replaced by an in-kernelet rule that must be checked against OSTD's monitor |
| D61 | a kernelet's tasks are host threads | **retired**; D116 replaces it |
| D62 | the CPU quota is an OSTD throttle; the weight is a per-thread `nice` | **Asterinas**, kept as the *bound*, with a new mechanism the prototype chooses (§4.6(2)); it is the interim answer to share until the prerequisite below is met |
| new | **a group scheduler in the Asterinas host** | a named **Asterinas prerequisite** (§8.3), beside the OSTD ones. Until it exists, the Asterinas chapter states that proportional share is not held on this host, and §5's experiment 4(b) measures how far off it is |
| D66 | the busy tick is counted into a shared record and consumed at a tick point | **Asterinas**, halved: no per-processor timer survives; consumption at tick points does not (§4.1) |
| D67 | no idle threads | **retired** |
| D88 | seats | already retired on Linux; nothing in the Asterinas text depends on the concept (*checked*: only the register mentions it) — but D85, D88, A25 and D101 all link `linux-mode/…/tasks.md#vcpus`, which moves |
| D116–D122 | the Linux second-level scheduler | **common**, with per-host bindings: the mirror, the watch timer and notifiers are **Linux**; the trap-return redirect is **Asterinas** |
| A8 | the host can switch tasks from the trap-return path | **kept**, for the yield stub (§4.3) |
| A9 | host-set RCU quiescence is sound | **kept in substance, rewritten**: the extended quiescent state survives, with the kernelet as its writer, and the monitor must be checked against a virtual CPU that sleeps through a whole grace period |
| A30–A34 | the Linux scheduler assumptions | **Linux** for the mirror and the notifier; **common** for the upcall (A32) and the cost (A34), each needing its own Asterinas measurement |
| new | four Asterinas assumptions | the trap-return redirect written below the hardware frame (unexercised on *both* hosts); the guard-count deferral without a mirror; the per-carrier save of tenant state and the page-table root; share by carrier count under a host with no group scheduler |

---

## Appendix C: the back-port's edit inventory

§6's pass-2 row named eight files. **It is twelve** (*counted* by grepping the Asterinas chapter for the mechanisms §4.3 deletes: `worker`, `job_wait`, `JOB_*`, `task_spawn`, `TaskName`, `BODIES`, `TaskRecord`, `preempt_switch`, `task_park`/`task_unpark`, `task_set_nice`/`task_set_vcpus`, `task_exit`/`task_destroy`/`task_yield`, "idle thread", "512 KiB", "inert"). Four files carry them that the pass-2 row does not mention, which is also why no page left for pass 4 can be extracted before pass 2: the back-port reaches nearly the whole chapter.

| file | what pass 2 must do there | mentions |
|---|---|---|
| `virtualizing-ostd/tasks.md` | rewritten and **split** into *Tasks and virtual CPUs* + *Scheduling* | 30 |
| `kernelet-api-service.md` | the processor group: 8 of 21 services and `job_wait` go; the prologue and epilogue restated | 42 |
| `virtualizing-ostd/interrupts-and-time.md` | rewritten: the upcall replaces the worker jobs; D18's aliasing replaced by the four `arch::irq` primitives (§4.7(4)) | 25 |
| `virtualizing-ostd/index.md` | the 72-row classification: every row naming a worker, a job, an inert scheduler or a host thread | 18 |
| `faults-and-reclamation.md` | the depth rule, the reaper's dead hand-off (§4.7(2)), *Exited*'s definition, and A6 from assumption to unmet prerequisite (§4.7(5)) | 12 |
| `kernelet-api-control.md` | the `spawn_task` hook, the task-name space, the `nice` mapping and the group-scheduler prerequisite | 14 |
| `principles.md` | the entry rule, I6 and I7, and "no kernelet code ever runs in interrupt context", which upcalls falsify | 2 |
| `virtualizing-ostd/the-rest.md` | `__handler_entry` from description to prerequisite (§4.7(1)); the idle rule | 6 |
| `index.md` | the chapter's own figure and its three false labels (§6 pass 6) | — |
| **`virtualizing-ostd/devices.md`** | **not in the pass-2 row**: the completion path is "one worker wakeup per interrupt" four times over, and a device thread's 512 KiB stack | 10 |
| **`virtualizing-ostd/memory.md`** | **not in the pass-2 row**: `JOB_GRANT` as the arrival path for a new run, and `pt_activate` recording the root "so that the host's scheduler restores it", which §4.7(3) changes | 6 |
| **`zero-copy-io.md`** | **not in the pass-2 row**: the completion hop is counted as "the worker, at completion" in the comparison table that carries the page's argument | 3 |
| **`endovisor.md`** | **not in the pass-2 row**: `on_oops(… task: TaskName …)`, "the virtual CPU whose worker delivers its interrupts", and the per-sandbox cost list's 512 KiB device-thread stack | 3 |

Two consequences for the plan. The pass-2 commit is larger than §6 implies, and it cannot be split without the chapter stating two incompatible models in between — which is the reason §6 gave for keeping it one branch, now with a count behind it. And `virtualizing-ostd/index.md`'s classification, which Appendix A sends to Design in pass 4, must be **back-ported before it moves**: 18 of its rows describe the model that pass 2 replaces. That is why the premature move of that page was reverted.

---

## Appendix D: the drift ledger

Work beyond §6's passes, and the owner may reject it. The two extractions each turned up concrete drift that §2's page-by-page comparison had missed, so the remaining eleven page pairs were audited for it systematically: the same knob with two values, the same thing with two names, a rule stated on one host and missing on the other, the same claim at two confidences, and outright contradictions. Thirteen candidates came back. **Every one was re-checked against the source before it was acted on, and one did not survive** — a reminder that an audit is evidence, not a finding.

| # | what | verdict | disposition |
|---|---|---|---|
| 1 | The image must not touch the x87, SSE, AVX or AVX-512 registers. The Linux page states the rule **for both hosts** and its audit checks it; the Asterinas audit has no such item | confirmed | **fixed**: Asterinas audit item 11 |
| 2 | Frame metadata: the Asterinas page's code, mapping step and cost all compute the **flat** index that the page's own D86 retires, and it calls A22 "the live bound" where the register marks A22 superseded by D86 | confirmed, and worse than reported — the same paragraph states D86's cost and then the flat scheme's | **fixed**: five surgical edits |
| 12 | The 98-percent-shareable measurement is flat on Asterinas and **[unverified]** on Linux, for the same unbuilt image | confirmed | **fixed**: the caveat added |
| 13 | A default task ceiling (4,096, *chosen*) is stated only on Linux, though both hosts bound it | confirmed; the *address-space* ceiling beside it is correctly host-specific, since each model costs Linux an `mm_struct` | **fixed**: `max_tasks` added to Design's `config.json` table |
| 3 | `grains_request`'s second argument is a **flag** on Asterinas ("as one contiguous run if set") and a **count** on Linux ("how many must form one run") — one service, one vOSTD source | confirmed | **pass 2**: Linux's count subsumes the flag, so keep the count; the Asterinas call site then passes `grains`, not `1`, which under the count reading asks for nothing |
| 6 | The service signatures said to be "the same on both hosts" and generated from one Rust module differ in width: `u16`/`u8` against `uint32_t` | confirmed | **pass 2**: no basis to choose; one ABI module fixes them once |
| 4 | Kernelet task stack: 512 KiB against 256 KiB | confirmed | **pass 2**: the 512 KiB is inherited from "a kernelet's tasks are host threads", which the back-port deletes |
| 5 | Idle: a 100 Hz default tick (10 ms wake latency) against idling to the kernel proper's next expiry, which Linux makes OSTD prerequisite D122 | confirmed | **pass 2**: §4.7's answer to §4.6(3) settles it — `halt_cpu()` takes no deadline, so D122 is a genuine OSTD addition and cannot be optional on one host |
| 7 | A per-request byte bound (`max_request_bytes`, 4 MiB block / 64 KiB network) exists only on Asterinas; Linux bounds only the *number* of requests by ring depth | confirmed | **pass 5**: Asterinas is right — ring depth bounds requests, not the bytes a descriptor names, and it is charged work under I6 on both hosts. Adding it to Linux is new design content |
| 8 | At the memory ceiling Asterinas may consult policy (`ask_before_oom`, `on_grant_exhausted`) and Linux refuses flatly | confirmed | **owner**: no basis to choose; raising a ceiling is endovisor policy on either host |
| 10 | The vsock operations carry a `VSOCK_` prefix on one host and not the other, and Linux has no half-close and no operation to open a sandbox pair, which common D68 requires | confirmed; the `LISTEN` half is already recorded on Design's runtime page | **pass 2 / pass 5**: prefer one spelling; Linux's table is missing two operations |
| 11 | Reading the exit status is `KERNELET_STATUS` on one host and `KERNELET_WAIT` on the other | confirmed | **pass 2**: Asterinas's name carries its reasoning — a non-blocking read plus `poll`, which no page justifies calling a wait |
| 9 | *Claimed*: the Asterinas chapter still has a per-kernelet kernel page table and non-Global window mappings, which D3 retired | **rejected** | The quoted phrase "window mappings are not Global" appears nowhere in the chapter (*checked*), and the CR3 text at `kernelet-api-service.md:299` is consistent with D3. The residue is three doc comments saying "the kernelet's kernel page table", two of which pass 2 deletes; the third is a phrasing nit |

Three near-misses were checked and rejected, and should not be re-investigated: the stack reserve of 64 KiB against 16 KiB (on Linux a service runs on the carrier's own stack, so the kernelet stack needs less headroom); the audit's "no stack frame over 4 KiB" existing only on Linux (it makes the Linux-only function-entry check sound, and §4.7(5) turns that check into the primary mechanism on Asterinas too, so it becomes common in pass 2); and Linux re-zeroing grains at destroy (stated reason, Linux does not clear memory handed to its own allocations).

The vsock credit clamp, 64 KiB against 256 KiB, is not in the table because Design's Channels page already records it.

---

## 8. The owner's decisions

All six questions were put to the owner and answered on 2026-09-27. They are recorded here and folded into the sections they govern.

| # | question | decision |
|---|---|---|
| 1 | **The Paper** (§0) | **Waits.** The four Paper sentences are a recorded debt with a trigger; the three Overview sentences, two of which the back-port makes false, are pass 3. |
| 2 | **The order** (§2.4) | **Back-port first**, then extract. |
| 3 | **Proportional share on Asterinas** (§4.4) | **Name a group scheduler as a prerequisite.** The property is stated as *not held* on the Asterinas host until one exists, alongside the OSTD prerequisites. |
| 4 | **The gate on evidence** (§5) | **Run experiments 1–3 first.** No back-ported prose is written until the minimal Asterinas prototype has run them. |
| 5 | **§4.6's four open mechanisms** | **The prototype settles them**, and reports each as a finding. |
| 6 | **The structural fallback** (§7) | **Extract as planned.** Design becomes authoritative, the host chapters stop restating it, and `linux-mode/index.md`'s "written to be read alone" is edited away in pass 4. The fallback is not taken. |

Decisions 2 and 4 interact: because the prototype gates the prose, the work now begins with a prototype run rather than with writing, and §6 is ordered that way.

---

## 9. What the reviews changed

Three independent reviews read the first draft: one on the design content, one on executability against the book's tooling, one on the premises. All three are folded in. Between them they reported thirteen blocking issues, and seven of those changed a conclusion rather than a wording:

- **The Paper conflict** (§0), which the reviews surfaced and the owner then decided: the Paper waits, the two false Overview sentences do not, and the debt is recorded with its trigger.
- **"Fairness becomes a property of the design" was wrong** (§4.4): the Linux argument's load-bearing step is the control group, which Asterinas does not have. Two sandboxes at one `nice` with different carrier counts do not get equal share.
- **The unverified assumptions do not go down, they go up** (§4.3). A8 stays for the yield stub; A9 changes hands but survives in substance; the stack-push redirect is unexercised on *both* hosts, because the Linux prototype kept the interrupted pointer in the record.
- **The redirect as written could not be implemented** (§4.1.1): on a kernel-mode trap with no interrupt-stack table, the words just below the interrupted stack pointer are the `SS` and `RSP` the `iretq` pops.
- **Two of the three "simpler than Linux" claims were wrong** (§4.2): the record needs two fields and the `disable_local` aliasing must be re-taken, and address-space adoption *does* have a counterpart — D17, which must be kept, because a carrier is a host kernel thread and the host's schedule handlers skip those.
- **The pass order would have failed `make check` at three commits** (§6): moves and repoints must be in the same commit, and passes 1 and 2 must be one branch.
- **The word arithmetic did not close** (§3.4), four "split" rows are rewrites (§A.4), and the new chapter's directory, three missing index pages and the figures had no owner (§3.1, §6).

The net effect on the recommendation: the back-port is still the right move, for the reasons in §4.4 — but it is **comparable in size to the Linux one, not smaller**, it adds three mechanisms Asterinas does not have today, and it cannot be stated as design until §5's first three experiments run. The plan's estimate of its own cost went up; its conclusion did not change.
