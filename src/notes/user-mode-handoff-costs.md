# User execution and CPU-state handoff problems

## Problem {#problem}

**Adaptation overhead.** The current Linux-host syscall round trip copies registers between the kernelet context and carrier `pt_regs`. It also switches between kernelet and Linux stacks. This bridge preserves OSTD's suspended `execute()` call while using Linux's entry and return paths ([current protocol](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#user-run)). Non-page-fault tenant exceptions also pass through Linux signal handling before reaching the kernelet ([exceptions](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#exceptions)).

**Safe API inputs.** Unchecked values submitted through safe user-state APIs can cause restore faults or grant excess user privilege ([safe API inputs](#caller-api)).

The common solution is developed in [Direct vOSTD memory and execution protocol](vostd-overhead-inventory.md#direct-vostd).

## Execution adaptation overhead {#current-design}

### Registers and user entry

The kernel proper calls `execute()` and expects it to return after a tenant syscall, exception, or event. Its suspended call must survive while tenant code runs. The current Linux-host design also relies on Linux's carrier entry and return paths. The adapter must preserve the kernelet call while satisfying Linux's stack and register conventions. Separate stacks and register copies are the current implementation's way to connect these contracts ([calling conventions](../blueprint/linux-mode/virtualizing-ostd/user-mode.md)).

`user_run` records the context pointer and switches to the Linux stack, leaving its call suspended on the kernelet stack. The resumed [hook](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#gate) copies the context into `pt_regs` for Linux's user-return path. On a tenant syscall, the syscall hook copies `pt_regs` back into the context. It switches to the kernelet stack so the suspended `user_run` call returns to vOSTD ([protocol](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#user-run)).

```text
tenant syscall -> Linux entry saves pt_regs
  -> syscall hook copies pt_regs into context
  -> switch to kernelet stack; execute() returns
  -> kernelet handles the call and selects the next tenant thread
  -> user_run adopts the selected mm if different
  -> switch to Linux stack; resume the hook
  -> hook copies context into pt_regs -> Linux user return
```

The syscall round trip above does not itself require a tenant FPU save/load pair. Kernelet code does not use floating-point or vector registers. A tenant-thread switch saves the outgoing thread's FPU state and loads the incoming thread's state before user execution. Linux separately preserves the carrier's FPU state across host scheduling. Linux does not track tenant-thread switches within a carrier. The adapter must coordinate these two owners ([FPU protocol](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#fpu), [state ownership](#host-state)).

### Address-space selection {#root-coordination}

Before user entry, `user_run` switches the carrier to the selected tenant address space only if it differs from the current one ([adoption](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#adopt)).

### Host entry dependencies

In the current x86-64 design, Linux configures the syscall, exception, and interrupt entry points. `SYSRET` and `IRET` return to user mode without changing those entry points. Tenant syscalls and exceptions therefore enter Linux before reaching the kernelet ([Linux v6.12 syscall initialization](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/cpu/common.c#L1904), [IDT setup](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/idt.c#L78), [entry code](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/entry_64.S#L83)).

Native OSTD assembly uses GS-relative TSS fields to save and recover the suspended kernel stack. Linux entry uses different per-CPU fields and stack conventions, with KPTI transitions where enabled ([OSTD at the book pin](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/trap/syscall.S), [Linux entry](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/entry_64.S#L83)).

### Cost and root causes {#costs}

| Mechanism | Trigger | Cost to distinguish |
|---|---|---|
| General-register handoff | Transfer between Linux hooks and the kernelet | Required register capture/restore; additional copies between `pt_regs` and the kernelet context, plus stack transfers |
| Exception delivery | A tenant exception reported to the kernelet | Required exception capture/handling; Linux fault-callback or signal-queue mediation before `UserException` delivery |
| FPU state | Tenant-thread changes, signal-frame save/restore, or host carrier scheduling | Required save/restore; staging-buffer zeroing/copies and restore validation in `fpu_save`/`fpu_load`; carrier ownership coordination |
| FS/GS bases | Each departure to tenant user mode | Required base-register state; updates to Linux's saved fields and hardware bases under preemption control |
| Linux `mm` adoption | The selected tenant space differs from the carrier's current space before user entry | Reference changes, Linux bookkeeping, root switching, and resulting TLB behavior |

A private snapshot can retain malformed state. Validation and restore-fault recovery therefore require separate analysis from copy costs ([state-input paths](#caller-api)).

## Safe API state hazards {#safety}

The kernel proper is trusted for memory safety, but may contain logic bugs ([Asterinas](../blueprint/asterinas-mode/principles.md), [Linux](../blueprint/linux-mode/principles.md)). Such code can submit invalid CPU state through safe APIs. Trusted vOSTD and host code must prevent excess privilege, disclosure of host or other-sandbox state, and host failure. Failures confined to the calling sandbox are permitted.

### Safe API inputs {#caller-api}

Each example finishes mutation before restoration and requires no caller-side `unsafe`. [Valid Rust integers and bytes](https://doc.rust-lang.org/reference/behavior-considered-undefined.html#invalid-values) can still encode invalid CPU state or excess privilege.

#### FPU: kernel-mode restore fault {#fpu-state-api}

The safe [FPU API](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/cpu/context/mod.rs#L526-L585) permits:

```rust
let mut image = FpuContext::new();
image.as_bytes_mut()[27] |= 0x80; // Set reserved MXCSR bit 31.
image.load();
```

Bytes 24–27 hold `MXCSR` ([layout](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/cpu/context/mod.rs#L651-L670)). When `XRSTOR` or `FXRSTOR` loads `MXCSR`, setting reserved bit 31 causes `#GP` ([XRSTOR rules](https://cdrdv2-public.intel.com/843855/334569-sdm-vol-2d-dec-24.pdf), [FXRSTOR rules](https://cdrdv2-public.intel.com/812383/253666-sdm-vol-2a.pdf)). These instructions execute at CPL = 0 on this path, so the exception enters kernel exception handling. Faults raised by tenant instructions at CPL = 3 follow the user-exception path.

During signal delivery, the kernel proper [copies saved FPU state into a user-space signal frame](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/process/signal/mod.rs#L364-L418). The application can set `MXCSR` bit 31 in that frame. [`rt_sigreturn`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/syscall/rt_sigreturn.rs#L35-L48) copies the bytes into a private `FpuContext` without checking that bit. [`CpuSync::set()`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/process/posix_thread/cpu_sync.rs#L149-L154) stores the context, which the [next user-entry hook](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/process/posix_thread/cpu_sync.rs#L173-L185) restores through [`load()`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/process/posix_thread/cpu_sync.rs#L240-L248). The private copy retains the invalid value; no concurrent writer is required.

The [Asterinas-host design](../blueprint/asterinas-mode/virtualizing-ostd/user-mode.md) keeps native FPU restoration. If the host retains the [baseline exception handler](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/trap/mod.rs#L187-L192), this `#GP` triggers a host panic. The specified [page-fault recovery](../blueprint/asterinas-mode/faults-and-reclamation.md) does not cover `#GP`. The Blueprint does not specify recovery for this restore fault.

On its native x86-64 signal-return path, Linux can [restore FPU state directly from user memory](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/fpu/signal.c). The restore instruction has an [exception-table entry](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/fpu/xstate.h); failure handling accounts for partially changed registers. A failed signal-frame restore causes `SIGSEGV` ([signal return](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/signal_64.c), [signal error handling](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/signal.c)). This path shows that fault containment does not require a full FPU-state copy.

#### FS/GS: kernel-mode base-write fault {#base-state-api}

The safe [base APIs](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/cpu/context/mod.rs#L71-L136) permit:

```rust
fn invalid_fs() {
    FsBase::new(1usize << 63).load();
}

fn invalid_gs() {
    let guard = ostd::irq::disable_local();
    GsBase::new(1usize << 63).load(&guard);
}
```

`1usize << 63` is noncanonical under both 48-bit and 57-bit addressing. The [safe constructors](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/cpu/context/mod.rs#L71-L136) accept it without validation. With FSGSBASE enabled, native `WRFSBASE` and `WRGSBASE` reject this value with `#GP`. Writing the same value to `IA32_KERNEL_GS_BASE` with `WRMSR` also causes `#GP` ([Intel instruction rules](https://cdrdv2-public.intel.com/843855/334569-sdm-vol-2d-dec-24.pdf)). These writes execute at CPL = 0 on the paths discussed here.

Assuming valid remaining arguments and successful child creation, a tenant can submit this value through `clone(CLONE_SETTLS)` on x86-64. The kernel proper [stores this TLS argument as the child's FS base without address validation](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/process/clone.rs#L704-L718). The child's [user-entry hook](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/process/posix_thread/thread_local.rs#L290-L296) later loads it. [`arch_prctl(ARCH_SET_FS)`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/syscall/arch_prctl.rs#L42-L46) rejects this value, but the clone path does not use that check. The GS example shows safe API exposure; no tenant-syscall trigger is established here.

#### General registers: excess user I/O privilege {#general-state-api}

Assume `mode` belongs to the current task and its authorized address space is active. User code and stack are mapped, and entry hooks prepare FS/GS and FPU state:

```rust
let flags = 2usize | (1usize << 9) | (1usize << 21) | (3usize << 12);
{
    let regs = mode.context_mut().general_regs_mut();
    regs.rip = user_entry_ip; // Canonical, mapped user code.
    regs.rcx = user_entry_ip;
    regs.rsp = user_stack_sp; // Mapped user stack.
    regs.rflags = flags;     // Fixed bit 1, IF, ID, and IOPL = 3.
    regs.r11 = flags;
}
let reason = mode.execute(&hooks);
```

The [safe accessors](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/user.rs#L64-L104) expose [writable integer register fields](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/cpu/context/mod.rs#L289-L297). Native [`execute()`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/cpu/context/mod.rs#L305-L309) sets IF and ID without clearing IOPL. The [return assembly](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/trap/syscall.S#L67-L99) selects `SYSRET` when RIP is canonical, RCX equals RIP, R11 equals RFLAGS, and TF/RF are clear. `SYSRET` loads RFLAGS from R11 with a mask that preserves IOPL ([Intel SYSRET](https://www.intel.com/content/dam/www/public/us/en/documents/manuals/64-ia-32-architectures-software-developer-vol-2b-manual.pdf)).

Without a trusted IOPL filter, this return path gives user code IOPL = 3. At CPL = 3, port I/O then bypasses the TSS I/O-permission bitmap ([I/O protection](https://cdrdv2-public.intel.com/843827/253665-sdm-vol-1-dec-24.pdf)). Tenant code can issue port I/O outside host device mediation without an I/O-permission fault.

### State restoration in each host {#host-state}

| State | Asterinas host design | Linux host design |
|---|---|---|
| General registers | vOSTD and host entry/return assembly use the same task-stack [`RawUserContext`](../blueprint/asterinas-mode/virtualizing-ostd/user-mode.md). No IOPL filter is specified. | Hooks copy between the kernelet context and carrier `pt_regs`. The [return hook](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#user-run) filters flags, including IOPL, and fixes user-segment selectors. |
| FPU | Native per-thread [`FpuContext`](../blueprint/asterinas-mode/virtualizing-ostd/README.md) saves and restores tenant-thread state. The [endovisor separately preserves live carrier FPU state](../blueprint/asterinas-mode/virtualizing-ostd/tasks.md#decides) across host scheduling. Image validation and restore-fault recovery are unspecified. | [`fpu_load`](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#fpu) copies into private staging, validates, then restores with instruction recovery and ownership repair. `fpu_save` saves into cleared staging and copies out. |
| FS/GS | FS uses native register access. User GS uses [`IA32_KERNEL_GS_BASE`](../blueprint/asterinas-mode/virtualizing-ostd/README.md), leaving active host GS unchanged. | The endovisor [updates saved carrier fields and hardware bases](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#fpu) with preemption disabled. When FSGSBASE is enabled, capture of user-written bases into tenant-thread state is unspecified. |

Neither host design specifies rejection of invalid FS/GS bases or recovery from base-write faults.

Linux's FPU staging snapshot prevents later source writes from changing checked input; validation separately rejects malformed values. These mechanisms address different failure conditions.

### Changes after validation {#mutation-after-submission}

[`FpuContext`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/cpu/context/mod.rs#L526-L585) stores its restore image in a private heap allocation. Safe mapping APIs cannot expose its typed heap frames to tenant user space ([frame conversion](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/mm/frame/mod.rs#L334-L348), [mapping API](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/mm/vm_space.rs#L358-L370)). The `load(&self)` borrow also excludes concurrent mutation through `as_bytes_mut()`. This path establishes no permitted writer that could change the image after validation.

## Evidence and open questions {#validation}

### Adaptation-cost evidence {#cost-evidence}

An earlier booted Linux experiment measured a constant-return hook at 39 ns and `getppid` at 44 ns. These are medians of five runs in a guest without mitigations; the reported difference is below measurement noise. The experiment did not measure the current stack bridge, complete handoff, or direct-transition savings ([earlier experiment](../blueprint/linux-mode/prototype.md#earlier), [current handoff](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#user-run)).

Comparing a kernelet syscall with a native Linux syscall changes both the kernel implementation and the handoff path. Existing measurements do not isolate register copies, stack transfers, or carrier-state coordination.

### State-restoration evidence {#state-evidence}

The safe API examples trace source paths and conditional hardware behavior. This analysis did not execute these examples.

The booted Linux prototype reports 1,037,002 FPU save/load pairs without lost values. This exercises its staging and ownership path, not isolated latency. Validator rejection and faulting-restore cleanup were not exercised ([FPU report](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#fpu)).
