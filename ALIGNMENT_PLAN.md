# Bringing the two host designs into line

*A plan, not a revision. Nothing under `src/` is changed by the commit that adds this file. It answers three questions: how the two design chapters compare today, what shape the Blueprint should take, and in what order to get there. Written 2026-09-22 against `main` at `a2188d0`, then revised once against three independent reviews of the plan itself; §9 lists what the reviews changed.*

---

## 0. One thing to decide before anything else

The task set the blast radius at "the Blueprint and the design register — the Paper, Executive Summary and Notes stay". **That instruction cannot be followed as given, and the conflict is in the book's own rules.**

`AGENTS.md`: *"where a Note disagrees with the Blueprint, the Blueprint wins, and where either disagrees with the Paper, the **Paper** wins."*

The Paper is not merely decorated with the old task model; it is built on it:

| where | what it says |
|---|---|
| `paper/introduction.md` | "Its threads are threads of the host kernel, scheduled by the host" |
| `paper/introduction.md` | machine virtualization is indicted because "a second scheduler runs beneath the guest's" |
| `paper/api-virtualization.md` | "there is **one scheduler**"; Table 1's row reads `| Schedulers | two | one | one |` |
| `overview/goals.md` | "A second scheduler runs beneath the guest's, so the guest's decisions about which thread to run are made twice" |
| `overview/challenges.md` | "every deferred piece of work must run on the kernelet's own **worker task**" |

The back-port *installs* a second scheduler, on both hosts, and deletes the worker task. So after pass 2 the Blueprint would say one thing and the Paper, which wins, would say the opposite. The Overview is in the same position, which also falsifies this plan's earlier claim that Overview is untouched.

The cost of fixing it is small, and much smaller than the cost of not fixing it: `paper/design.md` and `paper/implementation.md` are empty stubs today, so there is almost nothing to rewrite, and writing them later against a model the Paper contradicts is the expensive path. What is needed is a decision in writing about what "one scheduler" now claims — most likely *one scheduler of processors, and no hardware exit beneath it*, which is still true and still the argument against machine virtualization — and five sentences edited to match.

**Recommendation: add pass 0 (§6), five sentences across the Paper and the Overview, before pass 1.** If the owner would rather hold the line on scope, the alternative is to stop after pass 2 and leave the Blueprint knowingly in conflict with the Paper until a later run — which `AGENTS.md` forbids reading the other way round, so it must at least be recorded as a deliberate debt.

The Paper is *already* out of line with the Blueprint in one place, which weakens "out of scope" further: `paper/api-virtualization.md` still describes the Linux host with "per-CPU data is selected by a **seat**" and "every kernelet task is carried for life by a Linux task", both retired by D88 and D116 in the last run.

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

**Directories.** `src/blueprint/design/` is today the Asterinas chapter, so the names must be settled before pass 3 starts. Following the precedent of `linux-mode/`:

| chapter | directory |
|---|---|
| Design (common) | `src/blueprint/design/` — reused, its meaning widened |
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
| a carrier per virtual CPU | a host kernel thread, pinned to the virtual CPU's host CPU, created by the endovisor at `start` |
| a way into the image | `_kernelet_entry` on virtual CPU 0, the entry table's `vcpu_entry` on the others |
| an upcall to a virtual CPU running kernelet code | the host's **trap-return path**, which it already owns. *Checked on the tree at `ab9a4cfdc`*: the kernel-mode entry stub saves every general register and the `iretq` frame on the interrupted stack and calls `trap_handler(f: &mut TrapFrame)` (`ostd/src/arch/x86/trap/trap.S`, `ostd/src/arch/x86/trap/mod.rs:151`), whose `f.rip` and `f.rsp` the `iretq` restores — so the redirect is two stores in Rust the host already runs, with no timer callback and no `get_irq_regs()` equivalent, which is what Linux needs |
| virtual interrupts | bits in the per-virtual-CPU record, which that record already is; the host sets, the kernelet takes |
| the tick | the host tick still adds to the virtual CPU's `tick_pending` and **no timer is armed per processor** — that much of D66 survives. What does *not* survive is "consumed at the kernelet's next tick point": once the kernelet's own scheduler ends time slices, a compute-bound task reaches no tick point, so the tick must be delivered as an upcall like any other virtual interrupt |
| idling a virtual CPU | `vcpu_idle`: the carrier parks; the host wakes it when a bit is set |
| kicking another virtual CPU | `vcpu_kick`: set the bit, wake the carrier, or send a reschedule interrupt so that its trap-return path delivers the upcall |
| kernelet stacks | `kstack_alloc` / `kstack_free` over the host's allocator, from a per-sandbox pool, charged to the kernelet |
| a tenant thread's floating-point state | open, §4.6(1) |

