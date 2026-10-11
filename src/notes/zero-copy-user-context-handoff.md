# Zero-copy user-context handoff

## Problem {#problem}

OSTD's user-state APIs were designed for one kernel on one machine ([C1](../blueprint/overview/challenges.md)). Kernelets add two scheduling boundaries: the host switches carriers, and the kernel proper switches tenant threads.

Both host threat models permit logic bugs in the memory-safe kernel proper: [Asterinas](../blueprint/asterinas-mode/principles.md), [Linux](../blueprint/linux-mode/principles.md). Such code can submit invalid CPU state through safe APIs. Trusted vOSTD and host code must prevent excess privilege, foreign-state disclosure and host failure. Damage confined to the calling sandbox is permitted.

### Safe API inputs {#caller-api}

These examples use OSTD at `ab9a4cfdc`. Each finishes mutation before restoration and requires no caller-side `unsafe`. Rust permits the integer and byte values; its [validity rules](https://doc.rust-lang.org/reference/behavior-considered-undefined.html#invalid-values) do not enforce CPU encodings.

#### FPU: kernel-mode restore fault {#fpu-state-api}

The safe [FPU API](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/cpu/context/mod.rs#L526-L585) permits:

```rust
let mut image = FpuContext::new();
image.as_bytes_mut()[27] |= 0x80; // Set reserved MXCSR bit 31.
image.load();
```

Bytes 24–27 hold `MXCSR`. With SSE enabled and selected for restoration, reserved bit 31 makes `XRSTOR` or `FXRSTOR` raise `#GP` ([layout](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/cpu/context/mod.rs#L651-L670), [Intel restore rules](https://cdrdv2-public.intel.com/843855/334569-sdm-vol-2d-dec-24.pdf)). Restoration runs at CPL = 0, so this is a kernel-mode exception. An application instruction fault at CPL = 3 instead follows the host's user-exception path.

Tenant input can reach the same instruction. The kernel proper [places a saved FPU image in the user signal frame](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/process/signal/mod.rs#L364-L418). The application changes it; [`rt_sigreturn`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/syscall/rt_sigreturn.rs#L35-L48) copies it into a private `FpuContext` without checking that bit. `CpuSync::set()` takes ownership, and the [next user-entry hook](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/process/posix_thread/cpu_sync.rs#L173-L185) calls `load()`. Private storage preserves the malformed value; this path requires no concurrent writer.

Asterinas retains unchecked native restoration. If the host also retains the baseline's [unhandled kernel-`#GP` branch](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/trap/mod.rs#L187-L192), the fault panics the host. Its specified [page-fault recovery](../blueprint/asterinas-mode/faults-and-reclamation.md) does not cover this `#GP`. Receiving the exception alone does not break isolation; failure to contain it can.

Native Linux can restore directly from user signal memory. Its [restore-site fixup and cleanup](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/fpu/signal.c) handle faults and partially changed FPU state; a bad signal frame results in `SIGSEGV`. Thus, fault containment does not inherently require copying the image.

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

The constructors accept this integer, but it is noncanonical under both 48-bit and 57-bit addressing. With FSGSBASE enabled, `WRFSBASE` raises `#GP`; writing inactive user GS through its MSR also raises `#GP`. Both instructions execute at CPL = 0 ([Intel instruction rules](https://cdrdv2-public.intel.com/843855/334569-sdm-vol-2d-dec-24.pdf)).

For FS, a tenant can submit the value through `clone(CLONE_SETTLS)`. The pinned kernel proper [stores it without address validation](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/process/clone.rs#L704-L718), and the child's entry hook loads it. Assume valid remaining arguments and successful child creation. [`arch_prctl`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/syscall/arch_prctl.rs#L34-L46) rejects this address; the clone path does not use that check. The GS example establishes a permitted safe API call without establishing a tenant-syscall trigger.

#### General registers: excess user I/O privilege {#general-state-api}

Assume an initialized `UserMode`, an authorized address space, mapped user code and stack, and normal preparation hooks:

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

The [safe accessors](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/user.rs#L64-L104) expose public integer fields. Native execution sets IF and ID without clearing IOPL. For canonical RIP and matching RCX/RIP and R11/RFLAGS, with TF/RF clear, the [return assembly](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/trap/syscall.S) selects SYSRET. Its flag mask preserves IOPL ([Intel SYSRET](https://www.intel.com/content/dam/www/public/us/en/documents/manuals/64-ia-32-architectures-software-developer-vol-2b-manual.pdf)).

Without a trusted filter, user code reaches IOPL = 3 and can perform port I/O without permission-bitmap checks ([I/O protection](https://cdrdv2-public.intel.com/843827/253665-sdm-vol-1-dec-24.pdf)). This breaks device mediation without a CPU exception.

### Changes after validation {#mutation-after-submission}

Linux's [service-pointer contract](../blueprint/linux-mode/kernelet-api-service.md) accepts grant buffers that a tenant may also write. Directly checking and restoring such a buffer permits:

```text
Host:           check the image; MXCSR is legal
Another CPU:    tenant changes the same bytes; sets MXCSR bit 31
Host:           XRSTOR consumes the changed image and raises #GP
```

The same SSE conditions apply. This TOCTOU path requires an actual concurrent writer. Private `FpuContext` storage instead has a `load(&self)` borrow that excludes safe `as_bytes_mut()` access. Its typed frames [cannot become user-mappable `UFrame`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/mm/frame/mod.rs#L334-L348); [user mapping requires that type](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/mm/vm_space.rs#L358-L370). That private-object path establishes no permitted concurrent writer.

## Current design {#current-design}

| State | Asterinas host design | Linux host design |
|---|---|---|
| General registers | One task-stack `RawUserContext` serves assembly and vOSTD. No IOPL filter is specified. | Hooks copy between the kernelet context and carrier `pt_regs`. The outbound hook filters flags and fixes selectors. |
| FPU | Native per-thread `FpuContext` save/load; D17 separately preserves live carrier state. No image validation or restore-fault recovery is specified. | `fpu_load` copies into private staging, validates, then restores with instruction recovery and ownership repair. `fpu_save` saves into cleared staging and copies out. |
| FS/GS | Native FS access and inactive user-GS MSR access preserve active host GS. | Saved carrier fields and hardware are updated together under preemption protection. Effective-value capture after user FSGSBASE writes remains unspecified. |

Sources: [Asterinas user mode](../blueprint/asterinas-mode/virtualizing-ostd/user-mode.md), [API taxonomy](../blueprint/asterinas-mode/virtualizing-ostd/README.md), [D17](../blueprint/asterinas-mode/virtualizing-ostd/tasks.md#decides) and [Linux user mode](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#fpu). Neither host specifies invalid-base rejection or write-fault recovery.

Linux's snapshot prevents later source writes from changing checked input; validation separately rejects malformed values. Both hosts must preserve per-thread and per-carrier state. An unchanged same-thread syscall needs no per-thread FPU save/load pair. Hardware save/restore remains necessary even without buffer copies.

## Proposed approach {#proposed-approach}

### Common protocol and host changes

**Common protocol.** Keep writable APIs and preserve application state permitted by the host policy. Applications may modify ordinary user memory; only the restore operands must remain stable during trusted use. Host assembly and vOSTD share one general-register frame, and FPU operations use private save areas directly. Check control fields, substitute safe defaults for malformed FPU images and bases, and filter user privileges. The host retains hardware entry, carrier scheduling and physical interrupt handling.

**Asterinas.** Keep the shared `RawUserContext`, host-backed task stacks and D17. Route register loads through fixed trusted helpers that enforce input policy and contain restore faults. Add the return filter and initialize state before independent save calls. Protect hardware updates so D17 cannot save partially restored state.

**Linux.** Replace register-copy hooks and FPU staging with direct use of private native-format storage. Adapt frame placement, entry/return code, `task_pt_regs()`, unwinding and synchronized host readers; matching layout alone is insufficient. Preserve native FPU ownership and address-space adoption. Complete FS/GS capture and keep saved carrier fields consistent with hardware. These changes require host integration, not just a new vOSTD representation.

### CPU scope

Use the book's classic 64-bit entry path and the host-approved x87, SSE, AVX and AVX-512 components. Use one save format and component policy valid on every eligible CPU; reject incompatible CPUs for carrier placement. Enable the chosen instructions' prerequisites. Disable user PKU, matrix-state permission and [user shadow stacks](https://www.kernel.org/doc/html/v6.12/arch/x86/shstk.html) for this protocol; their additional state needs separate handling. Preserve native interrupt/NMI rules and CPU workarounds. Validators and bridges must avoid vector arithmetic and waiting x87 instructions while tenant state is live, as required by the [instruction audit](../blueprint/linux-mode/builds-and-images.md#audit).

### Storage ownership {#stable-operands}

The host derives sandbox identity from the carrier and verifies ownership, format, alignment and resident backing before hardware access. Private typed storage must remain unavailable to user mappings. Hold `load()`'s shared borrow through validation and restoration, and `save()`'s exclusive borrow through output completion. Linux's direct-buffer service excludes arbitrary grant buffers with concurrent writers.

`execute()` holds an exclusive context borrow until exit capture completes and host readers release the frame. Host-reader borrows must end before the carrier resumes. Trusted code must preserve these lifetimes across service calls and declare assembly memory accesses accordingly. Termination blocks new operations; cleanup releases backing only after hardware access and readers finish. Retain host-private [service-depth protection](../blueprint/asterinas-mode/kernelet-api-service.md) and [Linux cleanup rules](../blueprint/linux-mode/faults-and-reclamation.md#leaving).

### Restore policy

| Input | Trusted action |
|---|---|
| FPU image | Check format, size, alignment, header reserved fields, component selection and `MXCSR`. Restore a valid image directly; otherwise restore a trusted initial image without modifying the source. |
| FS/GS | Capture the scalar once. Keep canonical values, including unmapped addresses; replace noncanonical values with zero. Write that captured result while preserving active host GS. |
| Return frame | Clear IOPL, NT, VM, AC and reserved flags; force IF and fixed bit 1. Use fixed user CS/SS; Linux sets `orig_ax = -1`. Preserve permitted data and debug flags. |

For standard XSAVE, require `XCOMP_BV = 0`, zero reserved header fields and `XSTATE_BV` limited to allowed enabled components. Check `MXCSR` against the host's mask, not submitted `MXCSR_MASK`; the host supplies the instruction mask. The initial image must be immutable, aligned, resident and valid for the selected format. Its `MXCSR` must be initialized even when `XSTATE_BV = 0`. Constructors create images under the same policy ([Intel restore rules](https://cdrdv2-public.intel.com/843855/334569-sdm-vol-2d-dec-24.pdf)).

Check return fields after the last callback that can modify them. RIP/RSP must lie in the supported canonical user range; they need not name mapped pages. Use SYSRET only under native eligibility rules, otherwise checked IRET. Invalid return state terminates the sandbox through trusted cleanup. Legal floating-point data, rounding modes, exception masks and TLS bases remain unchanged.

### Save, enter and return {#handoff-protocol}

1. **Initialize.** Before any kernelet callback, initialize live and saved carrier FPU state and zero user bases. Save APIs must never expose host or other-sandbox registers, even if preparation hooks are omitted.
2. **Prepare and bind.** Hold the context borrow and mask virtual interrupts while preparation hooks run. Each restore API enforces its own policy. The host checks frame placement and binds the frame and authorized address space in host-private carrier state. Reject a second active binding.
3. **Enter.** Preserve the suspended kernel continuation separately from the user frame. Complete callbacks and any sleeping host work first. Under the host's final entry guard, install effective bases, apply the return policy and recheck termination and pending events. Enter user mode without releasing the guard's protection between these checks and the hardware transition. Keep frame and address-space references, with no host locks held across user execution.
4. **Capture.** Preserve early scratch registers while establishing trusted stack, GS and kernel mappings, then save directly into the bound frame. Capture effective user bases and page-fault CR2 before enabling interrupts or allowing preemption. Complete return reason and exception metadata for that frame.
5. **Complete.** Finish host work, release frame readers and clear the binding before returning to vOSTD. End the context borrow when `execute()` returns. A subsequent tenant-task switch binds the next task's frame and address space.

If an event returns before user entry, retain submitted state; do not claim a user-state capture. After actual user execution, capture effective FS/GS, including user FSGSBASE writes, before stale fields can be reloaded. Base `save()` returns effective carrier values, including pending loads before user entry. Linux updates saved carrier fields and hardware under one preemption guard. Asterinas updates effective user bases and hardware under host interrupt protection, retaining inactive user-GS access.

Physical interrupts remain host events. Nested kernel-mode interrupts use separate frames. Host preemption preserves carrier bindings and live FPU/base state; migration retains the destination's host GS. A host decision to resume user execution repeats the final entry checks; a return to vOSTD completes capture and clears the binding. D17 and Linux's native FPU machinery protect carrier switches separately from tenant-thread switches.

Linux's bridge must save into the bound frame before constructing a second full register image. Preserve the syscall number before [native entry](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/entry_64.S) replaces the saved RAX value. Host C work uses the Linux stack, and the suspended Rust call stays on its kernelet stack. Old host calls and readers cannot retain the previous frame after handoff. Preserve KPTI, entry mitigations and the [tenant gate](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#gate), including `int $0x80`; disable legacy vsyscall emulation for this scope. Early scratch stores and hardware-frame transfers remain.

### FPU ownership and failure cleanup {#kernel-exception-audit}

Before save or load, establish the carrier's live FPU state. Linux retains `fpregs_lock()` and `switch_fpu_return()` when needed; successful loads remain authoritative across native return. Asterinas's fixed host helper keeps physical interrupts disabled through ownership checks, hardware access and any fault repair. It exposes no callback to the kernel proper. Initialize destination bytes that XSAVE may leave unwritten before direct save. Sources: [Linux FPU ownership](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#fpu) and [Asterinas D17](../blueprint/asterinas-mode/virtualizing-ostd/tasks.md#decides).

Keep restore-site fault recovery alongside input checks. Match the faulting instruction and active carrier operation before attributing failure to a sandbox. Repair partially restored FPU state before releasing ownership protection: Linux invalidates hardware ownership and requires a trusted reload; Asterinas restores initial state before D17 can capture it. Failed base writes must preserve host GS and consistent carrier fields.

Return-fault entry code must recognize transition windows and regain trusted stack, GS and mappings before ordinary host handling. Terminate the affected sandbox after state repair and host-resource release.

Linux shared-frame integration, added recovery paths and Asterinas carrier FS/GS preservation remain **[unverified]**.
