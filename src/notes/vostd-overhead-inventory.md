# Direct vOSTD memory and execution protocol

## Problem {#problem}

The current Linux-host design uses Linux interfaces to manage tenant address spaces and enter user mode. Its adapters add [service calls for page-table changes](../blueprint/linux-mode/virtualizing-ostd/memory.md#ops), [register copies](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#user-run), and transfers between kernelet and carrier stacks. For kernel access to user buffers, vOSTD [walks page tables in software](../blueprint/linux-mode/virtualizing-ostd/memory.md#copies). The [memory analysis](memory-api-costs.md) and [execution analysis](user-mode-handoff-costs.md) document these costs and their evidence.

The proposed protocol aims to reduce adaptation work while preserving OSTD API behavior. The Host must retain control over [carrier scheduling](../blueprint/linux-mode/virtualizing-ostd/tasks.md#carriers), [sandbox termination](../blueprint/linux-mode/faults-and-reclamation.md#stopping), and [resource ownership](../blueprint/linux-mode/virtualizing-ostd/memory.md). The protocol must retain page tables and execution-context storage [until their last users finish](../blueprint/linux-mode/faults-and-reclamation.md#destroy). Trusted code must validate [CPU-state inputs supplied through safe APIs](user-mode-handoff-costs.md#caller-api) before restoring them.

## Current design {#current-design}

### The selected Linux boundary {#current-choice}

Linux schedules [carriers](../blueprint/linux-mode/virtualizing-ostd/tasks.md#carriers), while the kernel proper schedules its Tasks on those carriers. Each tenant `VmSpace` uses a [Linux `mm_struct`](../blueprint/linux-mode/virtualizing-ostd/memory.md#space), whose page tables the endovisor updates through Linux helpers. [`vm_activate()`](../blueprint/linux-mode/virtualizing-ostd/memory.md#ops) records the virtual CPU's selected space without installing its page-table root. Before user entry, [`user_run()`](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#adopt) adopts that space if it differs from the carrier's current Linux address space.

The current [user-run bridge](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#user-run) preserves the suspended `execute()` call and uses Linux's paths to enter and leave user mode. It switches between kernelet and carrier stacks and copies registers between the tenant context and Linux's `pt_regs`. Linux preserves carrier FPU state across Host scheduling. vOSTD uses Host services to coordinate tenant-thread [FPU save and restore](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#fpu) with Linux's carrier state.

### Root cause and retained obligations {#root-causes}

The current Linux-host design implements OSTD operations through Linux's [address-space](../blueprint/linux-mode/virtualizing-ostd/memory.md#ops) and [user-entry](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#user-run) interfaces. These interfaces require Linux memory-management objects and stack and register conventions that differ from OSTD's native mechanisms. Reusing them provides Linux's [address-space switching](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#adopt), [TLB invalidation](../blueprint/linux-mode/virtualizing-ostd/memory.md#flush), and user-return machinery. The adapter translates mapping operations and transfers execution state on the affected runtime paths, so this adaptation work recurs during execution. These costs follow from the selected interface boundary; the Blueprint does not establish that every Linux backend must incur them.

The Asterinas-host design reuses OSTD's [page-table code](../blueprint/asterinas-mode/virtualizing-ostd/memory.md) and [user-context layout](../blueprint/asterinas-mode/virtualizing-ostd/user-mode.md). vOSTD edits tenant PTEs locally, and Host entry code saves registers directly into the kernelet's context. The Host must still restore page-table roots, complete required TLB invalidation, and delay reclamation until all users finish. The endovisor must preserve tenant FPU and FS/GS state across carrier scheduling. The Blueprint records [gaps in global-mapping invalidation during reclamation](../blueprint/asterinas-mode/faults-and-reclamation.md) and [incomplete FS/GS preservation in the prototype](../blueprint/asterinas-mode/virtualizing-ostd/tasks.md#decides).

### Relation to the authoritative design {#state-validity}

This proposal follows the host chapters' [carrier model](../blueprint/linux-mode/virtualizing-ostd/tasks.md#carriers): the Host schedules carriers, and each kernelet schedules its own Tasks. The [Paper](../paper/api-virtualization.md#why) describes one scheduler and, for Linux, one Host task per kernelet Task. These descriptions conflict. The proposed protocol still requires validation in an integrated Linux backend.

The [writable context APIs](user-mode-handoff-costs.md#caller-api) let safe Rust callers construct values that encode invalid CPU state or excess user privilege. Copying those values into private storage does not validate them. Trusted vOSTD and Host code must validate restore inputs, enforce user privilege limits, and contain faults at identified restore instructions.

## Proposed approach {#proposed-approach}

### Direct memory and execution {#direct-vostd}

The proposed backend lets trusted vOSTD manage [tenant page tables](#memory-overview) and [user contexts](#execution-space-contract). The Host installs the selected page-table root before dependent execution resumes. Host entry and return code use these contexts to save and restore tenant registers. Linux continues to schedule carriers and handle physical interrupts.

The memory protocol aims to remove Linux mapping-service calls for [ordinary user PTE updates](#memory-operations-overview). [Direct user-buffer access](#user-access) also aims to remove software page-table walks from successful accesses. The execution protocol aims to remove the copy from a separate carrier `pt_regs` frame on [ordinary tenant `SYSCALL` entry](#handoff-protocol).

Kernel proper retains tenant memory and scheduling policy through the OSTD API. This proposal requires the [supported x86-64 Host configuration](#cpu-scope), with PTI and PCID inactive under existing Host policy.

### Memory: local page tables and direct user access {#memory-overview}

#### Page tables and CPU-use records

A `VmSpace` represents a tenant address space with its own page-table root. The root identifies the page tables used for address translation. Trusted vOSTD creates and updates the user mappings requested by kernel proper. The Host installs the root's [kernel entries](#memory-protocol) and maintains the shared kernel mappings.

[Native OSTD](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/mm/vm_space.rs#L70-L150) stores a page table and a CPU-use set in `VmSpace`. When switching spaces, `activate()` updates CPU-use records before activating the new page table. The [TLB flusher](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/mm/tlb.rs#L31-L95) uses the set to select target CPUs. The excerpt below shows the relevant fields:

```rust
pub struct VmSpace {
    pt: PageTable<UserPtConfig>,
    cpus: AtomicCpuSet,
    // Other fields omitted.
}
```

Linux can suspend a carrier or resume it on another physical CPU. vOSTD therefore cannot use a virtual CPU identifier as a physical CPU identifier. In this proposal, vOSTD manages tenant page tables, while the Host tracks their [physical CPU use](#scheduling). Each `VmSpace` stores a registered space identifier that refers to the Host record:

```rust
// Proposed structure; not a complete implementation.
struct VmSpace {
    pt: PageTable<UserPtConfig>,
    host_space: SpaceId,
    // Other ownership and lifetime fields omitted.
}
```

The identifier refers to a Host record for the same page-table root. The Host tracks the following information across [space, carrier, and per-CPU records](#retained-state):

| Host information | Purpose |
|---|---|
| Root physical address and owning sandbox | Identify the page table and authorize its use |
| Physical CPUs using or admitted to the root | Cover physical users during invalidation |
| Outstanding uses, including suspended execution | Prevent release while an operation or continuation still needs the root |
| Invalidation progress | Determine which mapping changes a CPU has covered |

Tenant mappings remain in vOSTD's page tables. The Host needs neither a Linux `mm_struct` for each tenant address space nor a duplicate mapping database. Carriers retain the native Linux task and address-space accounting required by the scheduler. The [retained-state contract](#retained-state) specifies these records and their ownership.

#### Create and use an address space

The proposal uses the existing [service table](../blueprint/linux-mode/kernelet-api-service.md) to connect vOSTD page tables with Host records. It adds `vm_register_root()` and changes `vm_activate()` to install the selected root before returning success. The [loader checks](#cpu-scope) that vOSTD and the Host use these semantics:

```c
/* Proposed addition. */
int64_t vm_register_root(uint64_t root_paddr);

/* Existing entries with the proposed direct-backend semantics. */
int64_t vm_activate(uint64_t space);
int64_t vm_flush(uint64_t space, uint64_t va, uint64_t len);
```

vOSTD first allocates an exclusive empty root and prepares an object to retain its resources during deferred cleanup. It then calls `vm_register_root()` with the root's physical address. The Host checks ownership and initial state, installs shared kernel entries, and publishes the root record. Success returns a space identifier for vOSTD to store before exposing the completed `VmSpace`. The [registration protocol](#memory-protocol) defines publication and failure cleanup.

Once registered, trusted vOSTD establishes the requested user mappings through [local page-table operations](#memory-operations-overview). The Host later installs this same root; registration does not copy the tenant mappings into another page table.

Creating page tables does not make the CPU use them. vOSTD requests [activation](#root-installation) through the Host service table. Success means the required page-table root is installed on the current physical CPU. The Host may reuse an installation that still satisfies the activation contract.

Successful activation records the address space required by the kernelet's continuing execution. Before [resuming a carrier](#resumption-overview), the Host restores the memory environment required by the code being resumed. A Host service can temporarily require a native environment while retaining the selected tenant space.

With the required root installed, vOSTD accesses user buffers through [trusted routines](#user-access) that use user virtual addresses. These routines validate user ranges and permitted operations and retain the objects needed by each access. The CPU translates addresses through the installed page tables, so successful accesses avoid software page-table walks. The Host must provide SMAP handling and recovery for faults at registered access instructions.

#### Update user mappings {#memory-operations-overview}

Trusted vOSTD updates ordinary user mappings through local page-table operations. This removes the Linux mapping-service calls and helper setup for those updates. vOSTD still enforces frame ownership and mapping permissions and preserves required frame lifetimes. The Host completes any required [invalidation of cached translations](#invalidation).

For example, removing a mapping follows this order:

```text
vOSTD removes the PTE and retains the removed storage
  -> vm_flush(space, address, length)
  -> Host completes invalidation for the affected physical CPUs
  -> vOSTD completes the remaining lifetime conditions before release
```

The Host synchronizes root installation with invalidation, so a CPU entering during an update cannot escape the required flush. The [switch and flush protocol](#invalidation) defines that ordering. Software-reader grace remains a separate requirement before storage reuse. Ordinary memory accesses require no Host service call to translate each address.

### Execution: direct context handoff and checked restoration {#execution-space-contract}

The execution protocol addresses both [context-transfer costs and safe API inputs](user-mode-handoff-costs.md#problem). It connects Host entry to the suspended vOSTD call. Trusted code also validates caller-supplied CPU state before restoration.

#### Retain the context and complete one user run

A user context holds the register values needed to resume a tenant thread in user mode. A kernel continuation records where a suspended kernel call will resume, including its stack position and required registers. Each internal `run_user()` invocation binds these two objects until completion or cancellation. Here, `run_user()` names one invocation through the `user_run(ctx)` service; the outer OSTD `execute()` loop can invoke it repeatedly.

With the tenant address space selected, an ordinary syscall round trip follows this sequence:

1. vOSTD saves the kernel continuation and exclusively borrows the Task's user context for this run.
2. The Host completes preparation, validates restore inputs, establishes the required address space, and prepares the CPU's register-capture destination.
3. Trusted return code restores the user registers and enters the tenant thread.
4. On a syscall, Host entry code saves registers into the bound context and completes the required Host processing.
5. The Host restores the required memory environment and kernel continuation, so the same internal call returns a syscall event.
6. The vOSTD execution loop delivers that event to kernel proper, which handles the tenant syscall.

The [run protocol](#direct-transition) retains the context, kernel stack, and address space throughout this interval. On ordinary tenant `SYSCALL` entry, saving registers into their final context removes the copy from a separate carrier `pt_regs` frame. Other entries can retain native frames and copy them into the context. Normal completion releases the context borrow after its last user finishes; the Task continues to use its kernel stack.

#### Complete Host work before resuming kernelet code

Ordinary Linux C services execute on the carrier's Linux stack. A short audited service can stay on the kernelet stack under the [service contract](#host-boundaries). A Host scope is an unfinished Host call with its locks, references, or bookkeeping obligations. Host calls must return through their cleanup paths before the final assembly transfer. Direct context handoff therefore retains stack transfers where Host processing requires them.

Tenant syscalls and user exceptions complete the matching user run with an event for vOSTD. The [entry protocol](#handoff-protocol) routes them to kernel proper for tenant policy. Registered kernel-copy faults return through their own recovery points; they do not complete an unrelated user run.

Linux completes ordinary physical interrupt handling before vOSTD delivers the corresponding virtual event. Virtual interrupt guards defer callbacks during protected transitions. For an interrupted kernelet Task, the Host releases its temporary bridge before vOSTD invokes callbacks or switches Tasks. The [interruption protocol](#host-boundaries) distinguishes this local scheduling from Linux's scheduling of carriers.

The proposal also uses Linux's clock and process tick. vOSTD adapts time units and callback periods to the Host configuration. The [time protocol](#time-protocol) separates elapsed time, pending tick notifications, and Host supervision of carriers.

#### Validate CPU state before restoration {#state-validation-overview}

Safe Rust callers can supply register values that encode invalid CPU state or excess privilege. Trusted code [validates restore inputs](#state-protocol) while holding the context against concurrent modification. Copying a value into private storage does not establish its validity.

Validation constrains return addresses, privilege flags, segment bases, and FPU state to the admitted CPU contract. Recognized restore faults use [instruction-specific recovery](#kernel-exception-audit) to repair partially changed state and prevent unsafe continuation. An unrecoverable private kernelet fault requests instance stop. Host operations still complete their required cleanup before control leaves their call chain.

Linux preserves the carrier's active floating-point state across Host scheduling. vOSTD maintains each tenant Task's snapshot and synchronizes it when internal Task switching requires that transfer. These responsibilities remain separate from direct general-register capture.

### Keep execution and memory consistent {#resumption-overview}

#### Restore the environment before execution resumes

An execution context determines which instructions resume and which registers they use. The installed page-table root determines what the resumed code's virtual addresses mean. Before resuming code, the Host must establish the memory environment required by that continuation. A correct register restore under the wrong root can access another address space.

Linux switches address spaces before restoring the next task's registers and stack. The following excerpt shows the order in its [native user-task branch](https://elixir.bootlin.com/linux/v6.12/source/kernel/sched/core.c#L5294):

```c
switch_mm_irqs_off(prev->active_mm, next->mm, next);
/* Other scheduler work omitted. */
switch_to(prev, next, prev);
```

The proposal uses the [common root installer](#interfaces) at this switching point. It takes the incoming task and the address space required by its retained continuation. Native reference accounting and required scheduler ordering remain in place. The carrier's selected tenant space remains distinct from the environment required by an in-progress Host service.

Linux can suspend or migrate a carrier while a run or Host service is active. The carrier retains the saved execution state and its required address space. Before resuming that execution, the Host [restores the required environment](#scheduling) on the destination CPU. It also refreshes CPU-local entry state before the next user entry.

For example, Linux can suspend a carrier during a user-memory copy and resume it on another CPU. That CPU must install the copy's required page-table root before the copy continues. Recording the carrier's selected space alone cannot establish this physical state.

The carrier retains the environment its continuation requires; the physical CPU records the environment actually installed. Linux's native `switch_mm_irqs_off()` reads `loaded_mm` and assumes a Linux address-space object. The backend must distinguish native, tenant, and transitional states before any native consumer dereferences that object. Writing CR3 without updating these consumers leaves Linux's bookkeeping inconsistent with hardware. [Native lookup and same-mm shortcut](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/tlb.c#L498), [installation protocol](#root-installation)

#### Finish outstanding use before release {#lifetime-overview}

Closing an address space prevents new ordinary uses. Existing operations retain their storage until their permitted accesses finish. The Host then acknowledges the end of its uses, and vOSTD waits for remaining software readers before destroying the tables.

Stopping a sandbox first closes new execution admission. Each carrier reaches a Host boundary that ends its ability to resume tenant execution. The Host also waits for outstanding services, callbacks, and device accesses before releasing their resources. A device can retain a buffer after the CPU stops using its address space.

The [retirement protocol](#space-retirement), [device protocol](#device-lifetime), and [stop protocol](#stop-and-reclaim) give these users separate completion conditions. Clearing an active pointer or sending a stop request does not establish that their final accesses have ended.

### Expected savings {#expected-savings}

| Operation | Proposed mechanism | Work that can be removed |
|---|---|---|
| Tenant page-table updates | Trusted vOSTD updates user PTEs locally | Service entry and Linux helper setup for PTE updates |
| Successful kernel access to user buffers | Direct user-address access under the target root, with SMAP handling and registered fault recovery | Per-page software page-table walks |
| Ordinary tenant `SYSCALL` entry | Host entry code saves registers directly into the active user context | Copy from a separate Linux `pt_regs` frame into that context |

## Implementation plan {#implementation-plan}

Implement the protocol as one negotiated backend. Existing interface names preserve caller compatibility only where their admitted contract says so. The following operations require Linux core changes; a module cannot obtain the complete behavior by exporting helpers or writing CR3 alone.

The code sketches below connect each protocol to its implementation boundary. Blocks marked **Existing code** quote the pinned source, with omissions marked. Blocks marked **Proposed code** specify additions or changed behavior; they have not been compiled. Their helper contracts include the ownership, failure, and ordering rules in the accompanying text. Assembly sketches show transfer order, not complete instruction sequences or unwind annotations.

| Component | Concrete changes | Completion criterion |
|---|---|---|
| [Loader and service ABI](#implementation-contracts) | Joint memory/execution profile, table extents, trusted-build checks, CPU placement constraints, new registration entries | Reject unsupported combinations before publication or execution |
| [vOSTD memory](#memory-protocol) | Owned roots, local cursors, direct-profile activation, retained unmap owners, deferred full-space Drop | No legacy Linux-mm writer; no release before corresponding completion |
| [Linux root backend](#root-installation) | Foreign-state dispatch, kernel-update list, common installer, generation callbacks, nonblocking unregister | Every reachable native consumer has a defined foreign-root action |
| [Linux scheduler](#scheduling) | Incoming continuation selection, native accounting, same-root barriers, CPU publication retirement | Resume only after required environment installation, including migration |
| [vOSTD execution](#direct-transition) | Call-scoped run and kernel continuation, final register backing, mirror ownership, logical FPU hooks | Each run completes or cancels once without stale reader reachability |
| [Linux entry and return](#handoff-protocol) | Direct syscall capture, explicit frame consumers, user and kernel IRQ bridges, exact-frame completion, fixed final dispositions | Correct GS, stack, root, IF, context tracking, and unwind state at each instruction boundary |
| [State operations](#state-protocol) | Verified FPU conversion, checked restore faults, effective TLS capture, admitted extended-state transfer | Invalid input cannot grant privilege or corrupt Host ownership |
| [Fault and event integration](#host-boundaries) | Registered copy fixups, vector-specific wrappers, NMI tokens, durable event/STOP publication, early panic entry | Every admitted route reaches its specified receiver without abandoning Host scopes |
| [Time and tick adaptation](#time-protocol) | Host time source, tick units, retained notification counts, callback periods, idle deadlines | Carrier suspension and notification coalescing do not stop elapsed time or discard accounting |
| [Lifetime and devices](#lifetime-implementation) | Operational-use ledger, persistent readiness, deferred work joins, DMA allocation/slot leases, queue drain | Reuse follows final CPU, software, and device use |

Complete the user-run assembly, generated offsets, frame-reader integration, and supported Host-work inventory together. Retain the currently specified protocols when implementing them; a missing test alone does not turn them into undecided mechanisms. Source-level composition and bounded research models do not establish that an integrated Linux implementation already exists.

Asterinas can use the same object ownership, completion, and lifetime contracts with Host-specific entry and scheduler adapters. Its OSTD compatibility simplifies representation handling. Verify dynamic root restoration, physical shootdown membership, ordinary flusher routing, FPU/base preservation, and global image reclamation independently. The Asterinas backend cannot be declared complete from shared API names alone.

### Configuration, records, and interfaces {#implementation-contracts}

#### Supported architecture and admission {#cpu-scope}

This proposal targets native x86-64 with four-level paging, conventional IDT entry, and 64-bit tenant userspace. The memory protocol specified here assumes PCID is disabled and prohibits Global tenant user mappings. FRED, Xen PV, compatibility-mode tenants, and `PREEMPT_RT` remain outside this proposal's scope. CPU hotplug is outside scope; the protocol assumes a fixed online CPU set while sandboxes run. Every CPU eligible to run a carrier must support the admitted CPU features and context format.

This user-entry profile requires Page Table Isolation (PTI) to be inactive under the Host's existing policy. For each tenant address space, user execution and kernelet kernel execution use the same registered full root. Host-private mappings remain supervisor-only. Trusted entry code, per-CPU data, stacks, and recovery records remain mapped. The loader rejects incompatible Hosts without disabling their mitigations. This profile remains **[unverified]**.

With PTI inactive, Linux skips the PTI-specific CR3 changes on kernel entry and user return. The direct backend must still install the required address space and track the root actually loaded on each CPU. [PTI root operations](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/calling.h), [root installation](#root-installation)

Audit inherited high-address mappings as well as the tenant lower half. Reject an actual user-readable Host vsyscall mapping for this initial profile; do not silently remove it from the Host. Route tenant faults before native vsyscall emulation or carrier VMA resolution. Shared-root user entries must permit execution according to tenant PTE permissions, without a PTI kernel-view top-level NX override. [Vsyscall mapping](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/vsyscall/vsyscall_64.c)

Extend the existing image and [service-table](../blueprint/linux-mode/kernelet-api-service.md) size negotiation with memory and execution contract identifiers. The loader checks both together, the trusted source hash, required table and info-page extents, CPU features, and admitted mappings before first execution. A selection-only `vm_activate()` backend cannot advertise the direct-root contract. Unknown combinations fail admission.

**Proposed code: loader admission.** Extend the [image loader's existing validation](../blueprint/linux-mode/builds-and-images.md) before publishing a carrier or an executable entry.

```c
/* Sketch: all checks precede publication; these are proposed predicates. */
if (!klet_image_matches_trusted_build(image) ||
    !klet_has_joint_contract(image, DIRECT_MEMORY, DIRECT_EXECUTION) ||
    !klet_table_extents_valid(image) ||
    !klet_host_matches_profile() ||
    !klet_eligible_cpus_match_context(image) ||
    !klet_entry_mappings_valid(image))
        return -KLET_UNSUPPORTED;

/* The normal creation transaction still checks RUNNING under the gate. */
return klet_publish_carrier_if_running(sandbox, prepared_carrier);
```

The profile predicate checks actual paging, entry, PTI, and PCID state. It does not change Host policy. The final publication rechecks instance state because stop can race with the earlier validation.

#### Retained objects and state {#retained-state}

These records let the Host authorize execution and retain its storage. They do not duplicate vOSTD's mapping policy. An operational use is a counted right to keep using a registered object; its holder must release it after the final access.

| Object | Trusted owner and retained information | Last-use condition |
|---|---|---|
| Sandbox | Endovisor; admission gate, stop generation, carrier membership, grants, and workers | All execution, resource, and queued-work obligations have ended |
| Registered space | Endovisor metadata; identity, root backing, phase, physical-use mask, and flush generation | Unregister acknowledges Host completion; vOSTD then completes local reader grace |
| Tenant page tables | vOSTD; user nodes, PTE ownership, mapped owners, and associated `IoMem` pins | Required TLB completion and software-reader grace precede destruction |
| Carrier | Endovisor; selected space, saved continuation requirement, service depth, Linux stack, and active run | Terminal Host context and retired CPU publications |
| Active run | Trusted transition code; exclusive user backing, actual Task kernel continuation, physical flags, root use, and mirror obligation | Normal completion or terminal cancellation finishes every reader and restore tail |
| Physical CPU | Architecture backend; actual native/foreign/transition state, generation, and prepared capture destination | Actual departure and owner-CPU publication retirement |
| DMA allocation | Private vOSTD owner plus Host registry; exact allocation or pool slot, rights, and captured backend uses | Unregister acknowledges the final device and ring access |

The carrier's selected space, its saved continuation requirement, and the CPU's installed root are distinct. A Host service can temporarily require native memory while retaining the tenant selection. Migration changes the installed CPU without ending the carrier's saved requirement.

The run's *mirror* records the zero-or-one contribution that vOSTD guards add to Linux's preemption count. Host preparation can temporarily remove that contribution without changing unrelated Linux nesting. The run retains the obligation to restore it before the corresponding kernelet continuation resumes. [Existing mirror contract](../blueprint/linux-mode/kernelet-api-service.md#depth)

State machines retain separate meanings:

```text
sandbox: RUNNING -> STOP_REQUESTED(generation) -> EXECUTION_QUIESCENT
space:   PREPARING -> LIVE -> CLOSING -> DETACHING -> UNREGISTERED
run:     HOST_RUN -> ADMITTED -> HOST_RUN -> COMPLETE -> KERNEL
carrier terminal state: QUIESCENT(generation)
CPU:     Native(mm) | Switching | Foreign(space, view, seen_generation)
```

`EXECUTION_QUIESCENT` records execution withdrawal; independent resource users may still be draining. `ADMITTED` covers the final departure tail and user execution, so it is not a hardware privilege-level test. Entry classification also uses the actual frame and trusted operation provenance.

Each generation belongs to one state machine. A root's flush generation identifies published mapping changes; run and stop generations identify execution records. They are not interchangeable. The CPU's `view` is the single full-root view in this profile; it does not imply a second PTI root.

Use the sandbox memory/admission lock as the single IRQ-safe instance gate. It protects instance state, carrier membership, root registration, and acquisition of operational root uses. Registration publication and STOP therefore share one commit order. Hold it only for bounded state updates; release it before root locks, `pgd_lock`, allocation, scheduling, wakeups, or waits. Scheduler release paths keep the order runqueue lock, then instance gate; they defer notifications through preallocated HARD IRQ work.

`RUNNING` checks govern new external admission. An existing retained use can authorize bounded completion or cancellation work after closure, including continuation restoration. Keep its parent lease until the child use is published. An integer identifier alone cannot acquire that authority.

#### Service and internal interfaces {#interfaces}

The following entries extend the existing service boundary. The Host authenticates the caller through its current carrier; kernel proper cannot submit another sandbox identity. Signatures describe proposed semantics and require generated C/Rust layout checks.

```c
/* Existing signatures; semantics selected by the admitted direct profile. */
int64_t vm_activate(uint64_t space);
int64_t vm_flush(uint64_t space, uint64_t va, uint64_t len);
int64_t vm_destroy(uint64_t space);
int64_t user_run(struct klet_user_ctx *ctx);
int64_t fpu_save(void *area, uint32_t len);
int64_t fpu_load(const void *area, uint32_t len);

/* Proposed appended service entries. */
int64_t vm_register_root(uint64_t root_paddr);
int64_t dma_register_alloc(uint64_t paddr, uint64_t len,
                           uint32_t kind, uint32_t rights);
int64_t dma_unregister_alloc(uint64_t dma_id);
```

| Interface | Completion and failure contract |
|---|---|
| `vm_register_root` | Return a positive, nonreused identifier only after publication. An ordinary error retains no reference from this registration attempt. |
| `vm_activate` | Success means the required root is installed and the carrier requirement is committed before dependent code resumes. Zero selects the native environment. Rejection preserves the old binding. |
| `vm_flush` | Synchronous, nonsleeping completion of the specified invalidation. The valid retained-space path cannot fail and still authorize reuse. Preserve the existing full-flush sentinel. |
| `vm_destroy` | Close new use. Return zero only after Host use and kernel-update membership end; return `BUSY` without waiting while uses remain. |
| `user_run` | Retain one internal run until syscall, exception, event, or preparation completion. Preserve the existing event codes; a negative backend-fault result requires defined vOSTD handling. Instance stop cancels the run without returning. |
| `fpu_save/load` | Validate private backing and supported format; synchronize the carrier state. Retain no snapshot pointer after the call. |
| `dma_register/unregister_alloc` | Register exact trusted DMA backing. Unregister closes admission and returns zero only after all captured uses end; `BUSY` retains ownership. |

The direct profile rejects legacy `vm_create`, `vm_map`, `vm_unmap`, `vm_protect`, `vm_wait`, and `vm_wake` operations. Local tables and local cursor locking replace that Linux-mm path. The legacy profile retains its existing meanings; the loader never mixes the two ownership models.

The other [existing services](../blueprint/linux-mode/kernelet-api-service.md) keep their caller checks and the following lifetime boundaries. A shared kernel address removes a tenant user-translation dependency; it does not remove ownership or continuation requirements.

| Service group | Required retention and completion |
|---|---|
| `grains_request` | Retain the allocation operation through publication or rollback. Published grants remain owned until sandbox cleanup. |
| `kstack_alloc/free` | Keep pool backing through sandbox cleanup. Permit slot reuse only when no run, IRQ frame, or retained continuation can reach the stack. |
| `vcpu_boot/kick` | Retain the target carrier and queued notification through delivery or cancellation. Boot also checks instance admission. |
| `vcpu_idle/yield/on_spin` | Retain the suspended service and its required environment. End wait and timer callbacks before releasing their storage. |
| `log_write/oops` | Preserve source storage through the bounded snapshot. No source pointer survives service return. |
| `mmio_read/write` | Preserve device records; queue operations additionally retain the exact [rings and DMA allocations](#device-lifetime). |
| `stop` | Retain the reason and admitted operations through the [terminal handoff](#stop-and-reclaim); do not return to the image. |

Explicit activation and Host scheduler restoration share one internal operation:

```c
enum root_install_effect { ROOT_RETAINED, ROOT_RELOADED };

/* Proposed Host-internal interface: no allocation, sleep, or remote wait. */
enum root_install_effect endovisor_install_execution_space_irqs_off(
        struct task_struct *incoming,
        const struct execution_space *required);
```

The scheduler passes its incoming task, not the outgoing `current`. `ROOT_RELOADED` reports an actual CR3 load, including a same-root invalidation. The scheduler uses this distinction to preserve ordering when hardware retains the root.

### Memory protocol {#memory-implementation}

#### Construct and register a space {#memory-protocol}

vOSTD prepares an exclusive empty user root and its deferred-retirement owner before requesting registration. This profile uses a dedicated 4 KiB page at an 8 KiB-aligned physical address, avoiding PTI's bit-12 kernel-root conversion hazard. It does not allocate an implicit adjacent user root.

1. Allocate Host metadata, lookup storage, and CPU masks before physical use can begin.
2. Under the sandbox memory lock, reserve identity, quota, and exclusive root ownership as `PREPARING`.
3. Validate the grant, alignment, resident backing, and empty root while the builder retains exclusive ownership.
4. Under `pgd_lock`, copy the Host kernel entries and attach the record to a separate kernel-update list.
5. Recheck sandbox admission under the memory lock, then publish `LIVE`. Do not nest the memory lock and `pgd_lock`.
6. Return the identifier. vOSTD publishes the completed `VmSpace` without another fallible allocation.

Copy and update-list insertion form one locked operation. A concurrent kernel top-entry addition either appears in that copy or reaches the attached record through the update walker. Extend the inspected Linux four-level synchronizer to visit this separate list. Shared child-table changes retain Linux's normal invalidation and lifetime rules. Uncovered producers and shared-parent replacement require an additional contract before support. [Kernel-root synchronization](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/init_64.c)

Use dedicated single-root copy/set operations. Linux's native PTI helpers can access an adjacent root, so they cannot operate on an unpaired allocation. vOSTD may edit only admitted user entries. It must preserve user permissions, forbid Global user mappings, retain backing, and exclude Host metadata or virtual device identifiers from physical PTEs. Registration does not validate future edits; private trusted constructors and cursor operations enforce these rules. [Native root-copy helper](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/pgtable.h), [current ownership rules](../blueprint/linux-mode/virtualizing-ostd/memory.md#ops)

In particular, the Blueprint's [`IoMem` for virtual devices](../blueprint/linux-mode/virtualizing-ostd/devices.md) names service operations, not physical mapping authority. Reject that object before any user PTE store, including fork and remap paths that reproduce mappings. Ordinary virtual register reads and writes continue through MMIO services. An infallible mapping API must use the defined stop path if no earlier check can report the unsupported operation.

**Proposed code: authorize local PTE writes.** Apply the same private checks to mapping, protection, cloning, and page-size changes. Safe kernel-proper inputs cannot bypass this boundary.

```text
before the first PTE store:
    require an admitted user range and permissions without Global
    obtain physical backing authority from a trusted retained owner
    reject virtual-device identifiers and protected Host or vOSTD storage
    retain the backing and metadata needed through update completion
commit the local PTE operation under the page-table synchronization contract
retain removed storage through required invalidation and reader grace
```

Failure before publication removes this attempt's list membership and reservation before releasing storage. After publication, trusted cleanup owns the live record even if stop prevents the service from returning. Cleanup waits for the in-flight registration. An infallible public constructor must use the defined instance-stop allocation-failure path rather than expose a partial object.

**Proposed code: registration and kernel-map attachment.** Linux's [`pgd_ctor()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/pgtable.c#L126) supplies the copy-and-track precedent. The new backend uses separate records because its roots have no tenant `mm_struct`.

```c
/* Service scope retains sandbox; helpers return ordinary errors before LIVE. */
root = klet_prepare_and_reserve(sandbox, root_paddr); /* PREPARING */
if (IS_ERR(root))
        return PTR_ERR(root);
error = klet_validate_exclusive_empty_root(root);
if (error)
        goto discard_reservation;

spin_lock_irqsave(&pgd_lock, flags);
/* Four-level, single-root profile; root->entries is checked Host access. */
for (unsigned int slot = 256; slot < 512; slot++)
        WRITE_ONCE(root->entries[slot], READ_ONCE(init_top_pgt[slot]));
list_add(&root->kernel_update_link, &klet_kernel_roots);
spin_unlock_irqrestore(&pgd_lock, flags);

error = klet_publish_live_if_running(root); /* takes only instance gate */
if (!error)
        return root->id;

spin_lock_irqsave(&pgd_lock, flags);
list_del_init(&root->kernel_update_link);
klet_clear_unpublished_kernel_entries(root);
spin_unlock_irqrestore(&pgd_lock, flags);
discard_reservation:
klet_discard_unpublished_registration(root);
return error;
```

`klet_publish_live_if_running()` commits `LIVE` and checks stop under the same gate. A rejected commit unlinks the root before releasing its reservation. Successful publication transfers cleanup responsibility to the registered-root lifecycle.

Extend [`sync_global_pgds_l4()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/init_64.c#L170) inside its existing `pgd_lock` section:

```c
/* Proposed addition for a newly present master kernel slot. */
list_for_each_entry(root, &klet_kernel_roots, kernel_update_link) {
        if (pgd_none(root->entries[slot]))
                WRITE_ONCE(root->entries[slot], READ_ONCE(init_top_pgt[slot]));
        else
                klet_require_same_child(root->entries[slot], init_top_pgt[slot]);
}
```

The child check compares table identity, allowing hardware-maintained bit differences. Changes below a shared child already reach both roots; their Host invalidation remains necessary.

#### Activate and restore the required root {#root-installation}

The vOSTD activation transaction holds virtual IRQ exclusion through the service call and local active-space reference update. Host execution separately excludes physical IRQs and migration during installation. This prevents a tenant upcall from reentering a partially committed activation while preserving Linux's interrupt handling outside that short interval.

Before changing hardware, resolve the checked space identifier and retain its permitted use. Admission failure leaves the previous binding intact. A validated installation performs no allocation or recoverable failure after physical admission.

1. Under the target root's short lock, add this physical CPU to its use mask and sample the flush generation.
2. Publish `Switching`. NMI readers cannot interpret it as a native mm or access tenant user memory.
3. Apply the admitted architecture controls and issue the flushing CR3 load.
4. Publish the foreign root, view, and sampled generation; invalidate incompatible native cached-mm state.
5. Publish `Foreign`, then remove the old root's membership after hardware departure.
6. Commit the carrier requirement before releasing the transition guard; release old operational uses at their valid boundaries.

The old and new root locks are never held together. No root lock covers a CR3 write, remote wait, or tenant execution. A same-root/view fast path retains membership and still performs required stop and scheduler checks. An audited Host path can keep that root when shared mappings satisfy its accesses.

Acquire operational uses before taking the root synchronization lock. Release that lock before final-use accounting under the sandbox memory lock. Keep the memory lock, root locks, and `pgd_lock` unnested. A void public activation API must contain a rejected request through its defined failure path; it cannot continue dependent code after an ignored service error.

The native/foreign discriminator must guard every reachable native consumer, including delayed flush callbacks and native return shortcuts. A sentinel in `loaded_mm` alone is insufficient. Foreign-to-native return rebuilds valid native tracking and performs the required flushing transition. Kernel/global flushes still invalidate applicable translations while a foreign root is loaded. [Linux TLB state and callbacks](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/tlb.c)

**Proposed code: install a different tenant root.** This is the hardware portion of the common installer. Its caller already holds valid old and target uses, excludes migration, and disables physical IRQs.

```c
old = klet_read_stable_cpu_binding();
raw_spin_lock(&target->sync.lock);
cpumask_set_cpu(smp_processor_id(), target->sync.users);
generation = target->sync.generation;
raw_spin_unlock(&target->sync.lock);

klet_publish_switching();           /* NMI readers reject tenant access. */
klet_apply_admitted_arch_controls(target);
native_write_cr3(target->validated_cr3); /* flushing load, PCID disabled */
klet_invalidate_native_cached_mm();
klet_publish_foreign(target, generation);
klet_finish_old_departure(&old);    /* remove membership after CR3 changed */
return ROOT_RELOADED;
```

The publication helpers order the discriminator and payload for same-CPU NMI readers. The short target lock ends before the CR3 load. The old-root lock is acquired only during departure, after the new-root lock has ended.

The `vm_activate()` wrapper commits the carrier requirement after this installation. vOSTD keeps its virtual IRQ guard until it also updates its local active-space reference. The same-root branch returns `ROOT_RETAINED` only with valid architecture state and retained membership; it does not acknowledge an outstanding flush callback.

Linux starts [`switch_mm_irqs_off()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/tlb.c#L498) by reading `cpu_tlbstate.loaded_mm`. Adapt that function and other consumers before their first native dereference:

```c
/* Proposed dispatch, before native mm field access. */
switch (klet_loaded_domain()) {
case LOADED_FOREIGN:
        return klet_handle_foreign_predecessor(request);
case LOADED_SWITCHING:
        return klet_handle_transition_observer(request);
case LOADED_NATIVE:
        return native_mm_operation(request);
}
```

These branches depend on the operation. A delayed native user-mm flush can acknowledge actual departure; a kernel/global flush must still invalidate shared translations. A native installation must rebuild native state. A transition observer cannot access tenant user memory; ordinary maskable callbacks cannot observe that IRQ-disabled interval.

**Proposed code: return to a native Linux root.** Add this branch inside `arch/x86/mm/tlb.c`, where [`load_new_mm_cr3()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/tlb.c#L279) is available. The caller retains `native_mm` and excludes physical IRQs and migration.

```c
old = klet_read_stable_cpu_binding();
if (native_mm != &init_mm)
        cpumask_set_cpu(smp_processor_id(), mm_cpumask(native_mm));
generation = atomic64_read(&native_mm->context.tlb_gen);
lam = mm_lam_cr3_mask(native_mm);
klet_publish_switching();
load_new_mm_cr3(native_mm->pgd, 0, lam, true); /* no-PCID flushing load */
klet_publish_native_tracking(native_mm, generation, lam);
klet_restore_native_controls(native_mm);
klet_finish_old_departure(&old);
```

The publication helper rebuilds native slot identity, generation, lazy state, and LAM metadata before publishing the native discriminator. The control helper restores target RDPMC/LDT policy and required switch mitigations. This branch must load CR3 even if `native_mm` equals the carrier's previous native mm; the physical predecessor is foreign.

#### Carrier preemption and migration {#scheduling}

The saved continuation records the environment it actually needs. A scoped native Host operation preserves the selected tenant space while replacing that requirement; its exit restores the previous requirement. Scheduling uses this retained record before restoring the incoming stack.

| Incoming execution | Scheduler action |
|---|---|
| Kernelet continuation or admitted Host continuation requiring a tenant space | Install that retained root, including an authorized closing root needed to finish existing work |
| Carrier continuation inside a native Host scope | Restore its native environment; preserve the tenant selection separately |
| Ordinary Linux user task | Follow native mm switching with foreign-predecessor dispatch |
| Unrelated Linux kernel thread | Leave the foreign root and use the defined native kernel environment; do not create an implicit tenant-root borrower |

Preserve native `active_mm` references and accounting even when tenant execution uses a foreign root. Retaining CR3 can remove ordering that Linux otherwise obtains from a root load. The scheduler adapter supplies the required memory barrier and core synchronization for supported membarrier operations. [Context switch](https://elixir.bootlin.com/linux/v6.12/source/kernel/sched/core.c), [membarrier obligations](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/membarrier.h)

For example, Linux can suspend a carrier during a tenant-memory copy and resume it on another CPU. The installer restores that copy's retained space before its stack resumes. The copy does not need another explicit activation merely because the carrier migrated. A later vOSTD Task switch commits its own selected space before dependent Task code resumes.

**Proposed code: scheduler target selection.** Replace the architecture operation at Linux's existing [`context_switch()` mm-switch point](https://elixir.bootlin.com/linux/v6.12/source/kernel/sched/core.c#L5294). Keep its native reference-accounting branches around this operation.

```c
/* IRQs and migration excluded; next is the incoming task. */
if (klet_has_saved_requirement(next))
        effect = endovisor_install_execution_space_irqs_off(
                next, klet_saved_requirement(next));
else if (next->mm)
        effect = klet_install_native_mm_irqs_off(next, next->mm);
else if (klet_cpu_uses_foreign_root())
        effect = klet_install_native_mm_irqs_off(next, &init_mm);
else
        effect = klet_native_lazy_tlb_irqs_off(prev, next);

/* Still inside context_switch(), before its existing register/stack switch. */
klet_preserve_scheduler_ordering(effect, prev, next);
switch_mm_cid(rq, prev, next);
prepare_lock_switch(rq, next, rf);
switch_to(prev, next, prev);
```

The native helpers must accept a foreign predecessor and report the actual CR3 effect. `prev->active_mm` continues to describe native accounting; the CPU record identifies the physical predecessor. The saved requirement owns its root across suspension, including while the root is closing.

If CR3 is retained across different native mm identities, the ordering helper supplies `smp_mb()` before `switch_mm_cid()`. It also performs `sync_core()` when the incoming mm requires private expedited core synchronization. This replaces ordering otherwise supplied by the native switch. [Native CID ordering](https://elixir.bootlin.com/linux/v6.12/source/kernel/sched/sched.h), [x86 barrier assumption](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/barrier.h)

#### Publish edits and complete invalidation {#invalidation}

vOSTD performs ordinary user PTE stores locally and retains removed frames and nodes. Cursor results, reference transfer, and failure ownership keep their OSTD meanings. Required invalidation completes before affected storage can be reused; software-reader grace remains a separate condition.

1. Publish the edited PTEs before entering `vm_flush` for their space and validated range.
2. With physical IRQs enabled and preemption disabled, increment the root generation and snapshot its physical users under the root lock.
3. Release the lock. Send local and remote callbacks using the carrier's preallocated request and target mask.
4. Wait with physical IRQs enabled until every callback completes. The request retains its root throughout.
5. Return completion to vOSTD; only then discharge that hardware-invalidation obligation.

A callback takes no root lock and waits for no CPU. If its CPU has departed, it acknowledges departure; every later foreign-root entry flushes. If the requested generation is already covered, it returns. If exactly one generation is missing, it invalidates that request's range; a larger gap requires a full local flush. It advances only through the generation whose edits it has covered. [Native generation-gap rule](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/tlb.c#L827)

Admission and snapshot use the same root lock. A CPU admitted before the snapshot receives a callback. A later admission samples the new generation and flushes on entry. This closes the race without PCID retention. Generation and identifier exhaustion must enter trusted containment before wraparound.

Synchronous flush is legal in Linux task context, including a tenant virtual upcall after physical IRQ handling has ended. It is prohibited in Linux hardirq, softirq, NMI, or a flush callback. Service exclusion prevents reentry into the preallocated request. This preserves the existing synchronous boundary; it does not restore asynchronous shootdown overlap. [Service context](../blueprint/linux-mode/kernelet-api-service.md#depth), [SMP callbacks](https://elixir.bootlin.com/linux/v6.12/source/kernel/smp.c)

**Proposed code: publish one flush request.** The service has validated the range and retained its root. The caller's PTE stores precede this block; `request` and its mask are preallocated.

```c
lockdep_assert_irqs_enabled();
preempt_disable();
raw_spin_lock_irqsave(&root->sync.lock, flags);
if (root->sync.generation == U64_MAX) {
        raw_spin_unlock_irqrestore(&root->sync.lock, flags);
        preempt_enable();
        klet_stop_preserving_flush_owner(request); /* does not return */
}
request->generation = ++root->sync.generation;
cpumask_copy(request->targets, root->sync.users);
raw_spin_unlock_irqrestore(&root->sync.lock, flags);

on_each_cpu_mask(request->targets, klet_flush_cpu, request, true);
preempt_enable();
/* Request/root lifetime ends only after synchronous callbacks finish. */
```

Generation exhaustion selects containment before wraparound, after releasing the lock. The root lock serializes the generation increment with CPU admission. The synchronous call executes local and remote callbacks with physical IRQs available during the wait.

**Proposed code: callback decision.** Use the native [generation-gap rule](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/tlb.c#L827) with the foreign-root record.

```c
/* Physical IRQs disabled. No root lock, service call, or remote wait. */
loaded = klet_this_cpu_foreign_or_null();
if (!loaded || loaded->root != request->root)
        return; /* Actual departure; a later entry performs a flushing load. */
if (loaded->seen_generation >= request->generation)
        return;

if (!request->full &&
    request->generation == loaded->seen_generation + 1)
        klet_invlpg_range(request->start, request->end);
else
        native_write_cr3(__read_cr3());
loaded->seen_generation = request->generation;
```

`klet_invlpg_range()` issues `INVLPG` for each page in the validated range. A generation gap forces a full flush because this request does not identify the earlier ranges. Returning from `vm_flush()` permits the caller to complete hardware retirement; software-reader grace still precedes storage reuse.

#### Direct user access and exception recovery {#user-access}

Direct access requires the target space to be installed before the first user-VA instruction. The selected root alone is insufficient. Trusted vOSTD validates the user range and permitted operation, retains the needed objects, and uses admitted access routines. Mapping-dependent operations retain their own pins or exclusion; a root reference alone does not freeze its PTEs.

SMAP handling changes RFLAGS.AC at defined boundaries, not CR4.SMAP for the machine. Kernelet direct-access execution may require AC set. Entry into Linux clears AC; ordinary IRQ restoration preserves the interrupted flags. A redirect saves those flags, clears AC before Host handling, and restores the appropriate value only in the final continuation tail. Also normalize DF before ordinary Host C. [Entry flag handling](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/entry_64.S)

Register trusted copy/atomic recovery entries before exposing the image. Each identifies an instruction, allowed vectors, fixup address, and error/progress register convention.

On a matching fault, retain the original frame and capture CR2 before it can change. Bridge to the carrier stack, dispatch before Linux's native VMA lookup, and redirect the original frame to its validated fixup. Finish Host scopes and restore that frame. The fixup closes the access window and returns error/progress to vOSTD. Kernel proper can then resolve demand paging or COW and retry. [Native page-fault dispatch](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/fault.c)

This path can execute without an active user run. It must not resume an old `run_user()` continuation. `rep` copies and each atomic operation need their own progress/failure ABI. Unregistered private faults select instance stop; faults in Host operations retain native recovery or fatal policy.

**Proposed code: access and recovery.** Replace the Linux backend's [software-walk reader/writer](../blueprint/linux-mode/virtualizing-ostd/memory.md) with a trusted direct-access implementation. Its range checks and object ownership remain required.

```text
trusted vOSTD copy, with its space retained and installed:
    validate user range and operation
    establish the admitted AC state
copy_site:                         // registered exact instruction range
    copy through user virtual addresses; maintain defined progress registers
    close this access window; return success
copy_fixup:                        // registered target, not caller-selected
    close this access window; return error and partial progress

Host fault wrapper, on the carrier stack:
    capture original frame, vector, error, and CR2 before they can change
    match carrier, operation, space, and trusted recovery entry
    if matched: redirect the original frame to copy_fixup
    otherwise: select the defined private-fault or Host-fault path
    finish Host scopes; commit frame return or stop; execute that fixed tail
```

Linux's [`exc_page_fault()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/fault.c) captures the fault address before normal dispatch. Its [`fixup_exception()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/extable.c) shows instruction-specific recovery. The new classifier must run before native tenant-address resolution through `current->mm`.

The entry bridge saves interrupted flags, executes `CLAC` and `CLD` before ordinary Host C, and restores permitted flags at the final tail. The access helper returns to its copy caller; that caller can request tenant fault handling and retry. Neither path borrows an unrelated user-run continuation.

### Execution protocol {#execution-implementation}

#### Bind, admit, and complete one user run {#direct-transition}

The outer OSTD `execute()` loop can perform several internal runs. Bind state to each internal `run_user()` invocation, not to an indefinite carrier slot. The run exclusively borrows its user-register backing and retains the real call-preserved registers and kernel stack pointer. Host suspension or migration does not end this binding.

**Existing code: the binding point.** The pinned [OSTD execution loop](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/arch/x86/cpu/context/mod.rs#L302) contains:

```rust
let guard = crate::irq::disable_local();
hooks.pre_user_run(&guard);
self.user_context.run(guard);
```

The proposed vOSTD `run()` calls the `user_run(ctx)` service at this boundary. Its entry stub records the real suspended call before entering ordinary Host C. The run record connects that continuation to the exclusively borrowed user backing:

```c
/* Proposed fields; layout, offsets, and complete ownership are ABI work. */
struct klet_run {
        struct klet_user_ctx *borrowed_ctx;
        struct kernel_continuation kernel_return; /* SP and preserved registers */
        struct space_use required_space;
        unsigned long kernel_rflags;
        unsigned int held_mirror; /* the owned zero-or-one contribution */
        uint64_t generation;
};
```

The Host retains this record across carrier scheduling. The entry stub saves the caller's continuation; it must not save only a temporary C helper's return address.

Use a Host-owned atomic vCPU word containing phase, pending-event bits, and sticky STOP. Payloads live in retained queues or counters. Phase updates use compare/exchange operations that preserve concurrent event bits. One short instance gate serializes first start, final admission, final kernel resumption, and the instance stop transition. NMI never takes that gate.

The proposed ordinary path is:

1. Hold the virtual IRQ guard, validate private context backing, and retain the run, selected space, and kernel continuation.
2. Under physical IRQ exclusion, transfer the owned preemption-mirror contribution into the run and remove that contribution from Linux's count.
3. Enter `HOST_RUN`. Establish the carrier stack and legal IRQ state before schedulable Host preparation.
4. Complete Host work, validation, root preparation, and any required FPU synchronization.
5. Disable physical IRQs and migration. Process required deferred wakeups and recheck supported Linux work flags. If work requires scheduling, return to Host preparation and repeat this check. Otherwise reacquire current-CPU state and prepare its capture destination, root, bases, and final register state.
6. Under the instance gate, require `RUNNING` and atomically change `HOST_RUN` to `ADMITTED` only when pending bits and STOP are clear.
7. Release the gate. Perform final nonscheduling user-departure accounting and a dedicated checked IRET tail; user IF is enabled at the privilege transition.
8. On ordinary user entry, leave `ADMITTED` before schedulable processing, capture the event, and complete required Host scopes.
9. Enter `COMPLETE` and retire capture/readers. With physical IRQs excluded, acquire the instance gate and require `RUNNING`. Restore the run's held mirror, then CAS to `KERNEL` with STOP clear and pending bits preserved. If completion fails terminally, remove any restored mirror before Host cleanup.
10. Release the gate, prepare IRQ/tracing state for the committed destination, and execute its fixed tail. Normal completion restores the retained kernel call with its physical IF; terminal completion joins stop. End each borrow only after its final consumer finishes.

Host preparation calls return before the final assembly tail; no ordinary Host C activation stays suspended across tenant user execution. The final tail carries the retained descriptor independently of those C locals. Its storage survives until assembly's last access. Returning from an internal run does not release the Task's still-active kernel stack.

A pending event before step 6 rejects user admission without claiming that user execution occurred. Publication after admission must force observation by a physical kick. Preparation failure completes the retained call through its defined error/event result; terminal failure cancels it through stop. An aborted departure restores partial Host bookkeeping before allowing scheduling and requires a fresh admission attempt.

Linux work flags and tenant pending bits are different inputs. The vCPU CAS cannot replace Linux's IRQ-off final work recheck. Use a carrier-aware adapter for the supported work loop and frame consumers; do not route tenant syscalls into native Linux syscall policy. [Native work-loop recheck](https://elixir.bootlin.com/linux/v6.12/source/kernel/entry/common.c#L83)

The run carries the removed [mirror contribution](#retained-state) across migration. Reject unrelated Host nesting; never clear hardirq, softirq, or another Host critical section to make a transition appear legal. Kernel completion restores physical IF separately from the still-held virtual IRQ guard. [Linux entry ordering](https://docs.kernel.org/6.12/core-api/entry.html)

Normal completion retries a CAS changed only by ordinary pending bits, preserving those bits and the single restored mirror contribution. Pending work may accompany a kernel return, but cannot bypass user admission. Release the gate's own preemption contribution before the fixed tail. Stop after commitment uses the kernel interception path; it cannot change a destination whose IRQ/tracing state is already prepared.

**Proposed code: final user admission.** Insert this decision after the carrier-aware [Linux work loop](https://elixir.bootlin.com/linux/v6.12/source/kernel/entry/common.c#L90) has rechecked native work with physical IRQs disabled. Root installation, restore validation, and CPU capture preparation have already succeeded.

```c
/* IRQs/migration excluded; only bounded operations under the gate. */
bool admitted = false;
raw_spin_lock(&sandbox->gate);
unsigned long word = atomic_long_read(&vcpu->control);
if (sandbox->state == RUNNING && klet_phase(word) == HOST_RUN &&
    !(word & (KLET_PENDING_MASK | KLET_STOP))) {
        unsigned long desired = klet_with_phase(word, ADMITTED);
        admitted = atomic_long_try_cmpxchg(&vcpu->control, &word, desired);
}
raw_spin_unlock(&sandbox->gate);
/* Return to the assembly caller before its final departure tail. */
```

The full-order compare/exchange preserves event publication ordering. A failed CAS requires classification of the observed word: pending work completes this run without entry; STOP selects terminal completion. A phase mismatch is a protocol failure. A successful CAS permits only the prepared nonscheduling departure tail.

**Proposed code: complete the suspended call.** This ordering sketch expands the normal completion decision; it is not a second admission to user mode.

```text
finish Host scopes and capture readers; enter COMPLETE
disable physical IRQs; acquire instance gate
if sandbox is stopping: choose terminal completion
otherwise:
    restore the held mirror once; record its transfer
    CAS COMPLETE -> KERNEL, preserving pending bits and requiring STOP clear
    retry only an ordinary pending-bit change, without restoring mirror again
    on terminal failure: remove the restored owned mirror
release instance gate, including its own preemption contribution
return from Host C with a fixed kernel-return or terminal disposition
assembly restores kernel_return and kernel_rflags, or enters terminal Host code
retire the run storage only after the selected tail's final access
```

The mirror transfer uses Linux preemption-count arithmetic under the stated exclusion. It moves only the recorded contribution; unrelated Host nesting makes the transition invalid. Restoring physical IF is separate from releasing the virtual IRQ guard in the resumed vOSTD call.

#### Entry capture and complete routing {#handoff-protocol}

The carrier remains the authority for the active run. A per-CPU prepared capture pointer accelerates entry only while its carrier and generation match. Schedule-out retires that publication; destination-CPU preparation installs a fresh one. Clearing the pointer does not end accesses through a pointer already loaded by assembly.

For ordinary SYSCALL, entry preserves scratch values and the tenant syscall number, then PUSHes registers into their final backing. Linux C processing receives that frame explicitly on the carrier stack. Preserve the syscall number before native RAX initialization; use the no-syscall sentinel only where its native meaning applies. Other entries may retain a native frame and copy it into the run's context. Hardware-frame transfers and early scratch stores remain. [Native syscall capture](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/entry_64.S#L83)

| Entry or operation | Retained state and processing | Permitted destination |
|---|---|---|
| First carrier start | Initialized Host carrier, stacks, root requirement, and registered image entries; startup C returns normally | Assembly consumes a checked start descriptor, or joins stop |
| Tenant SYSCALL | Direct capture into the matching run; tenant syscall policy stays in kernel proper | Complete that run with a syscall event |
| Tenant user exception or IRQ | Preserve native frame and vector metadata; finish vector-specific Host work | Complete the matching run; outer `execute()` handles pending work and decides whether to run again |
| Kernelet kernel IRQ | Preserve the exact interrupted Task frame; bridge to carrier C stack; finish IRQ/softirq and scheduling | Restore that frame, enter the permitted upcall, or stop |
| Host service | Retain service and required memory environment; finish normal cleanup | Return to its caller or stop after Host scopes close |
| Registered kernel-copy fault | Retain space, original frame, recovery entry, and partial progress | Restore the registered fixup, or stop |
| Kernel-origin NMI | Preserve the interrupted instruction, root state, and native NMI frame independently | Resume the interrupted bounded transition |
| Tenant user NMI | Retain invocation, native NMI frame, and root/view return token | Return to the same admission; defer stop to ordinary interception |
| Recognized user-return instruction fault | Preserve exact failing-PC provenance before native frame rewriting; repair Host state | Complete the matching run with a backend fault, or stop |
| Unrecoverable private kernelet fault or panic | Close instance admission; finish Host exception/service scopes | Trusted terminal Host path |

Saved CS identifies the frame's privilege origin. It does not identify a synthetic restore-failure frame or its intended destination by itself. Match exceptional completion to its exact frame, invocation, and generation. Kernel-origin events must not consume an unrelated old user run. Register supported alternate syscall entries, including reachable `int $0x80`, with explicit rejection routes; exclude legacy vsyscall emulation for this profile. [Native entry and bad-IRET repair](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/entry_64.S), [trap wrappers](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/traps.c)

Keep kernelet CPL0 execution in Linux's kernel-context tracking. A return to its kernel continuation closes the applicable IRQ or exception scopes without performing user-departure bookkeeping. Only an actual CPL3 return performs that departure. Preserve each vector's RCU, tracing, and instrumentation ordering; a saved user CS alone cannot select the final bookkeeping path. [Linux entry contract](https://docs.kernel.org/6.12/core-api/entry.html)

**Existing code: syscall frame construction.** Linux's [`entry_SYSCALL_64`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/entry_64.S#L100) includes these operations after constructing the return frame:

```asm
pushq   %rax                     /* pt_regs->orig_ax */
PUSH_AND_CLEAR_REGS rax=$-ENOSYS
```

**Proposed code: change the capture destination.** The trusted entry branch must preserve all incoming values before using scratch registers. The following labels describe new assembly operations, not existing Linux macros:

```text
tenant_SYSCALL_entry:
    establish Host GS; preserve user RSP in prepared scratch
    identify the prepared carrier/run/generation without losing tenant values
    select the top of that run's final register backing
    save the return frame, syscall number, and all required GPRs
    capture effective user FS/GS and other required entry state
    select the carrier C stack; pass the captured frame explicitly
    call the carrier-aware syscall-entry handler
    after C returns, execute its committed run-completion or stop tail
```

The destination change removes the later copy into the run's context. Linux C still uses the carrier stack. Readers such as tracing and diagnostics must consume the explicit frame; `task_pt_regs(current)` cannot silently select another copy.

Before user departure, the current CPU publishes `{carrier, run, generation, capture_top}` from retained storage. Schedule-out withdraws that publication before another carrier uses the CPU. Schedule-in restores the continuation first; final user preparation then publishes the destination on its current CPU. A loaded pointer remains a reader until its last assembly access, even after publication clears.

First start uses the same disposition rule. A proposed [`ret_from_fork` adapter](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/process.c) returns a checked `START` or `STOP` descriptor from initialization C. Assembly consumes it only after C returns. User IRQs and exceptions use their vector wrappers and complete the matching run; kernel IRQs retain their separate interrupted frame.

#### Host services, interrupts, and upcalls {#host-boundaries}

Ordinary Linux C services use the retained carrier stack. An audited leaf service can keep the kernelet stack. It must use mapped storage, perform no schedulable work, and preserve the same entry/exit contract. Root installation is a candidate for this restricted path. Every service records in-progress ownership before interruption can expose a partially committed operation.

For a kernel IRQ, save the original frame on its Task stack and bridge before calling ordinary Host C. Keep the register argument pointing to that original frame. Physical handlers and softirqs finish before carrier scheduling. At the legal task-context checkpoint, forced yield temporarily removes only the owned mirror and calls `schedule()` even without another runnable task. Native conditional preemption remains separately applicable; unrelated Host atomic scopes still prohibit scheduling. [IRQ exit](https://elixir.bootlin.com/linux/v6.12/source/kernel/entry/common.c), [scheduler entry points](https://elixir.bootlin.com/linux/v6.12/source/kernel/sched/core.c)

The interrupted stack must have mapped, writable space for the initial frame and bridge instructions. Retain the Blueprint's [stack-reserve and build checks](../blueprint/linux-mode/faults-and-reclamation.md#stack) until a replacement has been validated. Check upcall stack space before delivery as well. Switching to the carrier stack later does not protect the earlier pushes; the Blueprint's reserve argument remains **[unverified]**.

Restore the mirror as required by the committed kernel destination. Commit resume, upcall, or stop before preparing its final tracing/IRQ state. Assembly executes that fixed choice after C returns. A kernel IRQ without an active user run uses its own bridge ownership; it does not invent a run-level mirror transfer.

The upcall stub first saves a complete Task-owned continuation after the Host bridge retires. Only then may virtual handlers run or the inner scheduler select another Task. Virtual IRQ guards defer ordinary upcalls through activation and other protected transactions. Disabling inner preemption alone does not disable virtual interrupts; handlers retain the native task-association and locking rules. Incorrect use may terminate the sandbox, but must not corrupt Host state. [Virtual IRQ contract](../blueprint/linux-mode/virtualizing-ostd/scheduling.md#upcall)

Event producers store payload before publishing the pending bit. A producer observing `ADMITTED` must kick the carrier; a backend-owned sleeper also needs a wakeup. NMI publishes through preallocated local HARD `irq_work`, which later performs any remote kick or stop-gate operation. Keep notification objects alive through callback completion. Consumers atomically claim bits before draining payloads, preserving concurrent publication and sticky STOP. [KVM request precedent](https://docs.kernel.org/6.12/virt/kvm/vcpu-requests.html), [Linux IRQ work](https://elixir.bootlin.com/linux/v6.12/source/kernel/irq_work.c)

Host work must have a supported consumer contract. Fatal-signal detection returns a decision to the completion path; it cannot exit while borrowed frames or Host scopes remain live. Backend-owned idle waits register their wait before checking durable stop/event state. Arbitrary uninterruptible Host I/O still requires actual completion or a subsystem cancellation mechanism. A proposed external-control-signal filter must act before send-time group-exit/job-control mutations and distinguish trusted internal faults from external senders. [Signal processing](https://elixir.bootlin.com/linux/v6.12/source/kernel/signal.c), [wait contracts](https://elixir.bootlin.com/linux/v6.12/source/include/linux/wait.h)

**Proposed code: service and upcall exclusion.** Extend the existing [service-entry bridge](../blueprint/linux-mode/kernelet-api-service.md). The trusted entry state spans the complete operation, including its return decision.

```text
vOSTD: check this service's guard requirements; protect its local transaction
Host entry: authenticate current carrier; retain its service/root ownership
Host entry: mark service in progress before a partial operation is observable
    audited bounded leaf: perform its operation on mapped retained storage
    ordinary Host C: switch to carrier stack; perform operation and cleanup
Host epilogue: finish C scopes; commit return or stop; return to assembly
assembly: restore required root/flags/stack and retire bridge use at last access
vOSTD return: commit local state; release the transaction's required guard
```

A physical IRQ can occur during a schedulable service. Its native handler can run, but the in-progress service state prevents upcall delivery into unfinished Host work. Activation also holds the virtual IRQ guard through its local commit. Blocking services must meet their guard restrictions; they cannot acquire a virtual IRQ guard that their own contract prohibits. A service that uses the preallocated flush request cannot reenter that request through a virtual callback.

**Proposed code: kernel IRQ completion.** The extension belongs at the [native kernel IRQ exit](https://elixir.bootlin.com/linux/v6.12/source/kernel/entry/common.c), after handler and softirq scopes close.

```text
retain exact interrupted Task frame; bridge to carrier stack for Host C
finish physical IRQ/softirq processing
at a legal task-context checkpoint:
    if forced yield:
        hold only the owned mirror in the bridge; remove that contribution
        call schedule() unconditionally with the required native IRQ state
        restore that contribution if the selected kernel return requires it
    otherwise: perform permitted native conditional scheduling
with physical IRQs excluded, commit resume/upcall/stop under instance gate
prepare tracing and flags for that fixed destination; return from Host C
assembly retires the Host bridge before entering the Task's upcall stub
upcall stub saves a Task-owned continuation before callbacks or Task switching
```

Nested IRQs match their own frames and cannot consume the outer bridge's decision. Disabling inner preemption permits virtual IRQ delivery; the handler still obeys native Task association and locking rules. The virtual IRQ guard defers callbacks when the protected operation requires that exclusion.

**Proposed code: durable event publication.** The producer retains the event queue, vCPU, and notification objects through publication and notification.

```c
klet_enqueue_payload_retained(vcpu, payload);
old = atomic_long_fetch_or(event_bit, &vcpu->control);
if (in_nmi())
        klet_queue_preallocated_local_hard_irq_work(vcpu);
else
        klet_notify_observed_phase(vcpu, klet_phase(old));

/* Designated consumer; claim before drain, preserving phase and STOP. */
claimed = atomic_long_fetch_andnot(event_mask, &vcpu->control) & event_mask;
klet_drain_claimed_payloads(vcpu, claimed);
```

`klet_notify_observed_phase()` kicks `ADMITTED` execution, applies the kernel notification policy in `KERNEL`, and wakes a backend-owned sleeper. Other preparation/completion phases retain the bit for their next check. A local admitted producer uses HARD IRQ work because `kick_process()` does not supply a self-kick. The [IRQ-work callback](https://elixir.bootlin.com/linux/v6.12/source/kernel/irq_work.c) drains retained targets and performs required remote kicks; it cannot discard a concurrent target publication.

The idle path can use Linux's [wait-event pattern](https://elixir.bootlin.com/linux/v6.12/source/include/linux/wait.h), which registers the waiter before checking the condition:

```c
/* Proposed backend-owned wait; native interrupt/error results need cleanup. */
result = wait_event_killable(carrier->wait,
        klet_stop_requested(carrier) || klet_events_pending(carrier) ||
        klet_retirement_ready(carrier) || klet_deadline_expired(carrier));
return klet_finish_wait_scope(carrier, result);
```

All predicate fields are durable and read with their required ordering. Stop or event publication also wakes this wait. An armed deadline timer publishes expiry and wakes the retained carrier. Wait cleanup cancels the timer and joins its callback before releasing timer or wait storage. A fatal-signal result requests instance stop and still passes through service cleanup. This wrapper does not cancel a subsystem's unrelated uninterruptible I/O.

**Proposed code: observe Host death without abandoning the run.** Add a returning probe to the carrier-aware work loop before final admission. Its caller is a live attached carrier with retained signal state.

```c
spin_lock_irqsave(&current->sighand->siglock, flags);
killed = (current->signal->flags & SIGNAL_GROUP_EXIT) ||
         sigismember(&current->pending.signal, SIGKILL) ||
         sigismember(&current->signal->shared_pending.signal, SIGKILL);
spin_unlock_irqrestore(&current->sighand->siglock, flags);
if (killed) {
        klet_request_instance_stop();
        return KLET_STOP_AFTER_CLEANUP;
}
```

The probe leaves the fatal signal pending. The caller exits its retry loop, closes Host scopes, and reaches terminal completion. Only after retiring execution may Linux's nonreturning `do_group_exit()` path run. Native [`get_signal()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/signal.c) can stop or exit the task; calling it inside an unfinished borrowed-frame scope would bypass that completion. Nonfatal-signal filtering and supported task-work callbacks still require the declared Host-work inventory.

#### Time, tick delivery, and idle deadlines {#time-protocol}

The proposed backend uses Linux's clock and process tick. It does not require `CONFIG_HZ=1000` or add a timer solely to preserve native OSTD's fixed frequency. This changes the virtual TICK source from the Blueprint's [per-carrier watch timer](../blueprint/linux-mode/virtualizing-ostd/tasks.md#watch). It does not remove that timer's separate supervision duties.

Three quantities need separate treatment:

| Quantity | Required meaning |
|---|---|
| Elapsed time | Read Host time; it advances while a carrier is suspended or idle |
| Tick notification | Request virtual callback processing; a pending bit can represent several published ticks |
| CPU accounting | Preserve the admitted accounting units and Task attribution; elapsed wall time or callback count alone is insufficient |

Native OSTD defines a compile-time [`TIMER_FREQ`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/timer/mod.rs). [`Jiffies::as_duration()`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/timer/jiffies.rs) uses it for conversion. Update the constant consumers and validate their supported frequencies; a runtime field alone cannot change compiled expressions. Audit callback periods, [load statistics](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/sched/stats/loadavg.rs), [vDSO updates](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/comps/time/src/tsc.rs), [procfs conversions](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/fs/fs_impls/procfs/pid/task/stat.rs), and [per-tick CPU accounting](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/time/cpu_time_stats.rs) together. Retain count-dependent inputs in counters when notifications coalesce.

Linux's [`update_process_times()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/time/timer.c) provides a process-tick hook. The hook identifies the running carrier and publishes retained tick data before the pending bit. It invokes no image callback in physical interrupt context. An unrelated hardware interrupt does not create a virtual tick.

**Proposed code: publish and consume a tick.** Extend the existing [virtual-event path](../blueprint/linux-mode/virtualizing-ostd/interrupts-and-time.md), preserving the event ordering specified above.

```text
Host process-tick hook:
    identify current carrier and retain its virtual-event record
    update the admitted tick/accounting payload
    publish TICK through the durable event protocol
    finish native physical interrupt processing

vOSTD event processing, after the Host bridge has ended:
    hold the virtual IRQ guard; claim pending events before draining payloads
    read Host elapsed time and consume the retained tick/accounting payload
    run adapted virtual timer callbacks on the Task stack
    retain the outer execution loop's event and scheduling checks
    release the guard at the API-defined boundary
```

The payload and its consumers must agree on aggregation and Task attribution. Do not charge all delayed notifications to whichever Task happens to consume them. The native [`timer callback`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/timer/mod.rs) updates jiffies from callbacks, so unchanged reuse would violate the elapsed-time contract. Timer delivery can request scheduling; it does not require an inner Task switch after every physical interrupt.

On full-dynticks CPUs, a running carrier that requires periodic callbacks needs an explicit task tick dependency. Add its bit and recognition to Linux's [`check_tick_dependency()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/time/tick-sched.c); setting an unknown bit is insufficient. Install the dependency before execution and retain it through carrier scheduling. Idle deadlines still use Host timers and the [stop-aware wait](#host-boundaries); they cannot depend on a tick reaching a sleeping carrier.

Retain the watch mechanism's forced-yield and guard-deferral checks until equivalent supervision is implemented. Host-tick sampling does not reproduce accumulated carrier-runtime deadlines. The clock and tick interfaces above specify the adaptation boundary; consumer conversion and accounting remain implementation obligations, not measured behavior.

#### CPU-state ownership and restore validation {#state-protocol}

Keep each tenant FPU snapshot separate from the carrier's Linux-managed active FPU object. Linux owns physical residency across Host switches. A vOSTD save synchronizes the latest carrier state and exports it; a load validates and imports the snapshot, invalidates old residency, and restores valid ownership. Neither call retains the snapshot pointer afterward. This scheme preserves snapshot copies where required. [Linux FPU state](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/fpu/core.c), [return restoration](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/fpu/context.h)

An inner Task switch preserves outgoing state before that Task can run elsewhere. Its incoming hook imports state when required by `CpuSync`. A same-Task syscall needs no unconditional export/import. Every final user departure still checks Linux's pending reload after the last schedulable Host work, including when the inner hook skipped a load. `CpuSync` logical residency cannot mean that the physical registers stayed untouched by Host scheduling.

Local scheduling exclusion spans snapshot synchronization and the `CpuSync` state update. `get()` synchronizes without detaching the Task. `set()` makes its replacement snapshot authoritative; a later unconditional save must not overwrite it with stale carrier state. The next required import installs that replacement before user execution.

Initialize carrier FPU state, effective user bases, and admitted extended state before any tenant callback or independent save. Trusted code uses no tenant vector registers except through designated state operations. Private save areas remain resident, aligned, and unavailable through writable user aliases during trusted use. Hold the source borrow through validation/restoration and the destination borrow through save completion. [Current FPU contract](../blueprint/linux-mode/virtualizing-ostd/user-mode.md#fpu), [image instruction audit](../blueprint/linux-mode/builds-and-images.md#audit)

| State | Proposed transfer and validation rule |
|---|---|
| GPRs, RIP/RSP, flags, CS/SS | Keep Task-owned backing. Require canonical admitted user addresses and fixed user CS/SS. Clear excess privilege/reserved flags; force user IF and the fixed flag bit. Preserve supported application flags. |
| FS/GS | Capture effective user bases before scheduling, including FSGSBASE writes. Preserve active Host GS. Capture submitted scalars once; replace noncanonical bases with zero and synchronize carrier metadata. |
| DS/ES and segment mode | Admit only the declared 64-bit flat-segment contract. Define its initialization and transfer rules before exposing a corresponding API; reject unsupported segment modes. |
| FPU/XSAVE | Use a verified import/export format and admitted component mask. Validate extent, alignment, reserved fields, and MXCSR against the Host mask. Initialize bytes that save instructions can leave unwritten. |
| PKRU, when enabled | Save each Task's actual value before replacing it. Use the defined Host working value during Host work; install the selected Task value at final entry. Tenant pkey allocation needs its own mapping contract. |
| XFD and other admitted extended controls | Update saved metadata, CPU cache, and hardware consistently. Enforce the supported component policy; do not inherit another Task's permissions or readiness. |

For standard XSAVE, require zero `XCOMP_BV`, zero reserved header fields, and `XSTATE_BV` within the enabled admitted mask. Other formats require explicit conversion. A malformed FPU image can use the immutable trusted initial image without changing the caller's bytes. This fallback must itself contain valid MXCSR and component state. [XSAVE conversion](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/fpu/xstate.c)

Privileged return validation occurs after the last writer. Invalid return addresses or unsupported control state select trusted failure completion or stop; unmapped but valid user addresses can fault normally after entry. Use the dedicated IRET path for the initial protocol. A SYSRET optimization requires separate eligibility and restore-fault treatment.

**Proposed code: validate the bound return frame.** The trusted code holds the exclusive context borrow until restoration ends. These predicates implement the admitted architecture policy; their exact masks remain part of the declared interface work.

```c
/* Scalar capture from stable backing; no second complete GPR image. */
rip = READ_ONCE(ctx->rip);
rsp = READ_ONCE(ctx->rsp);
if (!klet_admitted_user_address(rip) || !klet_admitted_user_address(rsp))
        return KLET_BACKEND_FAULT;
ctx->rflags = klet_filter_application_flags(ctx->rflags) |
              X86_EFLAGS_IF | X86_EFLAGS_FIXED;
ctx->cs = __USER_CS;
ctx->ss = __USER_DS;
/* Checked FS/GS and extended-state preparation must also succeed. */
```

The filter removes privilege and unsupported bits while preserving admitted application behavior. Fixing selectors does not validate arbitrary segment modes. Effective FS/GS capture must follow the CPU state, including FSGSBASE writes, as Linux's [`save_fsgs()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/process_64.c) does. The adapter preserves Host GS and synchronizes carrier metadata before another Host switch.

**Proposed code: FPU import and export.** Put these wrappers inside Linux's FPU core. Reuse the lock-held synchronization in [`fpu_sync_fpstate()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/fpu/core.c#L459) and the [UABI conversion helpers](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/fpu/xstate.c). The source and destination are pinned private buffers; scratch allocation finishes before protection begins.

```c
/* Import into the current carrier; named klet helpers are proposed. */
fpregs_lock();
error = klet_convert_validate_to_scratch(snapshot, len, carrier_fpu, scratch);
if (!error) {
        klet_commit_admitted_components(carrier_fpu, scratch);
        __fpu_invalidate_fpregs_state(carrier_fpu);
        set_thread_flag(TIF_NEED_FPU_LOAD);
}
fpregs_unlock();
/* Conversion failure leaves carrier state unchanged. No pointer is retained. */

/* Export; share native lock-held logic rather than nesting its public lock. */
fpregs_lock();
if (!test_thread_flag(TIF_NEED_FPU_LOAD))
        save_fpregs_to_fpstate(carrier_fpu);
klet_export_admitted_snapshot(carrier_fpu, destination, len);
fpregs_unlock();
```

The importer preserves carrier-owned metadata and commits only after complete conversion. Replacing memory invalidates the carrier's cached register identity; setting a reload flag alone can leave the old registers classified as valid. [Linux validity test](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/fpu/context.h#L36)

Connect export/import to [`CpuSync::before_schedule()` and `before_user_exec()`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/kernel/core/src/process/posix_thread/cpu_sync.rs). `get()` can export without detaching; `set()` makes the new snapshot authoritative. The wrapper must not export stale carrier state over that replacement. Conversion rejection follows the declared fallback or stop policy before logical state can be marked ready.

After the last schedulable Host work, the final user-entry adapter repeats the physical reload check:

```c
/* IRQs/migration excluded; before admission and the final IRET tail. */
if (test_thread_flag(TIF_NEED_FPU_LOAD) &&
    klet_restore_carrier_fpu_checked() != 0)
        return KLET_BACKEND_FAULT;
```

The checked helper is new; Linux's [`switch_fpu_return()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/fpu/core.c#L782) returns `void`. PKRU follows a separate path: capture the Task value, establish the Host working value, then install the selected Task value at final entry. Admitted XFD and other controls require matching saved metadata, CPU cache, and hardware updates.

#### Restore faults and exceptional containment {#kernel-exception-audit}

Validation complements instruction-specific fault recovery. A checked FPU restore must report failure; Linux's ordinary void restore helper can reset state and continue. Linux already provides [`os_xrstor_safe()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/fpu/xstate.h), which returns an exception result. The proposed core wrapper can use it for the admitted XSAVE format and add ownership repair. Other restore instructions require corresponding checked operations.

On failure, repair partially modified registers, invalidate ownership, and leave trusted reload pending before releasing protection. Withhold user entry. Failed base writes similarly preserve Host GS and consistent saved fields. [Exception-table handlers](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/extable.c)

Invalidate Linux's CPU ownership indication and the carrier FPU object's `last_cpu`, as well as the rejected image's logical readiness. Restore a trusted initial hardware state before scheduling becomes possible. An active run receives `BackendFault` and requires a validated replacement or stop before another admission.

An independent infallible `FpuContext::save/load` wrapper must take the non-returning instance-stop path after Host cleanup. It cannot return and let `CpuSync` mark failed state ready. If trusted reset fails, use Host failure policy. A software-input fallback does not convert a hardware restore fault into successful completion.

Entry/return recovery recognizes exact instruction provenance before treating a frame as tenant state. It establishes trusted GS, stack, and mappings before ordinary handling. Shared layout alone does not make `task_pt_regs()`, unwinding, diagnostics, or tracing use the right frame. Admit only synchronized readers of the explicit authoritative frame and end their access before reuse.

Machine checks retain Linux's severity and fatal-policy decisions. A tenant action-required path needs a retained request, execution hold, and process-context receiver. That receiver must produce either completed memory repair with required invalidation or instance stop. Grant ownership lookup, affected-space enumeration, and physical quarantine require a memory-repair adapter; parking alone is not recovery. This recovery extension remains outside the completed initial profile. [Native machine-check handling](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/cpu/mce/core.c)

**Proposed code: contain a recognized FPU restore failure.** Use the [native checked restore primitive](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/fpu/xstate.h) inside a protected wrapper. The wrapper must propagate its result and repair ownership before allowing scheduling.

```c
/* FPU/transition protection and stable source ownership remain held. */
error = klet_restore_fpregs_checked(carrier_fpu->fpstate, admitted_mask);
if (error) {
        __cpu_invalidate_fpregs_state();
        __fpu_invalidate_fpregs_state(carrier_fpu);
        set_thread_flag(TIF_NEED_FPU_LOAD);
        klet_mark_logical_state_rejected(run_or_operation);
        if (klet_reset_hardware_from_trusted_initial_state() != 0)
                klet_native_fatal_restore_failure(); /* does not return */
        return KLET_BACKEND_FAULT;
}
/* Only successful checked restoration may publish valid ownership. */
```

The trusted reset repairs hardware before scheduling becomes possible; it does not authorize reuse of the rejected logical image. An active run receives the fault result through normal completion. An independent infallible state operation requests terminal cleanup instead of returning to a caller that would mark failed state ready.

For IRET and segment/base faults, preserve the original failing instruction and its registered operation before Linux rewrites the frame. Extend [`fixup_bad_iret()` integration](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/traps.c) with an exact operation token:

```text
recognized restore PC + matching frame/run/generation
    -> establish trusted GS, stack, root, and entry bookkeeping
    -> repair partial state and record BackendFault
    -> finish the native exception wrapper
    -> assembly completes that run or takes the committed stop path
```

An arbitrary Host fault cannot enter this route merely because a carrier is current. Recoverable machine-check memory repair remains outside the initial profile; its worker and quarantine adapter must exist before that recovery path is admitted.

#### NMI entry and return {#nmi-protocol}

NMI entry retains Linux's dedicated stack and nesting discipline. A user-origin token names the invocation, exact native frame, interrupted root/view, and required return environment. It borrows an already retained root use without acquiring a lock that interrupted code may hold. The bounded NMI return restores the same admission and capture destination. [Native NMI entry](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/entry_64.S), [NMI scopes](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/nmi.c)

During `Switching`, a kernel-origin NMI uses only shared Host mappings and returns to the interrupted transition. It cannot treat the partial CPU record as a native mm or authorize tenant user-memory access.

A `#GP`, `#SS`, or `#NP` at the dedicated NMI IRET can use its exact instruction, frame, invocation, and generation token. Preserve that provenance before native bad-IRET rewriting. Recover only after native NMI C scopes have ended; other faults or damaged recovery storage retain Host failure policy. Nested kernel NMIs use separate records and return to the interrupted recovery. Stop remains pending until ordinary interception completes withdrawal; neither NMI return nor a pending token permits reclamation.

**Proposed code: retain one NMI return.** Extend the [native NMI assembly](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/entry/entry_64.S) without overwriting its IST nesting storage. The token uses preallocated, separately retained storage.

```text
user-origin NMI:
    preserve native frame and actual root; bind run/frame/generation token
    establish trusted Host entry state using the retained root
    move to the reviewed carrier-stack frame; call exc_nmi(frame)
    wait for ordinary C return, including native NMI-scope cleanup
    restore the token's environment and this NMI frame's registers
tenant_nmi_iret:
    IRET

kernel-origin NMI:
    preserve an independent frame and return to the interrupted instruction
    do not consume an outer user-run or NMI-return token
```

[`exc_nmi()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/kernel/nmi.c) retains native nesting and bookkeeping. The full-root profile normally keeps the same root throughout this path. The token borrows existing ownership; it does not take the interrupted code's locks or wait for another CPU.

Only the exact `tenant_nmi_iret` failure can use this token for exceptional completion. A successful return preserves the current admission; it does not run a new schedulable admission loop. A pending STOP requires ordinary interception afterward, so the token cannot acknowledge quiescence.

### Resource lifetime {#lifetime-implementation}

#### Retire one space while the sandbox runs {#space-retirement}

Final `VmSpace` Drop transfers the complete tree, mapped owners, `IoMem` pins, and identifier into a preallocated retire node. It performs no Host call, allocation, or immediate field destruction. A suitable task-context drainer calls `vm_destroy`. The first call closes ordinary acquisition; existing trusted uses may still reinstall the root to finish admitted Host work.

Count operational uses separately from registry and allocation ownership. Physical bindings, saved requirements, active runs, flush requests, and admitted readers retain operational uses. New identifier lookup requires `LIVE`; an existing holder can transfer a bounded subordinate use while retaining its parent. Once a closing root reaches zero uses, its count cannot rise again.

`BUSY` keeps the complete owner queued and permits execution to progress. At zero uses, a finalizer claims `DETACHING`, removes kernel-update membership under `pgd_lock`, then removes registry ownership. It does not nest those locks or free the vOSTD root. Success authorizes local software-reader grace; only after that grace may vOSTD destroy the tree. Linux scheduling is not an inner vOSTD RCU quiescent state. [Current deferred Drop](../blueprint/linux-mode/virtualizing-ostd/memory.md#retire), [RCU separation](https://docs.kernel.org/6.12/RCU/whatisRCU.html)

Readiness is a persistent Host-owned level condition, not a transient kick. Add `retire_ready` to the versioned read-only info page. The Host publishes whether any closing root has zero uses with release ordering; vOSTD reads with acquire ordering and never clears it. Claiming finalization removes that root's contribution under the instance gate. Keep a separate DMA readiness condition for allocation retirement.

A final release under scheduler locks queues preallocated local HARD `irq_work`; that callback queues ordinary work to notify the drainer. It must not call `queue_work()` directly while holding a runqueue lock. Each drain pass attempts a finite snapshot once, republishes busy owners, then rechecks readiness. Idle registers its waiter before checking the durable conditions. This permits progress when a kick preceded requeue.

**Proposed code: transfer the complete owner on Drop.** Extend the existing [deferred retirement path](../blueprint/linux-mode/virtualizing-ostd/memory.md#retire). The registered owner contains the tree, mapped owners, pins, identifier, and its preallocated queue node.

```rust
// Proposed ownership sketch; the queue transfers nodes without allocation.
impl Drop for VmSpace {
    fn drop(&mut self) {
        if let Some(owner) = self.registered.take() {
            RETIRED_SPACES.push_owned(owner);
        }
    }
}

// Task-context drainer; each owner in this finite snapshot is attempted once.
for owner in RETIRED_SPACES.take_snapshot_owned() {
    match services().vm_destroy(owner.space_id()) {
        0 => local_reader_grace_then_release(owner),
        status if status == -KLET_BUSY => retry.push_owned(owner),
        _ => stop_preserving_owner(owner), // no ordinary return or early Drop
    }
}
RETIRED_SPACES.append_owned(retry);
request_later_drain_if_host_ready();
```

`local_reader_grace_then_release()` removes the registration action and keeps the storage alive through vOSTD reader grace. Any failure to enqueue that deferred release must preserve the owner through stop. A drain-in-progress guard prevents the `vm_destroy` service stub from recursively draining the same queue.

**Proposed code: close Host use without waiting.** The registry retains the root until one finalizer claims it. Lookup, phase, and use-count changes occur under the instance gate.

```text
vm_destroy(id):
    under instance gate:
        find this sandbox's registered root or reject
        if DETACHING: return BUSY
        change LIVE to CLOSING; reject new ordinary uses
        if operational uses remain: return BUSY
        claim DETACHING; remove this root's retire_ready contribution
    release instance gate
    under pgd_lock: unlink this root from kernel updates
    release pgd_lock
    under instance gate: remove identifier and reservation
    release instance gate
    release Host metadata and retained grant reference
    return success; vOSTD still owns the root frame and tree
```

Each early return releases its lock. A retained holder can transfer a subordinate use during closure only while its parent remains held. The zero-use transition therefore prevents later resurrection. Finalization never frees the vOSTD frame itself.

**Proposed code: publish readiness from final release.** This path can run with the scheduler's runqueue lock held, so it defers ordinary notification.

```c
/* Caller owns one counted root use and retains sandbox independently. */
raw_spin_lock_irqsave(&sandbox->gate, flags);
if (--root->uses == 0 && root->phase == CLOSING) {
        root->retire_ready = true;
        sandbox->retire_ready_count++;
        smp_store_release(&sandbox->info->retire_ready, 1);
        irq_work_queue(&this_cpu_ptr(sandbox->retire_irq_work)->work);
}
raw_spin_unlock_irqrestore(&sandbox->gate, flags);
/* No root access after the final release; caller still retains sandbox. */
```

The separate HARD IRQ callback queues the ordinary worker after the interrupted lock scope ends:

```c
static void klet_retire_irq_notify(struct irq_work *work)
{
        struct klet_retire_irq_work *item;
        item = container_of(work, struct klet_retire_irq_work, work);
        queue_work(system_wq, &item->sandbox->retire_notify_work);
}
```

The ordinary worker publishes a pending event and wakes the drainer's wait. The info-page flag remains set while any closing root is ready. vOSTD reads it with acquire ordering; only the Host finalizer removes that root's contribution. Callback storage remains live through the [worker joins](#stop-and-reclaim).

#### Retain independent device borrowers {#device-lifetime}

Root lifetime does not retain every buffer mapped through it. Trusted DMA constructors register genuine DMA allocations before exposure to device queues. The Host checks grant containment, overflow, rights, quota, and overlap. The private vOSTD owner supplies allocation provenance and excludes page tables, image data, and metadata. An arbitrary safe `DmaBuf::daddr()` is insufficient authority. [DMA backing constructors](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/mm/dma/dma_stream.rs), [current device borrowers](../blueprint/linux-mode/virtualizing-ostd/devices.md)

Pool storage requires a private vOSTD slot reservation. A slot remains `RESERVED` or `CLOSING` until unregister acknowledges completion; an early kernel-proper bitmap update cannot export it again. Keep the backing private so another constructor cannot register an overlapping whole allocation. Drop queues the exact owner and reservation for deferred release.

Queue activation retains its registered rings and records reverse allocation-to-queue links. Notify copies descriptors while holding a walker use, then rechecks queue admission before publishing work. It captures all buffer leases before publication. Backend requests retain those exact records; they do not reacquire authority from raw addresses later.

New DMA registrations, queue activation, and request publication require `RUNNING` under the instance gate, followed by the short DMA registry lock. Device-model paths use the order model lock, instance gate, then DMA lock; no reverse acquisition is permitted. A completion needing instance accounting releases the DMA lock first. Stop closes this shared admission point before later per-queue drainage. No I/O, allocation, notification, cancellation, or wait occurs under these admission locks.

Closing a ring closes every dependent queue under the same admission lock. Existing walkers can finish copying but cannot publish after closure. Previously committed requests finish or reach real cancellation completion. Release buffer leases only after their final access, and ring leases after the final used-ring write and all walkers end. Only then can unregister acknowledge allocator return, slot reuse, or retyping.

Virtio descriptors lack allocation generations. A stale descriptor admitted after reuse may name another currently authorized DMA allocation. Captured leases protect admitted-request lifetime and DMA-class authority; they do not prove the caller's intended I/O payload. No performance claim here includes eliminating device work.

**Proposed code: register owned DMA storage.** Add registration inside trusted [`DmaStream::map()`](https://github.com/asterinas/asterinas/blob/ab9a4cfdc726263b3f41ccea0337633e24b443fc/ostd/src/mm/dma/dma_stream.rs) and coherent-allocation constructors. The physical range comes from a private retained owner, not a public trait's address return.

```text
trusted DMA constructor:
    own the allocation; prepare its retirement node
    for a pool slot: reserve its exact range in private vOSTD metadata
    call dma_register_alloc(owned address, length, kind, rights)
    on ordinary failure: finish registration rollback, then release reservation
    on success: store id beside the complete owner before exposing the handle

final Drop:
    queue owner and slot reservation; keep the range unavailable
drainer:
    dma_unregister_alloc(id) == BUSY: retain everything and retry later
    dma_unregister_alloc(id) == 0: permit slot reuse or allocator return
```

The pool's bitmap cannot override vOSTD's reservation. The private backing also prevents another constructor from exporting an overlapping whole allocation. Host registration checks bounds and rights; trusted constructors establish allocation type and ownership.

**Proposed code: publish a device request with retained leases.** Extend the [MMIO queue-notify path](../blueprint/linux-mode/virtualizing-ostd/devices.md) before placing copied descriptors in its inbox.

```text
retain open queue walker and registered ring owners
copy descriptors into a prepared private request
under the required model lock, then instance gate, then DMA lock:
    require RUNNING and an open queue
    validate each copied range and capture its exact live allocation lease
    on any failure: publish nothing; arrange release of partial leases
    otherwise: commit the complete request into the prepared inbox slot
release admission locks before notification, I/O, or lease cleanup
release walker after its last ring access

backend completion:
    finish buffer access and required used-ring publication
    release request buffer leases and request-held queue use
```

Ring closure changes dependent queues to closing under the same admission synchronization. A walker that copied descriptors earlier must fail publication if closure won. Already published requests retain their captured records until real completion or cancellation completion. Queue teardown releases ring leases only after walkers and final used-ring writes end.

#### Stop execution, then reclaim resources {#stop-and-reclaim}

Stop closes carrier creation and new admission under the instance gate, records a generation, and publishes durable STOP to retained carriers. The coordinator releases the gate before notifications or waits. An admission committed earlier remains an outstanding obligation until interception. Virtual IRQ suppression does not veto trusted stop.

| Carrier position | Required withdrawal |
|---|---|
| User mode or private kernelet code | Physical entry completes its native scopes and selects a terminal Host destination |
| Capture or restore tail | Finish the bounded transition or its recognized recovery before terminal handoff |
| Host service, IRQ, or exception C | Finish or cancel its actual scopes; do not abandon locks or C activations |
| Suspended Host continuation | Restore its retained environment when scheduled, finish Host work, then observe stop |
| Backend-owned wait | Observe the durable predicate after wakeup and follow its cleanup path |
| Never started | Creation/start cancellation proves that no image execution was admitted |

The terminal tail runs only after outgoing Host C returns. It selects a reserved Host stack baseline, installs the native environment, normalizes AC/DF, and retires only the owned mirror contribution. It cancels the run without resuming its Rust continuation or requiring tenant destructors. An early bounded `panic_stop(reason)` requests stop before optional logging or backtrace locks can block it.

A carrier acknowledges its stop generation only from terminal Host task context. All frame readers, bridges, restore records, and active-run accesses must have ended. Owner CPUs retire every capture publication for that generation; a delayed clear must not erase a newer publication. No pointer clear, off-CPU observation, or IPI acknowledgement substitutes for this completion.

After closing instance admission, trusted cleanup can close device queues and start drainage while carriers withdraw. It waits for execution acknowledgements, admitted Host work, and remaining operational root uses. Once the final-release producers have ended, it synchronizes every queued HARD IRQ item, then joins or cancels ordinary notification work. Only after callbacks and finalizers finish may it release their storage. Uninterruptible I/O can delay this phase; retain resources until actual completion.

Finally detach registered roots, end software readers, and release grants and stacks. Unloading shared kernel/image mappings also requires the Host's kernel/global invalidation and entry-target quiescence. Empty tenant-use masks do not prove those global translations or published code targets are gone. [Current reclamation obligations](../blueprint/linux-mode/faults-and-reclamation.md), [global TLB operations](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/tlb.c)

**Proposed code: request stop.** Extend the existing [instance stop path](../blueprint/linux-mode/faults-and-reclamation.md). Creation, user admission, and kernel resumption already use the same instance gate.

```text
under instance gate:
    if stop already requested: retain the existing stop generation
    otherwise:
        change RUNNING to STOP_REQUESTED; assign a nonreused generation
        close carrier creation and new resource/execution admission
        retain the created-carrier membership for this generation
release instance gate
publish sticky STOP to each retained carrier; perform required kicks/wakeups
queue Host coordinator work without waiting for carrier acknowledgements
initiating carrier finishes its own Host scopes and reaches terminal handoff
```

NMI requests reach this operation through preallocated local HARD IRQ work. A private panic invokes the bounded `panic_stop()` entry before logging or backtrace locks. Neither requester waits for itself or abandons an active Host C frame.

**Proposed code: acknowledge actual execution withdrawal.** The fixed terminal tail runs after the ordinary service, IRQ, or exception C activation has returned.

```text
assembly terminal tail:
    select retained Host stack baseline and install native memory environment
    normalize AC/DF; retire only the owned mirror contribution
    cancel the run; make its kernelet continuation unreachable
    enter terminal Host task context

terminal completion:
    finish assembly readers, bridges, and restore-record uses
    confirm owner-CPU retirement of capture publications for this generation
    prohibit any new publication for that generation
    store_release(carrier.quiescent_generation, stop_generation)
```

Delayed CPU-slot cleanup compares the generation before clearing a publication. The coordinator observes acknowledgements with acquire ordering. A suspended service reaches this path only after Linux restores its saved environment and the service finishes its actual cleanup.

**Proposed code: join users before freeing storage.** The coordinator runs in Host task context, outside locks needed by carriers and completion callbacks.

```text
close device queues; start their drain while carriers withdraw
wait for every required carrier quiescence acknowledgement
wait for admitted Host work, root operational uses, and device accesses
establish that no final-release producer can queue another notification
for each retained HARD IRQ item: irq_work_sync(item)
cancel_work_sync(retire_notify_work); join other registered notification work
finish root finalizers and detach kernel-update membership
finish remaining software-reader and entry-target uses
unmap shared image/kernel allocations; complete Host kernel/global invalidation
release backing grants, stacks, and callback storage after their final uses
```

[`irq_work_sync()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/irq_work.c) must precede the ordinary work join because its callback can enqueue that work. Joining ordinary work first would permit a later enqueue into freed storage. Device completion remains independent of carrier quiescence; an uninterruptible request can keep its resources retained.

## Safety constraints and limitations {#safety}

The proposal retains vOSTD inside the trusted base. Kernel proper remains restricted to the admitted safe API and cannot publish arbitrary roots, restore targets, or DMA registrations. Moving enforcement into vOSTD requires reviewed private constructors and mutation paths under the existing [image trust contract](../blueprint/linux-mode/builds-and-images.md#audit).

The combined protocol must maintain these invariants:

1. Every permitted continuation has retained storage and an explicit memory requirement before it can resume.
2. Physical installed-state records and use membership agree with completed hardware transitions; transitional state authorizes neither native nor tenant access.
3. Every PTE writer enforces ownership and permissions; required invalidation and reader grace precede reuse.
4. Every user run owns stable restore operands and one actual kernel continuation through completion or cancellation.
5. Pending events and stop survive concurrent admission, migration, and notification coalescing.
6. Physical IRQ state, virtual guards, Linux nesting, and the owned mirror retain separate accounting.
7. Every Host activation closes normally or through its defined abort path before terminal stack transfer.
8. Root, allocation, queued-work, entry-publication, and image lifetimes each wait for their actual last users.

The first user-entry profile requires an admitted PTI-off Host; this is a support constraint with a mitigation-policy cost, not a portable replacement for PTI. No-PCID operation avoids retained tagged contexts but can increase flush cost. Shared roots still require native consumer integration and Host kernel-mapping maintenance.

> **To be written.** Extend user entry to PTI-enabled Hosts with real restricted/full roots, minimal entry mappings, low-half synchronization, view-aware recovery, and joint lifetime rules. Source: Linux v6.12 `pti.c`, entry assembly, and this Note's retained root/view contract. The single-root profile does not provide this extension.

> **To be written.** Fix the complete admitted architecture-state schema, exact segment/flag policies, FPU import/export formats, and reachable Host-work consumer set during implementation. Complete the time-consumer conversion and tick accounting/Task-attribution rules. Sources: the pinned state, entry, timer, and tick-dependency code linked above. These tasks refine named interfaces; they do not replace the existing control-flow protocol.

> **To be written.** Add grant lookup, affected-space enumeration, quarantine, and invalidation before admitting recoverable action-required machine checks. Sources: Linux's machine-check path and the [exception contract](#kernel-exception-audit). The initial profile retains Host failure policy for unsupported recovery.

Arbitrary reentrant Host-to-kernelet callbacks are excluded unless they supply an abort path for the outer Host activation. Wider paging configurations, shared-parent replacement, asynchronous Host page-fault adapters, and new device backends need explicit contracts. Stop progress depends on completing admitted Host operations; the protocol supplies no universal deadline for uninterruptible I/O.

**[unverified]** The protocol has a conditional source-level implementation argument. It is not a complete machine-checked proof, compiled Linux patch, booted backend, or demonstrated performance result.

## Validation {#validation}

### Existing evidence

The child Notes retain [memory measurements](memory-api-costs.md#validation) and [execution measurements](user-mode-handoff-costs.md#validation) from earlier prototypes. Those results concern their recorded controls and machines. They do not measure this independent-root, direct-execution backend. Bare-call estimates cannot price complete transitions.

The algorithms above supply local ordering arguments: admission versus flush snapshot, event publication versus entry CAS, and closure versus final-use release. Exploratory finite models remain outside the book. Their bounded checks do not validate Linux weak-memory behavior, every native consumer, real devices, or the assembled entry sequence.

### Required correctness checks

| Boundary | Required tests and acceptance conditions |
|---|---|
| Registration and closure | Fail each allocation; race stop with attachment/publication. No published partial root, leaked reservation, or early storage release. |
| Root installation and scheduling | Interrupt and migrate copies and services; switch through native tasks and unrelated kernel threads. Resume with the retained requirement and correct native accounting. |
| Flush completion | Race admission, departure, reentry, concurrent writers, and reversed callback order. No revoked translation survives completion and reuse. |
| Run admission | Publish events/stop before and after each final transition; consume kicks during preparation; migrate carriers. No lost payload, unauthorized readmission, or mirror imbalance. |
| Entry and return | Exercise syscall, user/kernel IRQ, upcall Task switches, nested NMI, copy fixups, and recognized restore faults. Match exact frame/run ownership and physical IF. |
| Stack entry | Exercise deep stacks and each pre-bridge push sequence. Verify mapped reserves, frame-size checks, and upcall admission before claiming overflow containment. |
| Time and virtual ticks | Vary Host HZ, suspend carriers, coalesce notifications, and idle on full-dynticks CPUs. Preserve elapsed time, accounting attribution, callback periods, and deadline wakeups. |
| CPU-state validation | Test malformed XSAVE, privilege flags, bases, Task state replacement, Host FPU use, and migration. No stale state, privilege gain, or uncontained restore fault. |
| Space and device retirement | Retire while off CPU, flushing, walking, or using shared rings. No allocation or slot reuse before final accesses and acknowledgements. |
| Whole stop | Stop every carrier phase, callbacks, waits, and partially published operations. Retire CPU slots and queued work before grants, stacks, or image mappings. |
| Supported profile | Test feature mismatch, alternate entry instructions, and excluded configurations. Reject unsupported admission rather than silently changing Host policy. |

Use lockdep, entry/unwind checks, fault injection, and a booted Linux backend alongside model tests. Check each instruction prefix where NMI can observe partially changed GS, stack, root, or capture state. A book build checks document structure only.

### Performance attribution {#component-options}

Compare the complete protocol with the current Linux backend and matched native OSTD execution where available. Separate controls can retain Linux mm while changing register capture, retain the entry bridge while changing PTE updates, or batch current helpers. These controls attribute costs; they are not separate proposed final designs.

| Workload | Counters and matched conditions |
|---|---|
| Fork, mmap, mprotect, munmap | PTE operations, helper/service entries, actual invalidation targets, waits, frame references; match pages, permissions, density, and CPU placement |
| Resident and faulting copies/atomics | Software walks, bytes, fixups, retries, root changes; match buffer layout, locality, and demand/COW behavior |
| Same-Task syscalls and Task switches | Register bytes copied, stack transfers, FPU imports/exports/reloads, base writes, actual CR3 operations; match useful work and mitigation policy |
| Migration and reclamation | Restoration, retained bytes, deferred work, stop latency, and final cleanup; match vCPU count, load, affinity, and device activity |

Record activation sequences that select several spaces before any user access. Immediate installation can perform root loads that the current deferred selection avoids. Same-root reuse reduces this work but does not remove the comparison. Native OSTD may already activate eagerly, so the baseline must be stated.

Report CPU work, waiting, latency distributions, throughput, and retained memory separately. Count extra integration work and root flushes in the result. Host coordination, hardware saves, required FPU snapshot transfers, and asynchronous device lifetime remain part of the proposed system. Direct page-table updates and GPR capture also retain the chosen vCPU-local lookup and stack-check mechanisms. Removing identified adapter operations does not establish a net performance improvement.