### 4.2 Three things simpler than on Linux

1. **No mirrored preemption count.** On Linux, vOSTD raises Linux's own per-processor count. On Asterinas the host already reads the kernelet's guard count out of a shared record at its preemption point (D16 does this today). The count moves from the task record to the virtual CPU's record, and the host's rule becomes: *do not preempt a carrier whose virtual CPU is in a critical section; at the bound, redirect it through the yield stub.* No mirror, no un-mirroring around service calls, no risk of leaving the host's count wrong.
2. **No watch timer and no preemption notifiers.** The host's own tick already fires on every processor and already knows which kernelet task it interrupted, which is where `policy.preempt_off_ticks` is enforced today.
3. **No address-space adoption.** A carrier runs the kernelet's own page table and `pt_activate` writes CR3. `kernelet_switch_mm()`, the per-model Linux address space and its reference counting have no counterpart.

What Asterinas must add that Linux did not: nothing. The upcall redirect is the trap-return hook it already has, reduced from "run OSTD's switch protocol" to "rewrite two fields of a trap frame".

### 4.3 What the back-port deletes

| deleted | register | why it goes |
|---|---|---|
| `task_spawn`, `task_exit`, `task_destroy`, `task_yield`, `task_park`, `task_unpark`, `task_set_nice`, `task_set_vcpus` — 8 of 21 services | D61, D31 | kernelet tasks are no longer host tasks |
| `job_wait` and the per-virtual-CPU **worker threads** | D9, D11 | virtual interrupts arrive by upcall on the interrupted task |
| the `spawn_task` hook and `KerneletTaskBody` | D61 | nothing spawns host threads per task |
| `RUNNING`, `BODIES`, the `TaskName` space | D7 | there are no names to exchange |
| the per-task `TaskRecord` | D8 | the record becomes per virtual CPU |
| `preempt_switch` as a *task switch* from the trap-return path | D16, A8 | the host redirects; the kernelet switches its own tasks |
| the host's save of FS/GS and XSAVE on involuntary switches | D17 | the state belongs to a tenant thread and moves with it |
| the host-set RCU extended quiescent state | D32, A9 | a virtual CPU that idles knows it is idling |
| the no-idle-threads `cfg` lines | D67 | a kernelet has idle tasks again, as on a machine |
| `nice`/affinity builders on `TaskOptions` and the kernel proper's `cfg` lines for them | D31 | the kernel proper's own scheduler honors them now |

**What happens to the unverified assumptions, stated correctly.** An earlier draft claimed A8 and A9 are "retired, not carried". That was wrong, and the correction matters:

