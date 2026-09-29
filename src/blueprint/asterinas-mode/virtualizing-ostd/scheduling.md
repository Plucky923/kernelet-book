# Scheduling

*How a sandbox's share of the machine is decided, and how the kernelet is told that time has passed or that it should give the processor back. Part of question 2, and where invariant I6's CPU-time half is discharged.*

There are **two levels, and they decide different things**. The host decides how much of the machine each sandbox gets, by scheduling its carriers as it schedules any kernel thread. The kernelet decides which of its own tasks uses that share, with the kernel proper's own scheduler, unmodified. Neither makes the other's decision, which is what separates this from a guest under a hypervisor, where two schedulers make the same decision twice.

The host must therefore be able to do three things to a carrier that is running kernelet code: tell it that a virtual interrupt is pending, take the processor back from it, and stop it. All three are the same mechanism, and it is the one thing on this page the tree does not already have.

## The upcall {#upcall}

A virtual interrupt is not delivered by waking a worker task, as this chapter said before; there are no workers. It is delivered by **redirecting the carrier at the return from a host trap** into a stub in kernelet text, which runs the kernelet's handlers and returns to exactly where the carrier was.

Each virtual CPU has a record in the shared pages, written by both sides:

- **pending bits** — a tick, a kick, and the device interrupt lines — set by the host with a release store;
- **`guards`**, the kernelet's count of held guards, read by the host;
- **`irq_off`**, the kernelet's virtual interrupt flag, read by the host.

Two fields and not one, for the reason the Linux prototype found and this one confirmed: a single count would mean that a kernelet holding a spin lock received no ticks. They are also why the aliasing of `irq::disable_local` and `DisabledLocalIrqGuard` to the preemption count is withdrawn (register D18): the L1 bottom half calls `disable_preempt()` and then `arch::irq::enable_local()`, which are two distinct facilities, and under the aliasing the second would have to lower the count the first just raised. The four real `arch::irq` primitives sit over `irq_off` instead.

**The redirect.** OSTD adds one hook where `trap_handler` is about to return to kernel mode. *Checked on the tree*: the kernel-mode path `_trap_from_kernel` saves every general register and returns with `iretq`, and `trap_handler(f: &mut TrapFrame)` may rewrite the frame, whose last five words are exactly what the `iretq` consumes. The hook fires only when **all six** of these hold:

1. the frame is a kernel-mode frame;
2. its `rip` lies in `KW_TEXT`;
3. the record's service depth is zero;
4. `guards` is zero and `irq_off` is clear;
5. `InterruptLevel::current().is_task_context()`;
6. `rip` is not inside the stub's own range.

The fifth is not redundant with the third, and no one predicted it: **a nested interrupt's bottom half runs on the kernelet stack at depth zero**, so depth alone would redirect out of a handler (*measured on the booted Asterinas prototype*, finding F2).

Then the hook writes the interrupted `rip` into a hand-off slot below the hardware frame, points the frame's `rsp` at it and its `rip` at the stub. The arithmetic is worth stating exactly, because two earlier versions of it were wrong:

```
slot = align_down(f.rsp, 16) - 56      // 8-aligned, not 16
```

