# Kernelet stack safety and conditional service switching

## Problem {#problem}

The [Linux Blueprint](../blueprint/linux-mode/kernelet-api-service.md#depth) switches from the kernelet stack to a host stack for each service call. It switches back when the service returns. For short services, these repeated transitions add work around a small service body. Conditional switching would let a service use the current stack when enough space remains.

The service must also leave room for interrupt and exception entry while it runs. Hardware saves the interrupted context before software can switch stacks. A service-entry check can cover these writes during the service. Kernelet code can still exhaust its stack outside service calls, so entry also needs an overflow path.

The existing [Linux](../blueprint/linux-mode/faults-and-reclamation.md#stack) and [Asterinas](../blueprint/asterinas-mode/virtualizing-ostd/tasks.md#stacks) designs preserve entry space through function-entry checks. This proposal combines service-budget checks with guard pages and independent exception entry to contain overflow during kernelet execution.

## Current design {#baseline}

A **carrier host stack** is the native stack of the host task that runs a kernelet virtual CPU. It is separate from the kernelet task's stack. A **per-CPU interrupt stack** holds IRQ-handler frames while that CPU handles an interrupt. The host must finish using those frames before another carrier reuses that stack. The interrupted task's saved return context can remain on its kernelet stack.

The interrupt descriptor table (IDT) selects interrupt and exception entry points. The interrupt stack table (IST) supplies independent stack pointers for designated events.

| Host | Service execution | Stack-overflow protection |
|---|---|---|
| Linux | Each service call switches to a host stack. | Compiler-inserted function-entry checks preserve space for host entry; stock double-fault handling is fatal to the machine. |
| Asterinas | Ordinary OSTD services use the kernelet stack after a reserve check. Endovisor hooks switch to a separate host stack. | Function-entry checks are the primary protection. The design also requires an independent double-fault IST stack as a backstop. |

These are Blueprint requirements: [Linux services](../blueprint/linux-mode/kernelet-api-service.md#depth), [Linux overflow](../blueprint/linux-mode/faults-and-reclamation.md#stack), [Asterinas services](../blueprint/asterinas-mode/kernelet-api-service.md), and [Asterinas stacks](../blueprint/asterinas-mode/virtualizing-ostd/tasks.md#stacks). Asterinas already avoids stack switching for ordinary OSTD services. For those services, this proposal changes budget admission rather than removing a stack switch.

### Linux interrupt entry {#linux-interrupt-stacks}

For ordinary device interrupts (IRQs), the [Linux Blueprint](../blueprint/linux-mode/virtualizing-ostd/tasks.md#stacks) describes an initial frame save on the kernelet stack. Software later switches to the per-CPU interrupt stack. The [reserved space](../blueprint/linux-mode/faults-and-reclamation.md#stack) protects these initial writes and other host work that still uses the interrupted stack.

Using IST for ordinary IRQs could avoid the initial writes on the kernelet stack described above. Hardware would switch to an independent stack before saving the interrupt frame. However, assigning ordinary IRQs a single fixed IST stack top introduces a different risk. If that stack still holds unfinished interrupt work, another IRQ using the same top can overwrite the existing frames. Linux's [x86 stack documentation](https://www.kernel.org/doc/html/v6.12/arch/x86/kernel-stacks.html) identifies nesting races as the reason for switching ordinary IRQ stacks in software.

Linux's [software switch](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/irq_stack.h#L132) checks whether the per-CPU interrupt stack is already in use. If it is, entry keeps the current stack pointer, so new frames are added below the existing frames. An IST design would need separate protection for unfinished frames during nested entry. This note retains software switching for ordinary IRQs. It therefore still needs the preceding entry-space protection or the proposed overflow exit path.

## Proposed approach {#proposal}

Service entry chooses a stack using a budget computed for the host build. Ordinary IRQs use software switching to the per-CPU interrupt stack. Supported non-IST exceptions use the carrier host stack. If stack exhaustion prevents entry while kernelet code runs, an independent exception stack allows the host to start sandbox termination.

### Check the complete service budget {#service-execution}

For each eligible service, establish two upper bounds for the final host build:

- **Service stack use:** the maximum additional space used below the checked stack pointer from the entry check through service return. This includes the wrapper and functions called by the service.
- **Interrupt-entry stack use:** the maximum additional space used before interrupt or exception entry switches away from the kernelet stack. This includes hardware frames, alignment, assembly register saves and any permitted nested entry. Handler frames on separate stacks do not count.

Use their sum as the required space at service entry:

```text
required_space = service_stack_use + interrupt_entry_stack_use
```

Checking only service stack use could admit a call that reaches its peak with no free stack space left. If an IRQ arrives there, saving its frame would overflow before software can switch stacks. The interrupted service may already hold a host lock that must be released. Double-fault entry does not reliably resume that service to release the lock ([Intel SDM, double fault](https://cdrdv2-public.intel.com/825758/253668-sdm-vol-3a.pdf#page=224)). With sound bounds, the combined threshold leaves enough space for both service execution and interrupt entry.

At runtime, an assembly entry stub reads the current stack pointer into `check_sp`. The host finds the allocated kernelet stack containing that address in its own stack-pool records. The record supplies `stack_bottom`, the lowest usable address above the guard pages. An invalid caller or stack takes the host-controlled exit path without executing the service.

For a valid stack, the stub reads this service's generated budget and computes:

```text
remaining_space = check_sp - stack_bottom
```

If the budget is known and `remaining_space >= required_space`, the service runs on the kernelet stack. Otherwise, the stub switches to a host stack, calls the service and restores the kernelet stack after return. Both cases execute the existing caller, permission and termination checks before the service body. The [implementation plan](#service-entry) specifies the stack lookup, budget table and assembly stub.

### Switch stacks before interrupt and exception handling {#software-interrupt-entry}

The service budget protects interrupt and exception entry while a host service runs on the kernelet stack. During kernelet execution outside services, no such check guarantees free space. Both entry paths must support successful entry and safe failure before host handling begins.

Retain the [software IRQ-stack switch](#linux-interrupt-stacks), but move it before host bookkeeping and handler calls. A bounded assembly prefix saves the required registers and switches directly to the per-CPU interrupt stack. Before switching, it acquires no host locks or owning references and changes no host bookkeeping state. If entry fails over kernelet execution, recovery may abandon this prefix under the [recovery conditions](#safety).

Apply the same early handoff to every supported non-IST exception on a kernelet stack. Use the carrier host stack before any C handler, bookkeeping or diagnostics, whether or not the handler can sleep. Saving the initial frame may succeed even when later handler calls would overflow. Once host bookkeeping has begun, that overflow cannot safely discard the handler.

After a successful switch, the host handles the event normally, except when an eligible guard fault selects overflow recovery. The saved return context stays on the kernelet stack, which remains allocated until that context's last use. The [host-entry changes](#early-host-handoff) cover both entry and return.

### Contain overflow with guards and independent exception entry {#guarded-overflow}

Guard pages provide the failure path when ordinary entry cannot finish. A stack write into a guard causes a page fault. If page-fault entry reaches a safe host stack, it selects sandbox termination before normal fault bookkeeping. If saving the page-fault frame causes another page fault, hardware delivers double fault.

Before running kernelets, the host configures a separate double-fault IST stack on each CPU. Hardware selects that stack before saving the double-fault frame ([Intel SDM, IST](https://cdrdv2-public.intel.com/825758/253668-sdm-vol-3a.pdf#page=212), section 6.14.5). Recovery can then execute without using the exhausted kernelet stack. The next section explains how it leaves the failed execution and returns control to the host.

### Exit the sandbox from preserved host state {#overflow-recovery}

The double-fault stack provides an emergency entry, not a return location for normal host execution. Before running kernelet code, the carrier saves a host stack position, resume address and required registers. This **host continuation** gives recovery a place to run cleanup without using failed kernelet frames. The saved double-fault instruction pointer cannot reliably resume the interrupted computation ([Intel SDM, double fault](https://cdrdv2-public.intel.com/825758/253668-sdm-vol-3a.pdf#page=224)).

Recovery identifies the carrier and execution phase from host-owned records, then claims recovery once and marks the sandbox dying. It transfers to the preserved host continuation and completes any IRQ accepted before entry failed. The carrier then follows normal host termination. Sandbox memory remains allocated until all carriers and device accesses have stopped.


### Expected cost {#cost}

Each service entry looks up caller and stack records, reads the budget and selects a route. Compare this work with the caller and reserve checks it replaces.

| Path | Proposed change |
|---|---|
| Eligible call whose baseline switches stacks | With sufficient space, avoid switching to a host stack and back. |
| Service with an unknown budget or insufficient space | Use the host-stack route. |
| Ordinary kernelet function | Remove checks used solely to preserve interrupt-entry space after the replacement is validated; retain required probes. |
| Kernelet task stack switches and saved-context lifetime | Record and release stack uses so the allocator cannot reuse a live stack. |
| Ordinary IRQ on a kernelet stack | Switch directly to the per-CPU interrupt stack before bookkeeping; specialize return and use the carrier host stack when scheduling is needed. |
| Supported non-IST exception on a kernelet stack | Transfer to the carrier host stack before host bookkeeping or diagnostics. |
| Host invocation of kernelet code and overflow | Prepare a continuation per invocation; use independent recovery only on failure. |

Asterinas ordinary OSTD services already use the kernelet stack. Their fallback can add a switch to the host stack and back. Existing reserve failure terminates the sandbox, so successful fallback also changes behavior.

Linux already switches ordinary IRQs to its per-CPU stack. Count changes to entry order and return handling without counting that existing switch as added work. Asterinas adds the ordinary IRQ-stack switch.

Memory costs include the budget table and newly allocated or enlarged stacks, allocation-use records and recovery records. **[unverified]**: The net overhead and speedup have not been measured.

## Implementation plan {#implementation-plan}

### Generate service budgets {#analysis}

Generate one budget per Service Table entry from the optimized, linked host build. Initially admit bounded service bodies that cannot sleep or schedule. Wrapper branches that park or terminate the carrier must first transfer to its host stack. Each table entry contains `required_space` in bytes or `HOST_STACK`, which selects the host-stack route.

The build tool performs these steps:

1. **Collect function-frame data.** Retain Rust's `.stack_sizes` from [`-Z emit-stack-sizes`](https://doc.rust-lang.org/nightly/unstable-book/compiler-flags/emit-stack-sizes.html), or GCC's [`-fstack-usage` and `-fcallgraph-info=su`](https://gcc.gnu.org/onlinedocs/gcc-14.1.0/gcc/Developer-Options.html) outputs. With LTO, collect link-stage outputs. These are inputs to analysis, not complete service bounds.
2. **Resolve the complete service path.** From `check_sp`, check linked control flow, stack accesses and pointer adjustments. Include assembly, wrappers, compiler helpers, error paths, cleanup and supported unwinding through return or a nonreturning host-stack handoff. Derive assembly bounds from instructions. Unknown bounds or control-flow targets, recursion and unbounded growth require `HOST_STACK`; missing metadata never means zero use.
3. **Compute peak simultaneous use.** At each call, combine the caller's live stack use with the callee's bound. Count ABI arguments, alignment and return addresses once. Take the maximum across paths; sequential calls do not accumulate after return. Account for tail-call adjustments. Express the bound below `check_sp`.
4. **Add the interrupt-entry bound.** Cover every supported interrupt and exception prefix until its safe-stack handoff. Include hardware frames, assembly saves, permitted nesting and any return-path writes to the kernelet stack. A common bound can cover shared entry paths. Reject arithmetic overflow when adding the bounds.
5. **Fill the budget table.** Reserve fixed-size storage initialized to `HOST_STACK`, defined and read from assembly to prevent constant folding. Fill it after analysis using [`llvm-objcopy --update-section`](https://llvm.org/docs/CommandGuide/llvm-objcopy.html#cmdoption-llvm-objcopy-update-section). Verify unchanged code bytes, addresses and section sizes; package without relinking. Keep the table read-only at runtime.

Disable the red zone, the ABI's temporary space below `RSP`, in kernelet and admitted host code ([Rust](https://doc.rust-lang.org/rustc/codegen-options/index.html#no-redzone), [GCC](https://gcc.gnu.org/onlinedocs/gcc-14.1.0/gcc/x86-Options.html)). Hardware interrupt frames can otherwise overwrite that data.

Run analysis at host build time; runtime calls read the generated results. Cover all permitted code variants. Disable affected admission before enabling unanalyzed tracing, patches or configuration changes.

### Implement the service entry {#service-entry}

Give each Service Table entry an assembly stub with a fixed service body and budget index. Capture `check_sp` before the stub adjusts the stack, preserving ABI arguments and establishing trusted host CPU-local access. The caller's return-address push precedes this capture and belongs to the pre-admission overflow window. Perform lookup and comparison without host locks or owning references.

Use the caller and stack records specified by each host's Blueprint:

| Host | Caller and stack lookup |
|---|---|
| Linux | The current host task's [gate pointer](../blueprint/linux-mode/kernelet-api-service.md#depth) identifies the carrier and sandbox. Find `check_sp` in that sandbox's registered [stack-pool ranges](../blueprint/linux-mode/virtualizing-ostd/scheduling.md#upcall). |
| Asterinas | The host's [per-CPU carrier state](../blueprint/asterinas-mode/kernelet-api-service.md) identifies the sandbox. Round `check_sp` down to its aligned stack slot, then verify membership in the host-recorded pool. |

Validate the caller and allocated stack; require `stack_bottom <= check_sp < stack_top` before subtracting the bounds. Keep lookup records mapped and stable. Use host-owned bounds rather than vOSTD's shared `stack_limit`.

Prevent allocation reuse throughout active execution and saved host return contexts, including intervals before service entry. Retaining the pool's mapping alone does not provide this protection. The allocator and trusted stack-switch paths need a coordinated lifetime protocol.

Replace the Blueprint's stack-selection and fixed service-reserve logic with the stub. Retain caller, permission, state and termination checks. Record that the carrier is executing a host service before the wrapper acquires resources. Nested calls and interrupts must preserve the outer state. Restore that state only after cleanup and ordinary wrapper frames finish, in a final assembly tail holding no host resources. The host-stack route retains the service's scheduling requirements.

The [Asterinas Blueprint's wrapper](../blueprint/asterinas-mode/kernelet-api-service.md) acquires a reference before its reserve check, so admission must precede that acquisition. A pre-admission fault uses the [overflow path](#guarded-overflow) only if the interrupted execution is discardable. A completed check that finds insufficient space selects the host stack.

### Change interrupt and exception entry and return {#early-host-handoff}

For ordinary IRQs on kernelet stacks, establish trusted host CPU-local access and mappings, then switch before host bookkeeping. Preserve the original frame pointer, ABI alignment and unwind metadata. The following sources describe current host entry code; the changes remain proposals.

| Host | Required entry change |
|---|---|
| Linux v6.12 | Switch before optional [`CALL_DEPTH_ACCOUNT`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/entry_64.S#L1057) and [`irqentry_enter()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/idtentry.h#L212). Adapt [`idtentry_body`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/entry_64.S#L288) to pass the preserved `pt_regs` pointer instead of deriving it from the new `RSP`. |
| Asterinas upstream at `ab9a4cfdc` | This [kernel trap entry](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/trap/trap.S#L104-L154) has no ordinary IRQ-stack handoff. Add one before `trap_handler`, ahead of [interrupt-level accounting](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/irq/level.rs#L83-L97). Preserve existing handler and acknowledgment ordering. |

For each supported non-IST exception on a kernelet stack, switch to the carrier host stack before any C call, bookkeeping or diagnostics. Use a host-recorded landing pointer below live host frames, with room for the handler and supported nesting. Preserve the original exception-frame pointer and outer execution phase.

On Linux, use the same boundary before `CALL_DEPTH_ACCOUNT` and [`idtentry_body`'s C call](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/entry_64.S#L311). Cover specialized entries, including [invalid opcode (`#UD`)](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/traps.c#L300) and [page fault (`#PF`)](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/fault.c#L1493). Their C code does work before `irqentry_enter()`. Asterinas must select the IRQ or exception stack before the shared `trap_handler` call cited above.

A matching guard fault from discardable execution selects recovery before ordinary exception accounting. Otherwise, record protected host exception execution before bookkeeping or diagnostics. Restore the outer phase only after balanced exit and ordinary host frames finish. An exception does not make an interrupted host service discardable. Keep existing reserve checks for exception paths without this handoff.

Nested IRQs already on the per-CPU stack retain its current pointer. Only the entry that claimed the stack clears occupancy. Clear it after leaving that stack, before faultable return work on the kernelet stack or another carrier can run. Each entry restores its own interrupted pointer.

For direct IRQ or exception return, finish bookkeeping on a safe stack, then restore the kernelet context through a bounded, IRQ-masked assembly tail. For scheduling after an IRQ, finish the per-CPU call chain and transfer explicit return state to the carrier stack below live host frames. No per-CPU frames may remain in use when scheduling starts.

Preserve Linux's [`irqentry_exit()` ordering](https://elixir.bootlin.com/linux/v6.12/source/kernel/entry/common.c#L328): rescheduling can precede final tracing and lockdep exit. Its [rescheduling helper](https://elixir.bootlin.com/linux/v6.12/source/kernel/entry/common.c#L303) expects a task stack, so provide a matching exit path. These non-IST exception handlers already use the carrier host stack, including when they sleep.

Keep the host-controlled IDT/IST setup active during host and kernelet execution. Service crossings alone therefore require no IDT reload or IST reconfiguration. Host handlers may still update IST pointers for safe nesting.

Publish a host continuation and carrier records before each host invocation of kernelet code. Preserve outer records during nesting and refresh CPU-local ownership after migration. Invalidate the continuation when its invocation ends. Recovery preserves current host mappings, task and preemption state.

After architectural entry setup, enter guard-fault or double-fault recovery before bookkeeping that the recovery jump would skip. The emergency prefix acquires no locks, allocates no memory and does not unwind failed frames. Complete accepted IRQs through balanced host entry/exit using a valid recovery context; an incomplete hardware frame is not valid `pt_regs`.

## Safety constraints and limitations {#safety}

This proposal covers x86-64 IDT/IST entry with supervisor shadow stacks disabled. It uses the book's [trust model](../blueprint/linux-mode/principles.md): the kernel proper is memory-safe; vOSTD, the host, compiler and host metadata are trusted.

**Guard coverage.** An `RSP` adjustment can skip a guard without touching memory. Retain [stack probes](https://blog.llvm.org/posts/2021-01-05-stack-clash-protection/). Require the guard gap to exceed the maximum unprobed descent plus the furthest interrupt or exception write below that position. Include alignment and permitted nesting; check the interval between each adjustment and its probe. Validate restored stack pointers against the active allocation. These local bounds do not require a bound on total recursion.

**Recoverable overflow.** Host-owned records must identify the carrier, active stack and execution phase. Fault information must match that stack's guard. A double fault alone is insufficient. Recover only from kernelet execution or unfinished entry with no host resources or bookkeeping to undo. Recovery must not discard an interrupted host service that requires cleanup. If these conditions cannot be established, retain normal host fault handling.

**IRQ completion.** A fixed-delivery IRQ can be dispatched before saving its frame fails. The local APIC's in-service register (ISR) identifies this delivery, provided no older IRQ is in service ([Intel SDM, APIC](https://cdrdv2-public.intel.com/825758/253668-sdm-vol-3a.pdf#page=414)). Enter or resume kernelet code only with an empty ISR. Keep IRQs masked and issue no end-of-interrupt (EOI) in the initial entry prefix. Capture ISR before recovery changes controller state.

Recovery must complete the identified vector's host work and acknowledgments once, in host order, before sandbox termination can block. Sending EOI alone leaves the host work undone; an empty ISR requires no EOI. Every enabled IRQ vector needs this completion path. Other delivery modes require separate attribution.

**Independent exception entry.** On every CPU running carriers, emergency code, stacks, descriptors and records must remain mapped in each permitted address space. Nested non-maskable interrupts (NMIs) and exceptions must preserve unfinished emergency frames and outer fault and IRQ records.

**Coverage before removing checks.** Remove function-entry reserve checks only when every supported path using a kernelet stack meets these conditions. This includes IRQs, non-IST exceptions, upcalls and return tails. Keep existing checks where coverage remains incomplete.
