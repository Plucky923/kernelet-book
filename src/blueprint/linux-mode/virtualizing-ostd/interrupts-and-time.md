# Interrupts and time

*How a kernel that owns no interrupt controller receives interrupts, and how it tells the time. It discharges the rule that no kernelet code ever runs in Linux's interrupt context, which is what lets a carrier be stopped wherever it stands.*

## A kernelet has no interrupts, only virtual ones

Linux owns the interrupt controller, the vector table and every physical line. Nothing in vOSTD can register a hardware handler, mask a hardware interrupt or send one to another processor; OSTD's interfaces for those are absent from the kernelet build, so a use of one does not compile.

What a kernelet has instead are **virtual interrupts**. Each [virtual CPU](tasks.md#vcpus) has a record shared with the endovisor, and in it a `pending` word. The endovisor sets a bit; the kernelet's handler for that bit runs on that virtual CPU soon after, in *task context* on the virtual CPU's carrier, never in Linux's interrupt context. How "soon after" is arranged, including the case where the virtual CPU is in the middle of kernel code, is the **upcall**, specified on the [Scheduling](scheduling.md#upcall) page because the scheduler is its most demanding user. This page says what the bits are and what stands behind them.

| bit | meaning | set by |
|---|---|---|
| TICK | a millisecond of execution has passed on this virtual CPU | the [watch timer](tasks.md#watch), while the carrier is on a processor |
| KICK | another virtual CPU wants this one to look at its run queue | the service `vcpu_kick` |
| LINES | at least one device line is pending; which ones is in the record's `pending_lines` words, one bit for each of lines 32 to 255 | a device model, through `raise_irq`, which sets the line's bit first and LINES second |

Every test for "is anything pending" in this chapter is a test of the one `pending` word, which is why device lines have a summary bit in it. The reader's order is the writer's reversed: vOSTD swaps `pending` to zero first and then swaps out each `pending_lines` word, so a line set after the summary was taken is found by the next summary.

The kernel proper's interrupt handlers, bottom halves and timer callbacks keep their code. `IrqLine` is OSTD's type with its callback lists; what is gone is everything beneath it: the mapping to a hardware vector and the acknowledgment. A handler runs inside the upcall with the virtual CPU's `irq_off` set, so it is not interrupted by another on the same virtual CPU, which is the assumption such handlers make on a machine. Work that a machine would defer to a bottom half is deferred the same way, to OSTD's own mechanism, which runs when the handler returns.

**Raising a line.** A device model in the endovisor calls `raise_irq(line)`. Each device line is bound for the kernelet's life to one virtual CPU. Raising it is two atomic bit-sets in that virtual CPU's record (the line's bit, then LINES) and a kick, all legal from any Linux context, including a hardware interrupt handler. Delivery is edge-triggered: a line raised twice before its handler runs is handled once, as with a real interrupt line.

**An idle virtual CPU.** When the kernelet's scheduler has nothing else to run on a virtual CPU, it runs the kernel proper's idle task, which calls OSTD's `halt_cpu()`, the function that on a machine halts the processor until the next interrupt. In vOSTD that is the service `vcpu_idle(deadline)`: the carrier sleeps in Linux, killably, until a bit is pending or the deadline passes. A kick or a raised line wakes it with [`wake_up_process()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/sched/core.c#L4412). The order on the two sides is the one every Linux sleep uses, so that no wake-up is lost: the sleeper sets its task state to *sleeping, killable, freezable*, marks the virtual CPU idle, *then* tests `pending`, and only then calls `schedule_hrtimeout()`; the waker sets the bit, *then* tests the idle mark and wakes the task.

## Time

**Reading the clock** needs no crossing. The endovisor publishes the processor's timestamp-counter frequency in the kernelet's boot arguments, and keeps one **clock page**, shared read-only by every kernelet on the machine, holding a coarse tick count and the monotonic time in nanoseconds. One Linux high-resolution timer updates it at the kernel proper's tick rate, machine-wide, not per kernelet. vOSTD's `Jiffies` and its clock sources read the page and the counter.

**The tick.** OSTD's whole timer interface is a periodic tick (`TIMER_FREQ`, 1000 Hz) with per-CPU callbacks, and the kernel proper drives its timer wheel and its scheduler from those callbacks. While a virtual CPU is running, in kernel or user mode, the [watch timer](tasks.md#watch) of the processor it is on sets TICK every millisecond, and vOSTD's handler for the bit runs the callbacks. A virtual CPU that Linux has taken off the processor gets no ticks until it is back. That is harmless because nothing in a kernelet keeps time by counting ticks: vOSTD's `Jiffies` is a read of the clock page, the kernel proper's timer wheel compares its expiries with `Jiffies`, and its scheduler accounts run time from the timestamp counter (*measured on the tree*: its `sched_clock` is `read_tsc`). The design depends on that. One consequence is the one every guest kernel knows: time that Linux takes from a virtual CPU is charged, inside the kernelet, to whichever task was current, because the timestamp counter does not stop. The virtual CPU's record carries the total (`stolen_ns`, which the endovisor advances each time it puts the carrier back on a processor), for a kernel proper that wants to subtract it; OSTD has no interface that does so today.

**An idle virtual CPU's deadline.** An idle virtual CPU gets no ticks, yet its timer wheel may hold a tenant's `nanosleep`. Something must wake it in time, and OSTD has no interface that says when: `halt_cpu()` takes no argument, because on a machine the tick keeps coming. The design asks OSTD for one small addition, its third prerequisite ([Scheduling](scheduling.md#asks)): the owner of a timer wheel may register a function that returns the wheel's earliest expiry on this CPU, and `halt_cpu()` consults it. vOSTD passes the answer to `vcpu_idle` as the deadline, and when the carrier wakes because the deadline passed, vOSTD runs the tick callbacks as if a tick had arrived. If no such function is registered, vOSTD idles only until the next tick, which is correct and costs a wake-up per millisecond per idle virtual CPU. With it, an idle kernelet with no timer pending costs Linux nothing at all, which matters when a machine holds thousands of them.

A sleep with a deadline is a Linux timer, and time spent in a timer interrupt is charged to whatever it interrupts, not to the sandbox; so a kernelet must not be able to turn its charged processor time into a storm of uncharged interrupts by idling with deadlines a nanosecond away. The endovisor rounds every deadline up to at least 50 µs from now, which is the slack Linux applies to an ordinary process's timers by default. A kernelet can then cause no more timer interrupts per virtual CPU than a Linux thread could with `nanosleep`.

A *running* virtual CPU has the tick's resolution for its timers, 1 ms, which is what OSTD gives the kernel proper on a machine.

## What a tenant sees

Timers and sleeps behave as on the other host, at the resolution of Linux's high-resolution timers. Interrupt latency for a virtual device is the latency of waking or flagging a Linux task, a few microseconds (*estimated*), plus, when the virtual CPU has its virtual interrupts off, the length of that section. `/proc/interrupts` inside the sandbox shows only virtual lines.

## Costs

- **Per virtual interrupt**: one atomic operation, and a wakeup or a flag if the virtual CPU is idle or in user mode.
- **Per idle period with a deadline**: one Linux timer, armed and cancelled inside the sleep.
- **Per millisecond that a virtual CPU runs**: one watch-timer interrupt.
- **Per machine**: one timer updating the clock page.

## What this page decides

- **No kernelet code runs in Linux's interrupt context; interrupts are bits in a virtual CPU's record, handled by an upcall on that virtual CPU's carrier** ([register D117](../../../notes/design-register.md#decisions), which replaces the per-virtual-CPU worker tasks and job queue of D11 on this host). The alternative, calling a kernelet's handler from Linux's timer or device interrupt, would run tenant-driven code where it cannot be stopped, cannot sleep and cannot be charged.
- **An idle kernelet arms no timer, provided OSTD can say when its next timer expires** ([register D101](../../../notes/design-register.md#decisions), kept, and D122, the OSTD addition it needs). The alternative, a fixed tick per kernelet, makes the idle cost of a machine proportional to its sandbox count; it is also what vOSTD falls back to when the kernel proper registers no next-expiry function.