The words below the interrupted stack pointer are free only because the kernel builds carry `-C no-redzone=y`, which therefore becomes a **build requirement for the kernelet image and an audit item** ([Builds and images](../builds-and-images.md#audit)). A slot at `f.rsp − 48` is wrong: when the interrupted stack pointer is 8 modulo 16, that address *is* the hardware frame's lowest live byte, and writing there clobbers the `rip` the `iretq` pops. Eight-aligned rather than sixteen is not an accident either — it is what the calling convention wants at a function's entry, so the stub need not be naked assembly. The prototype reaches the slot by taking `&f.trap_num` rather than computing an offset, which is what makes it right at both alignments.

Three more things the hook must do before it returns, each of which is a way the first draft was unsound:

- **Clear the interrupt flag in the frame's `rflags`** as well as setting the record's mask. The `iretq` restores `rflags` from the frame, so the mask alone leaves a window in which a second redirect overwrites the first return address.
- **Sample the interrupted privilege into the record.** The stub cannot derive it: the redirect only ever rewrites kernel-mode frames, so the frame always says kernel, yet a tick that interrupted the tenant in user mode must be charged as user time.
- Leave `cs` and `ss` alone.

**The stub** preserves all fifteen general registers, because the interruption point is arbitrary and not a call boundary; enters the interrupt level itself with the sampled privilege, because `InterruptLevel` is the host's cell and is back to its pre-trap value by the time the `iretq` runs, so the kernelet's replica would read task context and the first delivered interrupt would hit the bottom half's `unreachable!("this function must have been call in interrupt context")`; runs the kernelet's handlers; may switch kernelet tasks; then unmasks and returns by rebuilding its own `iretq` frame, restoring `rflags` last.

**It works.** *Measured on the booted Asterinas prototype*: against an undisturbed run of 0 upcalls and 907 deferrals, the disturbed run took **3,895 upcalls with 0 deferrals, and every checksum chunk was `0xf705414f5604b292`, bit for bit the undisturbed value.** The whole host patch is **50 added lines in three files** — a trap-return hook, two accessors on `UserContext`, and two items widened to `pub` — against 169 lines in 8 files for the Linux host's gate.

**Three targets, not one.** The redirect replaces only the first of the three jobs the old `preempt_switch` did. The other two need stubs of their own: a **yield stub**, which reaches the host's own voluntary switch in task context at depth zero, and an **exit stub** for termination. Without the yield stub the host can never take a processor back from a carrier that computes, and nothing else in OSTD does: *checked*, `might_preempt()` is reached only from `halt_cpu`, the return to user mode, and after an enqueue. That is what [assumption A8](#decides) is about, and it stays.

## Giving the processor back {#cooperative}

A carrier may hold its processor while the kernelet is inside a critical section, because the host will not redirect it there. The bound on that is the one thing this design costs a host that a container does not, and it is stated the same way on both hosts: about **2 ms**, counted by the host in the carrier's own run time since the last real scheduling event, never read from the kernelet's record.

**At the bound the carrier yields; it is not killed.** This chapter said before that the kernelet is killed, with `preempt_off_ticks` and `PreemptOffTooLong`. That is wrong, and the reason is structural: the L1 bottom half disables preemption for its whole duration and the kernel proper's work there is not bounded by construction, so a killing bound would kill *correct* kernelets. The yield stub has to exist anyway, and is reachable exactly at that point, so yielding costs no new mechanism. The host counts the yields.

Two prerequisites make the yield land promptly, both additions to OSTD and both needed on either host:

- **A preemption check at the end of a critical section.** Neither guard's `Drop` calls `might_preempt()` today. Both must, when the count reaches zero, *gated* on task context and on interrupts being enabled — because the switch runs `might_sleep()`, which panics on a nonzero guard count or with interrupts off, and the L1 bottom half drops a preemption guard with interrupts deliberately off. And because a context switch forgets its guard while the next task re-enables with the bare primitive, the guard `Drop` alone cannot see every section end: **the four `arch::irq` primitives must carry the check too.**
- **A preemption check on an interrupt's return to kernel code**, after the callbacks return, when the interrupted privilege was kernel and the frame's interrupt flag was set, with interrupts re-enabled first — and in the other two architectures' trap paths for parity.

## The quota, and what share a sandbox gets

The configuration's quota is enforced by **parking the carriers**, at the host tick and at the yield stub. The old mechanism parked each *task* at its next quiescent point, and there are no per-task parks left; nothing in OSTD throttles a computing host kernel thread from outside, and the run queue has no throttled state, but `park_current` and `unpark_target` act on a host task and a carrier is one (register D62, kept as the bound, with the carrier as its object). **[unverified]**: chosen with reasons, and not implemented by the prototype.

**Proportional share is not held on this host.** A sandbox's share is the sum of its carriers' shares, and *checked on the tree*, `kernel/core/src/sched/sched_class/fair.rs` carries a per-*thread* weight of 1024·1.25<sup>−nice</sup> with no group entity anywhere under `kernel/core/src/sched/`. Two sandboxes at the same `nice` with two and eight virtual CPUs get one share against four. What the back-port does buy is real and smaller than fairness: **a tenant's thread count stops buying machine share**, because the currency is now the operator-chosen virtual CPU count rather than the tenant-chosen thread count.

A **group scheduler in the host kernel is therefore a named prerequisite** of this design, recorded beside the OSTD ones, and until it exists this chapter states plainly that the property is not held here. Two things a first version could do instead, recorded as such and not as the design: a per-carrier weight of about *W*/*N* off the forty-step geometric ladder, or leaning on the quota above as the equalizer. Nothing in the prototype bears on this either way: its host is a minimal OSTD kernel rather than `kernel/core`, so **it is not evidence about share**.

## What this asks of OSTD {#asks}

Additions, not host-specific ones — each is needed whichever kernel is the host (register D118): the trap-return redirect hook; `vcpu_idle` with a deadline, since `halt_cpu()` is the only idle primitive and takes no argument (register D122); the `might_preempt` checks above, in two `Drop` implementations, the four `arch::irq` primitives and three architectures' trap paths; an interrupt-stack-table entry for vector 8 ([Tasks](tasks.md#stacks)); and an extended quiescent set for RCU, since a grace period completes only when every processor has switched and a virtual CPU asleep in `vcpu_idle` passes no switch point.

## Costs

- Per virtual interrupt: one redirect (a handful of loads and compares on a path every kernel-mode trap return takes machine-wide), the stub's register save, and the handlers — against a worker wakeup of about 3 µs before.
- Per critical section: one check at its end, gated.
- Per bound hit: one yield, counted by the host.
- Per quota period: one park and one unpark per carrier.

## What a tenant sees

- Its own scheduler's decisions, honored: priorities, classes, affinities within the sandbox, and a truthful load average ([Tasks](tasks.md)).
- Latency that includes the host's: a carrier waits for the machine as any thread does, and a sandbox's virtual CPUs are not gang-scheduled.
- Nothing of the bound, unless it holds a processor for 2 ms, in which case the kernelet yields and continues.

## What this page decides {#decides}

- **Virtual interrupts are delivered by redirecting a carrier at a trap return, under six conditions** (register D117, this host's binding; it retires D9 and D11, the worker jobs, and rewrites D16, which is no longer a task switch. D119, Linux's mirrored preemption count, has no counterpart here: there is no mirror to build). **[unverified]** in one respect: the stack hand-off is exercised by this prototype and by no other, and the Linux prototype kept the interrupted instruction pointer in the record instead.
- **The bound yields and is counted by the host** (register D124, new, replacing the kill of D66's half). The alternative kills correct kernelets, because the bottom half's preemption-off region is not bounded by construction.
- **The quota parks the carriers** (register D62, kept with a new object). The alternative, refusing to enqueue, would have to live in the class scheduler rather than in OSTD.
- **A group scheduler in the host kernel is a prerequisite for proportional share**, and until it exists the property is not held on this host.
- **A8 stays**: that the host can reach its own voluntary switch from the trap-return path, for the yield stub. Nothing else in OSTD preempts a host kernel thread that computes.
