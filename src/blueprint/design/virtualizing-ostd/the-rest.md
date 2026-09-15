# Boot, power, panic, and the rest

*Part of question 2. Virtualizes `boot`, `smp`, `power`, `console`, `log`, `panic`, the `#[ostd::main]` and `#[ostd::panic_handler]` expansions, and the `arch` surface. Discharges invariant I8's obligation to list every tenant-visible difference.*

## Boot {#boot}

The selected startup model follows native OSTD: bootstrap has a stack but no Task, and `Task::current()` is None until the first context switch.
Host OSTD owns a temporary startup stack per vCPU. Host scheduling reuses the vCPU Thread's original stack; each return from internal execution continues a saved call on this stack.
The [stack and return lifetimes](tasks.md#stack-return) distinguish this persistent Host call stack from the temporary startup stack and each internal Task's stack.
The first internal switch retires startup. Pause and transfer requests return to the vCPU execution loop, while only final stop lets `enter_vcpu` return to its caller.
vCPU 0 enters the ELF's `_kernelet_entry` once, initializes the shared vOSTD state, then calls the kernelet kernel's `__ostd_main`.
No extra boot-only vCPU or temporary schedulable boot Task is created.

The ELF entry is proposed vOSTD code in `ostd/src/kernelet/entry.rs`, selected by the kernelet build.
The current tree provides native `ostd/src/lib.rs::init` and the `__ostd_main` macro expansion; it does not yet implement this kernelet entry.
The [image contract](../builds-and-images.md) supplies the validated entry address and these host-owned arguments:

```rust
// Proposed vOSTD entry storage. These are references to published read-only
// host tables, not references to host Task/Thread objects.
static SERVICES: Once<&'static ServiceTable> = Once::new();
static BOOT_ARGS: Once<&'static BootArgs> = Once::new();

pub(crate) fn service_table() -> &'static ServiceTable {
    *SERVICES.get().expect("entry installed the service table")
}

unsafe extern "Rust" {
    fn __ostd_main() -> !; // Emitted in this image by #[ostd::main].
}

#[unsafe(no_mangle)]
pub extern "C" fn _kernelet_entry(
    services: &'static ServiceTable,
    boot: &'static BootArgs,
) -> ! {
    SERVICES.call_once(|| services);
    BOOT_ARGS.call_once(|| boot);
    // The vOSTD implementation of init validates BootArgs/source compatibility.
    // SAFETY: Only vCPU 0 enters here, once, on the prepared startup stack.
    unsafe { crate::init() };
    // SAFETY: The kernel's macro supplies this symbol in the same ELF.
    unsafe { __ostd_main() }
}
```

The two builds select different implementations of `ostd::init` at compile time. In the kernelet image, that function performs the following operations in order:

1. Validate BootArgs, source hash, shared descriptors and the service table before exposing their accessors to the kernel.
2. Parse the early command line and initialize logging with the native boot/log code. The logger's default level is Off.
3. Initialize the virtual CPU-local replicas and frame allocator from the host-published grant. Host OSTD has already mapped the pages and metadata.
4. Initialize the local task/synchronization machinery with each current-task slot empty. Publish no runnable Task before its scheduler can be used.
5. Run `.init_array` once, as native `invoke_ffi_init_funcs` does.
6. Clear `IN_BOOTSTRAP_CONTEXT` as native `init` does, then return to `_kernelet_entry`, which calls `__ostd_main`.

Clearing that flag ends the framework's early-initialization exemption; it does not assert that a Task already exists.
The native tree also clears it before kernel `main` while current is still None.
Physical CPU feature enabling, early allocation, serial setup, page-table construction, physical SMP startup and boot-page-table dismissal are omitted in the kernelet build because the host already performed them.

Kernel `main` initializes and injects its ClassScheduler, initializes the boot CPU's kernel state, registers `ap_init`, and creates its idle Thread.
The [Task startup sequence](tasks.md#task-execution) contains the native `main`/`ap_init` bodies and the first enqueue/switch path.

`boot::boot_info` is virtualized without a crossing: a `BootInfo` synthesized from `BootArgs` with the bootloader name `"kernelet"`, the command line, `memory_regions` holding one `Usable` region per initial run, so that the kernel proper's `MemTotal`, which is the sum of the `Usable` regions (checked on the tree: `kernel/core/src/vm/mod.rs`), is the initial grant; and no ACPI, framebuffer or initramfs arguments. An initial RAM file system, if the endovisor provides one, arrives as a block device, not as a boot module. `MemoryRegion`, `MemoryRegionType` and `EarlyCmdline` are identical types; the two constructors `MemoryRegion::kernel` and `MemoryRegion::module`, which use the absent kernel offset and linear map (checked: `ostd/src/boot/memory_region.rs`), are absent, and nothing in the kernel proper calls them.

`boot::smp::register_ap_entry(entry)` retains the native role: publish the AP initialization hook after shared initialization is ready.
A secondary enters `EntryTable::run_task(ENTRY_SECONDARY_VCPU, vcpu_id)`, with no current Task, and waits for that hook before using initialized virtual CPU-local state.
This can reuse the native `ap_early_entry` publication loop: it reads the kernelet's own AP-entry slot and spins until publication; host physical preemption and stop remain possible.
Host OSTD does not read a kernelet Rust function pointer out of that slot or invoke the AP hook itself.
The vOSTD secondary path invokes it inside the image, initializes that vCPU's idle Thread, and transfers into its first Task.
Secondaries do not rerun global `_kernelet_entry`, `.init_array` or `__ostd_main`.
The boot vCPU must publish the hook before waiting for AP work. After first scheduling, each idle closure creates its pinned ordinary interrupt-worker Thread as shown in [Tasks](tasks.md#objects); no worker callback runs on a bootstrap stack. Callback infrastructure is ready before this first idle entry.
Physical `smp::inter_processor_call` remains absent; vCPU notification crosses through the service table.

The relevant code reuses `AP_LATE_ENTRY` and `register_ap_entry` from `ostd/src/boot/smp.rs`.
The secondary's host entry establishes its vCPU identity and startup stack; this tail waits before using the vOSTD state published by boot-vCPU initialization:

```rust
// vOSTD's boot/smp.rs: the existing image-local publication slot and API.
static AP_LATE_ENTRY: Once<fn()> = Once::new();

pub fn register_ap_entry(entry: fn()) {
    AP_LATE_ENTRY.call_once(|| entry);
}

// Kernelet branch reached by EntryTable::run_task(ENTRY_SECONDARY_VCPU, vcpu_id).
fn secondary_bootstrap() -> ! {
    let ap_entry = AP_LATE_ENTRY.wait(); // Spins locally; no Waiter or Task yet.
    ap_entry();                        // Kernel ap_init: local init, spawn idle.
    Task::yield_now();                 // Native fallback if spawn did not switch.
    unreachable!("first scheduling from bootstrap must not return");
}
```

**The end of main uses the native expansion.**
With bootstrap current still None, the existing `ostd_main_body` emitted by `#[ostd::main]` is applicable in both builds:

```rust
// Equivalent to the generated body, with imports for readability.
use ostd::{
    power::{poweroff, ExitCode},
    task::Task,
};

let () = main();
Task::yield_now();
assert!(Task::current().is_none());
poweroff(ExitCode::Success);
```

`let () = main()` preserves the generated body's check that `main` returns `()`.
The actual macro uses absolute paths to avoid depending on imports at its call site.

If task publication already switched into idle, bootstrap never reaches this epilogue.
Otherwise yield performs the first selection; a successful first switch likewise never returns here.
Only a kernel main that leaves no runnable Task reaches poweroff, just as on the host; vOSTD's poweroff uses the stop service.
No new main-epilogue abstraction or proc-macro change is needed for this selected startup model.
The same reasoning applies to the native `#[ostd::test_main]` expansion.

## Power and exit

`power::poweroff(code)` and `power::restart(code)` are virtualized to the `stop` service call with `STOP_EXIT`, with `ExitCode::Success` mapped to `0` and `Failure` to `1`, and `restart` setting the `EXIT_RESTART` bit (bit 31, chosen) so that the runtime can tell a `reboot(2)` restart, which it may honor by creating the sandbox again, from a power-off; the kernel proper's `reboot(2)` passes `Success` to both (checked on the tree: `syscall/reboot.rs`). A new `power::exit_with_code(u32)`, present in both builds and `poweroff` in OSTD, carries an arbitrary code, and the kernel proper's init-exit path, which on the host kernel panics with the init process's status (checked: `kernel/core/src/init.rs`, `bsp_idle_loop`), gets the one `cfg` line the [taxonomy](index.md) lists, calling `exit_with_code(status)` so that init exiting ends the kernelet as `Exited(status)` rather than `Panicked`. `inject_poweroff_handler` and `inject_restart_handler` are accepted and never called. The kernel proper installs only a restart handler, from `arch/x86/power.rs`, which acquires the ACPI reset port and falls back to the i8042 controller (checked on the tree); that file uses `ACPI_INFO`, `IoPort` and `aster_i8042`, all absent, so the `cfg` line is on the call to its `init` in `arch::init` and the file is compiled out. The kernelet runtime reads the code through the [endovisor ABI](../endovisor.md).

## Panic, oops, and the unwinder

`catch_unwind`, `begin_panic` and `print_stack_trace` keep their code: the unwinder and its tables are in the kernelet image, and the image's linker script provides the symbols it needs ([Builds and images](../builds-and-images.md)). The kernel proper's oops mechanism is its own: a panic on a thread whose `PanicInfo` can unwind is raised as an `OopsInfo` and caught by `catch_panics_as_oops`, which wraps every user task and kernel thread, counts it against `MAX_OOPS_COUNT`, and lets the thread die while the kernel continues (checked on the tree: `kernel/core/src/thread/oops.rs`, `thread/kernel_thread.rs`). On the tree that path is compiled in but switched off: `PANIC_ON_OOPS` is a `static` initialized `true` with no writer, the `panic` command-line key is registered as unimplemented (checked: `comps/cmdline/src/unimplemented.rs`), and so every panic on the host kernel today aborts. the kernel proper's kernelet configuration turns the path on with one `cfg` line, initializing `PANIC_ON_OOPS` to `false` under the feature, listed in the [taxonomy](index.md)'s inventory; a tenant's kernel oopses where the host kernel would halt, which is the behavior a sandbox wants and the mechanism the kernel already has.

Three things are virtualized beneath it, all in OSTD, none in the kernel proper's handler, which does not know whether the panic it raises will be caught:

- **The message is stashed.** The `#[ostd::panic_handler]` expansion, like `#[ostd::main]` above, is changed in both builds to enter through `::ostd::panic::__handler_entry(info)` before calling the kernel's handler; the OSDK's `#[ostd::test_panic_handler]` enters the same way. In vOSTD the entry copies the panic message and location into a kernelet-wide fixed buffer of 1 KiB (chosen, the `log_write` limit) under a spin lock, `PANIC_MESSAGE`, without allocating; the first panic to reach `panic` or `abort` is the one reported if two race. In the host build it does nothing.
- **A caught panic is reported.** `catch_unwind` in vOSTD, on the `Err` path, makes the `oops` service call with the stashed message, so that the endovisor's oops budget counts it and can end a kernelet that oopses without limit; the kernel proper's own `MAX_OOPS_COUNT` applies as well, and the lower of the two is the one that ends the kernelet in practice. `catch_panics_as_oops` is the only caller of `catch_unwind` in the kernel (checked on the tree), so a report per catch is a report per oops.
- **`abort` ends the kernelet.** `panic::abort`, which the oops handler calls when a panic cannot be caught or the count is exceeded and which on the host kernel powers the machine off, is the `stop` service call with `STOP_PANIC` and the stashed message, or `"abort"` if nothing was stashed: it ends the kernelet as `Panicked(message)` and only the kernelet. `__ostd_panic_handler`'s fallback, when no handler is injected, does the same. `print_stack_trace` writes through `log_write` at `LEVEL_CONSOLE`.

An allocation failure the kernel cannot absorb reaches `#[alloc_error_handler]`, whose `abort_with_message!` on the tree logs the layout and aborts (checked: `ostd/src/mm/heap/mod.rs`); in vOSTD the macro writes its formatted text into `PANIC_MESSAGE` before `abort`, so that the layout reaches the host in `Panicked(message)` ([Memory](memory.md)).

## Console and log

`early_print!` and `early_println!` are virtualized to `log_write` at `LEVEL_CONSOLE`; they are the kernel proper's boot-time and panic-time output, and the endovisor's `log` hook delivers them to the log endpoint beside the kernel's records. The host kernel silences its early console unless `earlycon` is on the command line (checked on the tree: `EarlyCmdline::has_early_console`, `arch/x86/serial.rs`); a kernelet's early console is always on, since the host pays nothing for it until the hook runs. The log macros are virtualized at their sink: formatting, the per-crate prefix and level filtering are identical, and `__write_log_record` delivers each record that passes the filter first to the `log_write` service call, formatted into a kernelet-side buffer and truncated at the call's 1 KiB limit, and then to the injected `Log` if there is one. The kernel proper's `logger` component is not machine-facing: it writes records to the `aster-console` devices, the tenant's virtio consoles, and falls back to `early_print!` (checked on the tree: `comps/logger/src/console.rs`), so it stays in a kernelet's kernel, injects itself at bootstrap as on the host, and gives the prelude its `print` and `println`. A kernel record inside a kernelet therefore goes two places: the host's log hook, rate-limited by the kernelet's policy, and the tenant's console, as on the host kernel. `set_max_level` and `max_level` are identical. The kernel proper keeps no log ring buffer today: there is no `syslog(2)` and `/dev/kmsg` refuses reads (checked: `device/mem/file.rs`), so the host's log hook is the only place a kernelet's records exist, and a tenant's `dmesg` fails inside a kernelet as it fails on the host kernel.

## `arch`, `bus`, `util`, `prelude`

`read_tsc`, `read_random` and `cpuid` are identical: unprivileged or ring-0 instructions with local effect. `tsc_freq` is virtualized to read `BootArgs::tsc_freq_hz`, since the host calibrated it. `TrapFrame`, `USER_CS_VALUE` and `USER_SS_VALUE` are identical. `IRQ_CHIP`, `MappedIrqLine`, `ACPI_INFO`, `DEVICE_TREE`, `SERIAL_PORT` and the port I/O types are absent; their users are the host-only components, the power file above, and the virtio crate's MMIO bus and PCI transport, whose `cfg` lines [Devices](devices.md) lists. `bus::BusProbeError`, everything in `util`, the `prelude`, `Error` and `Result` are identical.

## `ktest`

The kernel's unit tests run inside a kernel; a test of the kernel proper in its kernelet configuration must run inside a kernelet. `#[ktest]` and `.ktest_array` are identical, and the harness is the endovisor's: a test kind of kernelet image whose `main` runs the test array, created by a host-side test runner through the control half, with its log routed to the runner and its exit code the verdict. The OSDK's `test_main` keeps the native bootstrap epilogue, while `test_panic_handler` uses `__handler_entry`, so a `#[should_panic]` test's panic is caught by the harness as today and is reported to the host as an oops, which the test runner's budget must allow for. That is an Implementation-chapter matter; the design's requirement is only that nothing on this page prevents it, and nothing does.

## What a tenant sees

- `/proc/cmdline` is the configured command line; `/proc/meminfo`'s total is the initial grant; there are no ACPI tables, no framebuffer, no serial ports.
- `reboot(2)` ends the sandbox with a code the runtime sees, and a restart is marked as one; whether the sandbox comes back is the runtime's choice. `init` exiting ends it with init's status.
- A kernel panic ends the sandbox, not the machine; a kernel oops kills the thread and continues, up to a budget the runtime sets. The host kernel today halts on every panic, so a tenant's kernel is more forgiving than the host's.
- Kernel log records reach the host's log hook and the tenant's console, cut at 1 KiB each; `dmesg` is unsupported, as on the host kernel. The early console is always on.
- `nproc` is the virtual-CPU count; the TSC frequency is the host's.

## Costs

- Entry: initialization uses a temporary startup stack per vCPU, reclaimed after first transfer or stop. Count the persistent vCPU Thread, idle and virtual interrupt worker stacks separately. Initialization and complete stack costs have not been measured for this startup path.
- Per log record: the formatting into a kernelet-side buffer, one crossing, a copy of up to 1 KiB, and the hook, when under the rate limit; a dropped record costs the crossing and a counter; the console write the `logger` component makes, as today.
- Per oops: the kernel's own `format!` and `Box` for the `OopsInfo` and the unwinding, as on a host kernel with oopses enabled; plus the stash copy, one crossing and the hook.

## What this page decides

- **Bootstrap has no internal current Task** (D26 revised). The native idle-thread publication triggers the first transfer; its local Task obtains a stack through the same task_create service as ordinary tasks. The startup stack retires after that transfer. The native main epilogue remains applicable, including shutdown when no Task was created.
- **Init exiting ends the kernelet as an exit, not a panic** (register D27), by one `cfg` line in the kernel proper; without it the runtime could not tell a clean shutdown from a crash.
- **Every caught panic is reported to the host, from OSTD's `catch_unwind`, with a message stashed at the handler's entry** (register D28), so that an oops budget can be a host policy rather than only the kernel proper's own constant. The alternative, a `cfg` line in `catch_panics_as_oops`, would put the report in the kernel proper and would be the second such line for one mechanism.
- **vOSTD enables the kernel proper's oops path** (register D48) with one `cfg` line on `PANIC_ON_OOPS`. The alternative, leaving it off as the host does, makes every tenant-kernel panic end the sandbox and leaves D28 with nothing to count.
- **The `logger` component stays in the kernelet, and every record also goes to the host** (register D49). The alternative, making the component host-only, would take `print` and `println` out of the kernel proper's prelude and the tenant's console out of the kernel's own view of its log.