- A8 (*the host can run OSTD's switch protocol from the interrupt-return path*) is **replaced** by a weaker claim — *the host can rewrite two fields of a trap frame so that the interrupted kernelet code resumes at the image's upcall stub*. Strictly less risky, and §4.1 shows the code that would do it. It is still an assumption, and it is an **Asterinas** assumption: the Linux prototype's A32 measured a redirect from a *timer callback* on a different kernel, and does not discharge it.
- A9 (*host-set RCU quiescence is sound*) is **replaced** by *a kernelet notes its own quiescence at its own switch points, and a virtual CPU that idles notes it before parking*. That is how RCU works on a machine, so the argument is better, but OSTD's grace-period monitor has to be checked against the carrier model rather than assumed.
- Appendix B adds three further Asterinas assumptions (the redirect, the guard-count deferral without a mirror, share by carrier count under a host with no group scheduler).

So: the *risk* drops, the *count* does not. §5 says what would discharge them.

### 4.4 What it buys

- **A tenant's thread count stops buying machine share.** The currency changes from tenant-chosen (how many threads it runs) to operator-chosen (how many virtual CPUs it was given). That is the honest claim, and it is a real gain. It is *not* "fairness becomes a property of the design": the Linux argument's second step is "**the control group** bounds the N tasks", and Asterinas has no group scheduler, so *N* carriers at a `nice` still take *N* shares against a neighbor's *M*. Proportional share on Asterinas needs either D62's quota kept as the bound, or a group scheduler in the host — which is a scheduler project and out of scope. **For this reason the fairness argument belongs in each host chapter, not in Design.**
- **The tenant's kernel schedules the tenant's threads**: real-time policies, `nice` within the sandbox, `/proc/loadavg` and `sched_getscheduler` become true instead of inert.
- **A sandbox stops costing the host two task objects and a 512 KiB stack per tenant thread.** A thousand tenant threads cost *N* carriers and a thousand kernelet stacks from the sandbox's own accounted pool.
- **A task switch stops crossing**: ~1,000 cycles, *measured on the booted prototype of the Linux design*, **[unverified]** on Asterinas.
- **Chapter 12 gets smaller**: eight services, one hook, two shared-page structures, three mechanisms and two assumptions leave it.

### 4.5 What it costs

- The OSTD prerequisites the Linux design names (D118, D122) become prerequisites on **both** hosts — which is where they belonged, since they are additions to OSTD, not to a host.
- **The host loses its view inside a sandbox** (§2.2): no load balancing within the sandbox's CPU set, no per-thread charging, no host-side visibility of what a tenant runs. `times(2)` becomes the kernelet's own business, as on a machine.
- **The kernel proper's scheduler becomes load-bearing** without evidence that it is good (§2.2, A33).
- **Neighbor latency.** The Linux chapter calls the cooperation contract "the one thing this design costs a host that a container does not": about 2 ms on every processor a sandbox may touch, and "a real-time Linux should not host kernelets at all". The Asterinas equivalent must be stated the same way — and note that Asterinas's present bound **kills** the kernelet (`preempt_off_ticks` → `PreemptOffTooLong`) where Linux's forces a yield. Which policy Asterinas keeps is a decision pass 1 must take, not inherit.
- Every back-ported claim rests on evidence from the other host until §5 is done.

### 4.6 What the back-port leaves open

1. **Does Asterinas need the FPU services at all?** The host is Asterinas; `FpuContext::save`/`load` are OSTD's own code and might stay identical with the host saving nothing. The Linux prototype's unexplained deviation about *where* the save belongs must be understood first.
2. **What enforces the quota once `task_park`/`task_unpark` are deleted?** D62's throttle is implemented through them: every task parks at its next quiescent point on a per-kernelet throttle queue. With carriers there are no per-task parks, so the ceiling needs a new mechanism (park the carriers, or refuse to schedule them) — pass 1's work, not a question to defer.
3. **Do worker threads survive for anything?** With upcalls there is nothing left for a worker to fetch. Confirm nothing else in the chapter depends on a per-virtual-CPU thread existing.

---

## 5. Evidence: what an Asterinas prototype must show

The back-port's claims would otherwise rest on a prototype of the *other* host. The Linux prototype is good evidence that the mechanism works at all, and no evidence about Asterinas's trap path, its scheduler or its accounting.

A prototype in the Asterinas tree (`~/Workspace/asterinas`), in the spirit of the Linux one: a minimal endovisor, a minimal vOSTD, and the tree's own 100-line example kernel unchanged.

**The gate.** Items 1–3 are also §7's soundness checks. Running them *first* discharges both, and **until they pass, the back-ported mechanisms are a proposal in the text, not a design**: every claim carries **[unverified]** and every number names the host it was measured on.

1. **Hello World on the real path.** The 100-line kernel, source byte-identical, as a kernelet on an Asterinas host: one carrier, the entry table, the service table, a tenant address space, `user_run`, two system calls.
2. **A virtual CPU that is a carrier.** *N* = 2 carriers; the kernel proper's injected scheduler running its own tasks on them; a trace checked against a reference model, as the Linux phase-4 test does.
3. **The upcall from the trap-return path.** The host rewrites `f.rip`/`f.rsp` in `trap_handler` so that an interrupted kernelet task at depth 0 enters `virq_entry` and resumes intact. Assertion: a checksum computed across thousands of upcalls is bit-identical to one computed undisturbed.

Then:

4. **Share by carrier count.** Two sandboxes on the same host CPUs, one running 1 busy kernelet task and the other 50, with equal configuration. Assertion: neither sandbox's share moves with its task count. (Today's design fails this by construction. Note that the Linux prototype measured a sandbox against a *sibling control group*, never two sandboxes, so this experiment is new work, not a port.)
5. **Cooperation and its bound.** A kernelet holding a spin lock across the host's preemption point: zero involuntary switches inside a critical section, and a bounded stay for a deliberate overstayer — with the kill-or-yield decision of §4.5 exercised.
6. **What was deleted is really gone.** A sandbox with 50 tenant threads must cost *N* host threads plus device threads, not 50-something, and no 512 KiB host stack per tenant thread.

Not in scope for a first Asterinas prototype: devices, channels, zero-copy I/O, the runtime, more than one kind, the window's full layout.

---

## 6. The passes, in order

Revised after review: the original pass order would have failed `make check` at three of its six commits. The rule that fixes it is **no commit may leave a link pointing at material that has moved**, which means page moves and their repoints — register included — happen together.

| # | pass | what it does | why here |
|---|---|---|---|
| **0** | **Settle the Paper** | Decide what "one scheduler" claims; edit the five sentences of §0 in the Paper and the Overview. Reconcile `overview/terminology.md` at the same time: it still defines "endovisor ABI" as the image's table (the standing note in `design/principles.md` has flagged this for a while) and has no *carrier* or *virtual CPU*. | Everything after this contradicts the Paper, which by `AGENTS.md` wins. Cheap now, expensive later. |
| **1+2** | **Back-port, as one branch** | Rewrite `virtualizing-ostd/tasks.md` (splitting it into *Tasks and virtual CPUs* + *Scheduling*), `interrupts-and-time.md`, the processor group of `kernelet-api-service.md`, the `spawn_task` hook and task-name space in `kernelet-api-control.md`, the entry rule and I6/I7 in `principles.md`, `faults-and-reclamation.md`, `the-rest.md`, and `index.md` including its figure. Revise D7, D8, D9, D11, D15, D16, D17, D31, D32, D61, D62, D66, D67; replace A8 and A9; add the Asterinas side of D116–D122 and the three new assumptions. | These pages quote each other's model. Split across two commits, the chapter states two incompatible things in between — `make check` tests links, not sense. **Run §7's tree checks before writing.** |
| **3** | **Create Design, page by page** | For each page in §3.3: create it, move the text, repoint **every** inbound link in the same commit (the register's 167 included), update `SUMMARY.md`, run `make renumber`, check. Rename `design/` → `asterinas-mode/` first, as one mechanical commit. Add the three missing `index.md` files. | Moving one page at a time keeps every commit green, which the all-at-once order could not. |
| **4** | **Trim the host chapters** | Delete from both host chapters what Design now holds; add each chapter's *What this chapter assumes* page; edit `linux-mode/index.md`'s "written to be read alone" and `AGENTS.md`'s reading rule **in this commit**, as `AGENTS.md` requires of a reversed decision. | The chapters must stop restating the common core, or it will drift again. |
| **5** | **Figures** | Redraw the architecture figure (it says "a kernelet's threads are host threads", "entry table: start a thread", "service table: 21 C-ABI calls" — all three die in pass 1), the Executive Summary's two, and the seven Linux figures that sit on split pages. `make render` each and look at it. | Figures cannot be moved mechanically and none of the other passes owns them. |
| **6** | **Register and conventions** | Finish the scope column (Appendix B); `AGENTS.md`'s structure and vocabulary sections; the index child lists, which are hand-written and unchecked. | What is left after pass 3 did the link work. |
| **7** | **The Asterinas prototype** | §5, in the Asterinas tree, on its own branch; then fold its numbers and deviations back in, as the Linux prototype's were. Items 1–3 are the gate of §5 and should run *before* pass 1 if the schedule allows. | A separate run. |

**Exit criteria, every pass**: `make renumber` when `SUMMARY.md` or a heading changed; `make check` prints `check: OK`; `make build` exits 0; every changed figure rendered and looked at; external links verified by hand against the local v6.12 tree or the Asterinas tree at `ab9a4cfdc` (no target does this — `make check` skips `http(s)` entirely); no stale vocabulary; the commit message says what changed, what was verified with the actual output, and what the next pass should attack.

**A checklist the tooling cannot replace.** `make check` validates link targets, anchor ids, orphans and bare `§`s — but a link that still resolves to a file whose *material* has left is invisible to it, and 514 of the 818 internal links into these two chapters are unanchored. Each page move in pass 3 therefore carries a before/after diff of that page's `##` headings, and every link into the page is checked against it by hand. Two further tool limits worth knowing: `make renumber` rewrites only the *text* of `[§…](href)` links and never an href, and it numbers `##` headings only — so no `§` link may point at a `###` heading (today none does; keep it that way by giving every linked heading `##` level or an explicit `{#id}`).

---

## 7. How this plan could be wrong

- **The back-port could be unsound on Asterinas for a reason not visible from Chapter 12.** Candidates to check against the tree *before* pass 1 writes anything: the hook stack and `catch_unwind` when a hook is reached from an upcall rather than a service call; the reaper's assumptions about task lifetimes; CR3 restoration when the host switches away from a carrier; `InterruptLevel` bookkeeping across a redirect; nested interrupts and the double-fault path; and whether OSTD's RCU monitor works unchanged with carriers.
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
| `principles.md` | 2,754 | split | parties, interfaces, crossing, threat model, invariant claims, comparative prose → **D**; each invariant's Asterinas body → **A**; the standing note on Terminology → resolved in pass 0, not carried |
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
| `src/paper/introduction.md`, `src/paper/api-virtualization.md` | edit | pass 0 (§0), including Table 1 |
| `src/blueprint/overview/goals.md`, `challenges.md` | edit | pass 0: the second-scheduler sentence, the worker-task sentence |
| `src/blueprint/overview/terminology.md` | edit | pass 0: "endovisor ABI"; add *carrier*, *virtual CPU*; its forward pointer to "the API Virtualization chapter" is a page, not a chapter |
| `src/blueprint/index.md` | edit | three chapters; and "Nothing in it has run" is already false — the Linux prototype has |
| `src/SUMMARY.md` | edit | the new chapter and the renamed directory; then `make renumber` |
| `src/notes/design-register.md` | edit | in pass 3 for links, pass 6 for the scope column |
| `src/notes/ostd-api-inventory.md`, `io-microbenchmarks.md` | edit | 3 links |
| `src/executive-summary.md` | edit | 1 link, and two figures in pass 5 |
| `AGENTS.md` | edit | pass 4 (reading rule) and pass 6 (structure, vocabulary) |

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
| D62 | the CPU quota is an OSTD throttle; the weight is a per-thread `nice` | **Asterinas**, kept as the *bound*, with a new mechanism (§4.6(2)); it is also, until a group scheduler exists, the only answer to proportional share |
| D66 | the busy tick is counted into a shared record and consumed at a tick point | **Asterinas**, halved: no per-processor timer survives; consumption at tick points does not (§4.1) |
| D67 | no idle threads | **retired** |
| D88 | seats | already retired on Linux; nothing in the Asterinas text depends on the concept (*checked*: only the register mentions it) — but D85, D88, A25 and D101 all link `linux-mode/…/tasks.md#vcpus`, which moves |
| D116–D122 | the Linux second-level scheduler | **common**, with per-host bindings: the mirror, the watch timer and notifiers are **Linux**; the trap-return redirect is **Asterinas** |
| A8 | the host can switch tasks from the trap-return path | **replaced** by the weaker redirect assumption, still **[unverified]** on Asterinas (§4.3) |
| A9 | host-set RCU quiescence is sound | **replaced** by the in-kernelet rule, to be checked |
| A30–A34 | the Linux scheduler assumptions | **Linux** for the mirror and the notifier; **common** for the upcall (A32) and the cost (A34), each needing its own Asterinas measurement |
| new | three Asterinas assumptions | the trap-return redirect; the guard-count deferral without a mirror; share by carrier count under a host with no group scheduler |

---

## 8. What this plan asks the owner to decide

1. **The Paper (§0).** Add pass 0 and edit five sentences, or accept a knowing conflict with the rule that the Paper wins. *Recommendation: pass 0.*
2. **The order.** Back-port first (§2.4), or extract first and write two pages twice.
3. **Proportional share on Asterinas (§4.4).** Accept that the back-port buys "thread count no longer buys share" and not proportional share, keeping D62 as the bound — or open a group scheduler as a named Asterinas prerequisite.
4. **The gate on evidence (§5).** This plan says the back-ported mechanisms are a *proposal* in the text until §5's items 1–3 run on Asterinas. Agreeing to that means either running them early or marking a chapter's worth of mechanism **[unverified]** for a while.
5. **§4.6's three open questions**, or leave them to pass 1 to settle against the tree.
6. **The fallback structure (§7)**, if `linux-mode/index.md`'s "written to be read alone" is worth more than the deduplication.

---

## 9. What the reviews changed

Three independent reviews read the first draft: one on the design content, one on executability against the book's tooling, one on the premises. The second and third have been folded in; the first is still running as this revision is written, and anything it finds will be reported separately rather than silently merged.

Corrections that changed a conclusion, not just wording:

- **The Paper is not out of scope** (§0). New, and it overrides the task's stated blast radius.
- **"Fairness becomes a property of the design" was wrong** (§4.4): the Linux argument's load-bearing step is the control group, which Asterinas lacks.
- **"Two unverified assumptions are retired" was wrong** (§4.3): they are replaced by weaker ones that the Linux prototype does not discharge.
- **The pass order would have failed `make check` at three commits** (§6): moves and repoints must be in the same commit, and passes 1 and 2 must be one branch.
- **The word arithmetic did not close** (§3.4), and four "split" rows are rewrites (§A.4).
- **Three Asterinas advantages were undersold** (§2.2) and three costs were missing (§4.5).
- **The new chapter's directory, three missing index pages, and the figures** had no owner (§3.1, §6 pass 5).
