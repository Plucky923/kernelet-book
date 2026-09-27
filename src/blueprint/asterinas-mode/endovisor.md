# The endovisor

*Answers question 4, kernel side: how is the endovisor implemented on OSTD, and what user-space ABI does it expose to the kernelet runtime?*

The endovisor is a part of the host kernel, the blue box beside "Linux functionality" in the [figure](index.md): safe Rust in the kernel proper's host configuration, with no `unsafe` of its own, built over two things and nothing else. Downward it uses the [control half](kernelet-api-control.md) of OSTD's kernelet API to create, feed, watch, kill and destroy kernelets and to receive their hooks; sideways it uses the host kernel's ordinary Linux functionality, files, threads, wait queues, the random source, to give those hooks something to do. Upward it exposes the **endovisor ABI**: a character device, `/dev/kernelet`, whose operations are how the kernelet runtime in host user space does everything it does. It is to the kernelet API what a VMM is to KVM, except that it lives in the kernel and its device models are function calls away from the kernelets they serve.

## Where it lives

```
kernel/core/src/endovisor/                a module of the kernel crate, `cfg(not(feature = "kernelet"))`
  mod.rs                                  registration from `device::misc::init_in_first_kthread`
  images.rs                               include_bytes! of every kernelet image the build produced
  device.rs                               `/dev/kernelet`: the misc character device and its ioctls
  sandbox.rs                              one `Sandbox` per created kernelet: hooks, devices, endpoints
  models/{blk,console,rng,vsock,net}.rs   the virtio MMIO device models and their device threads
  vsock_switch.rs                         the host-wide switch of [Channels](channels.md)
  retry.rs                                the thread that finishes destroys and retries zombies
  policy.rs                               limits, defaults, who may create; the vsock pair table
```

It is a module of the kernel crate and not a component under `comps/`, because everything it needs from the kernel proper is crate-private to that crate (checked on the tree: `Device` and `PerOpenFileOps` in `device/mod.rs`, `Ioctl` and `ioc!` in `util/ioctl`, `FileLike` and `FileTable` in `fs/file`, `ThreadOptions` in `thread/kernel_thread.rs`), and no component depends on the kernel crate, since the kernel depends on them. It registers its device from `device::misc::init_in_first_kthread`, the `tdxguest::init()` pattern, which runs after the misc major number exists and after the component stage (checked: `kernel/core/src/init.rs`, `device/misc/mod.rs`). The device node is created by the registry from `devtmpfs_meta`, with mode `u+rw` by default, root only (checked: `fs_impls/devtmpfs/tree.rs`); the policy may widen it with `DevtmpfsNodeMeta::with_mode`.

## The `Sandbox`

One object per created kernelet, owned by the file description that created it:

```rust
// endovisor/sandbox.rs
pub(crate) struct Sandbox {
    state: Mutex<SandboxState>,                    // Configuring | Live(Arc<Kernelet>) | Gone
    cid: u32,                                      // allocated at CREATE, never reused
    config: Mutex<PendingConfig>,                  // devices, budgets, cmdline, gathered before START
    hooks: Arc<SandboxHooks>,                      // implements `KerneletHooks`
    devices: Mutex<Vec<AttachedDevice>>,           // model + backend + device thread, per device
    exit: Once<ExitStatus>,
    pollee: Pollee,                                // readable once exited
    owner: Uid,                                    // who created it; for the policy's per-user caps
}

struct SandboxHooks {
    models: RwLock<Vec<Arc<dyn DeviceModel>>>,
    log: Once<Endpoint>,                           // kernel log records and early-console bytes
    dropped: AtomicU64,                            // log or console bytes dropped for want of an endpoint or space
    policy: Policy,
}
impl KerneletHooks for SandboxHooks {
    fn spawn_task(&self, k: &Kernelet, body: KerneletTaskBody, hints: SpawnHints) -> Result<Arc<Task>, SpawnError> {
        ThreadOptions::new(move || body.run()).cpu_affinity(hints.vcpus).nice(hints.nice).build_task()   // not yet running
    }
    fn mmio_read(&self, k: &Kernelet, dev: DeviceId, off: u32, width: u8) -> u64 { self.models.read()[dev].read(off, width) }
    fn mmio_write(&self, k: &Kernelet, dev: DeviceId, off: u32, width: u8, v: u64) { self.models.read()[dev].write(k, off, width, v) }
    fn log(&self, k: &Kernelet, level: LogLevel, module: &str, text: &str) { self.push_log(LogLine::Record(level, module, text)) }
    fn on_grant_exhausted(&self, k: &Kernelet) -> u32 { self.policy.extra_grains(k) }
    fn on_oops(&self, k: &Kernelet, task: TaskName, msg: &str) { self.push_log(LogLine::Oops(task, msg)) }
    fn on_dying(&self, k: &Kernelet, why: &ExitReason) { for m in self.models.read().iter() { m.cancel() } ; vsock_switch::mark_dead(k.id()) }
    fn on_exited(&self, k: &Kernelet, st: &ExitStatus) { /* record `st`; wake the sandbox's pollee */ }
}
```

The early console reaches the endovisor as `log` records at `LogLevel::Console`, so it and the kernel log share the **log endpoint**: a kernelet's boot and panic output is an operator's concern, not the tenant process's standard output, which is the virtio console's ([kernelet runtime](kernelet-runtime.md)). `push_log` never sleeps, since `log` is called from kernelet tasks that cannot: when the log endpoint does not exist yet or is full, the line is dropped and counted in `dropped`, which `KERNELET_STATS` reports as `log_records_dropped` beside the rate limiter's count.

An **`Endpoint`** is a pair of bounded byte queues in host memory with a wait queue and a `Pollee` each way, one end held by the sandbox and the other surfaced to user space as a file descriptor; it is what a log stream, a console, a user-space network backend and a vsock stream are made of. Its bytes are charged to the kernelet with `charge_host_bytes` in both directions, and its bound is the policy's. In the kernel-to-user direction a device thread blocks on a full queue and a hook drops; in the user-to-kernel direction a `write(2)` blocks on a full queue, or returns `EAGAIN` if the descriptor is non-blocking, and wakes the device thread's wait queue, while a device thread's pop wakes the `Pollee`.

A **`DeviceModel`** is the virtio MMIO register file of [Devices](virtualizing-ostd/devices.md) over a backend:

```rust
pub(crate) trait DeviceModel: Send + Sync {
    fn read(&self, off: u32, width: u8) -> u64;                       // never sleeps
    fn write(&self, k: &Kernelet, off: u32, width: u8, v: u64);        // never sleeps; a notify records into the inbox; status 0 is reset
    fn cancel(&self);                                                  // from `on_dying`: sets a flag and wakes the thread, nothing more
}
```

The device thread behind a model is a kernel thread of the host kernel proper (`ThreadOptions::new(…).spawn()`), started at `START` and adopted with `adopt_current_task` until it finishes, so that its CPU time is charged to the kernelet. `cancel` does not join it, because the reaper task that calls `on_dying` must not wait on one kernelet's host I/O, so it sets a flag and wakes the thread; the thread finishes the host I/O it is in, since no file operation on the tree can be interrupted, abandons the rest of its inbox, calls `disown_current_task`, and exits. `destroy` reports `Zombie` for exactly that interval, and the retry thread retries it. The backends of the first version and the host objects they hold, taken from the runtime's own file table at `ATTACH` (the `ioctl` runs on the runtime's process, so `FileTable::get_file(fd)` is the lookup, and the file is held as `Arc<dyn FileLike>`, an `InodeHandle` for a regular file or the block device's open file):

| backend | host object held | the device thread's work |
|---|---|---|
| block | the runtime's open file for a regular file or a block device, opened read-write | `read_at` and `write_at` inside `guest_memory`; `sync` for flush |
| console | an `Endpoint` | moves bytes between the virtio-console queues and the endpoint |
| entropy | nothing | fills request buffers from the host's random source |
| vsock | the switch | transmit-queue packets into the switch, switch deliveries into the receive queue |
| network | an `Endpoint` carrying Ethernet frames | moves frames between the virtio-net queues and the endpoint; the runtime's user-space NAT terminates them |

Assumption A5 applies to every row: the file and endpoint operations are driven by a kernel thread without a process. If a host path turns out to need one, the thread borrows the runtime's process context for that call.

## The endovisor ABI

The ABI is the misc character device `/dev/kernelet` and two kinds of file descriptor it hands out, both `FileLike` implementors inserted into the caller's file table with `FdFlags::CLOEXEC`, the way the tree hands a pseudo-terminal's descriptor out of an `ioctl` (checked on the tree: `device/tty/pty`): a **sandbox descriptor**, one per created kernelet, and **endpoint descriptors** for streams. `CLOEXEC` is what keeps an OCI hook process the runtime forks from inheriting a sandbox and keeping it alive. Possession of a descriptor is the capability to operate on the sandbox, as with `/dev/kvm`; no per-call credential check is made, and the `owner` field serves only the policy's per-user caps. Every operation is an `ioctl` in the typed form the kernel proper uses (`ioc!(NAME, MAGIC, NR, DataSpec)` over `Ioctl<MAGIC, NR, IS_MODERN, D>` with `InData`, `OutData` and `InOutData`, whose `T: Pod`; checked on the tree: `util/ioctl/mod.rs`). The magic is `MAGIC = 0xC7` (chosen; `'K'` is Linux's virtual-terminal family, which the tree already uses in `device/tty/vt`; **[unverified]** that `0xC7` is free in Linux's `ioctl-number` list, to be checked before the first build). Argument structures contain no pointers, since no pointer type is `Pod`; a user buffer is passed as a `u64` address and length and read with the tree's user-space reader.

**On `/dev/kernelet`:**

```rust
/// Creates a sandbox in the endovisor's `Configuring` state and returns a sandbox descriptor.
/// No kernelet exists yet; `START` creates it from what `ATTACH`, `ENDPOINT` and `BUDGET` gathered.
type KerneletCreate = ioc!(KERNELET_CREATE, MAGIC, 0x01, InOutData<CreateArgs>);
#[repr(C)] pub struct CreateArgs {
    pub image: u16,                  // kind: 0 = the Linux kernelet
    pub num_vcpus: u16,              // the endovisor picks the host CPUs at START
    pub initial_grains: u32, pub max_grains: u32,
    pub nice: i8, pub cpu_quota_us: u32, pub cpu_period_us: u32,           // per-thread `nice`; 0 = uncapped
    pub oops_budget: u32, pub preempt_off_ticks: u32, pub log_bytes_per_sec: u32, pub idle_tick_hz: u32,
    pub cmdline_ptr: u64, pub cmdline_len: u32,   // copied in during the call
    pub out_cid: u32,                // written: the sandbox's vsock CID
}
/// Enumerates registered kinds; at most 8 (chosen).
type KerneletListImages = ioc!(KERNELET_LIST_IMAGES, MAGIC, 0x02, OutData<[ImageInfoRaw; 8]>);
```

**On a sandbox descriptor**, in `Configuring`, before `START`:

```rust
/// Attaches a virtual device; returns its index on the kernelet's MMIO bus.
type KerneletAttach = ioc!(KERNELET_ATTACH, MAGIC, 0x10, InOutData<AttachArgs>);
#[repr(C)] pub struct AttachArgs {
    pub kind: u16,                   // BLOCK, CONSOLE, RNG, VSOCK, NET
    pub backing_fd: i32,             // BLOCK: a file or block device; CONSOLE, NET: an endpoint descriptor from `ENDPOINT`; else -1
    pub flags: u32,                  // BLOCK: read-only; NET: the MAC address in `arg`
    pub vcpu: u16,                   // the virtual CPU whose worker delivers its interrupts
    pub arg: u64,
    pub out_index: u16,              // written
}
/// Creates a two-ended byte stream and returns the user-space end; the sandbox keeps the other.
type KerneletEndpoint = ioc!(KERNELET_ENDPOINT, MAGIC, 0x11, InOutData<EndpointArgs>);   // kind: CONSOLE, LOG, NET
/// Creates the kernelet from the gathered configuration and starts it: `Configuring → Live`.
type KerneletStart = ioc!(KERNELET_START, MAGIC, 0x20, NoData);
```

**On a sandbox descriptor**, at any time:

```rust
type KerneletKill   = ioc!(KERNELET_KILL,   MAGIC, 0x21, InData<u32, PassByVal>);   // a reason code the status reports
type KerneletGrant  = ioc!(KERNELET_GRANT,  MAGIC, 0x22, InOutData<u32>);           // grains asked, grains given; EINVAL before START
type KerneletBudget = ioc!(KERNELET_BUDGET, MAGIC, 0x23, InData<BudgetArgs>);       // before START: the initial budget; after: `set_budget`
type KerneletStats  = ioc!(KERNELET_STATS,  MAGIC, 0x24, OutData<StatsRaw>);        // `KerneletStats` plus state and CID; zeros before START
type KerneletStatus = ioc!(KERNELET_STATUS, MAGIC, 0x25, OutData<StatusRaw>);       // state, and the exit status once exited
/// A vsock stream: connect to the kernelet's `port`, or listen on a port scoped to this sandbox for one connection from it.
type KerneletVsockConnect = ioc!(KERNELET_VSOCK_CONNECT, MAGIC, 0x30, InOutData<VsockArgs>);   // returns an endpoint descriptor
type KerneletVsockListen  = ioc!(KERNELET_VSOCK_LISTEN,  MAGIC, 0x31, InOutData<VsockArgs>);
/// Reclaims everything. `EBUSY` unless the kernelet has exited or never started; a `Zombie` is queued for the retry thread and the call succeeds.
type KerneletDestroy = ioc!(KERNELET_DESTROY, MAGIC, 0x2f, NoData);
```

**On a vsock endpoint descriptor:** `KERNELET_VSOCK_SHUTDOWN` (`MAGIC`, `0x32`, `NoData`) sends a send-side `Shutdown` for a half-close ([Channels](channels.md)). Endpoint descriptors are otherwise ordinary readable, writable, pollable streams.

`START` is where `Kernelet::create` and `Kernelet::start` are called, back to back: the control half fixes a kernelet's devices, CPUs and command line at creation ([control half](kernelet-api-control.md)), so the endovisor gathers them first and creates once. It picks `num_vcpus` host CPUs from the policy's set, the ones with the fewest virtual CPUs of other kernelets already placed on them (chosen), and fails with `EINVAL` if the set is smaller than `num_vcpus`. The kernel boot inside the kernelet begins at `START`, so a runtime that must know the sandbox is viable before reporting success starts it and waits for its agent ([kernelet runtime](kernelet-runtime.md)).

A sandbox descriptor is pollable: readable when the kernelet has exited, so that the runtime waits with `poll` rather than a blocking `ioctl`. Closing the last sandbox descriptor of a live kernelet kills it and hands the wait-and-destroy to the **retry thread**, a host kernel thread the endovisor starts at registration, so that a runtime that crashes leaves no sandbox behind and no `close` or `exit_group` blocks on a kernelet's death; the retry thread also calls `destroy` again for every kernelet that returned `Zombie` whenever a pin or an adoption is released.

## Policy

The endovisor is where the host's policy lives, and the control half has none. In the first version: a per-user cap on live kernelets, on total grains, on total virtual CPUs and on endpoint bytes; the set of host CPUs kernelets may use, from which `START` picks; the default `KerneletPolicy` values; the device models' `max_request_bytes` and the vsock credit cap and connection bound; and `on_grant_exhausted`'s answer, which is to grant up to the user's remaining cap. Which kernelets may reach which over vsock is the switch's policy, per pair of CIDs, and the default is deny: containerd runs every pod on a node as one user, so "allow within a user" would let every sandbox on the node reach every other. The runtime opens a pair with `KERNELET_VSOCK_POLICY` (`MAGIC`, `0x33`, `InData<VsockPolicyArgs>`: a peer CID and allow or deny) on the sandbox descriptor, and only a runtime that holds both sandboxes' descriptors can open a pair, which is what a pod's sandboxes share.

## What the endovisor is trusted for

Everything. It runs in kernel mode in the host kernel and holds every kernelet's hooks; a bug in it is a host bug. What limits the damage a *tenant* can do through it is that every tenant input reaches it through a checked path: register accesses through the service half's bounds, descriptors through `guest_memory`'s owner checks, packets through the switch's credit. Its own code carries `forbid(unsafe_code)`, stricter than the host kernel proper's `deny` (checked on the tree: `kernel/core/src/lib.rs`), and it is the second-largest piece of new code in the design after vOSTD, *estimated* at 8,000 to 12,000 lines with the five device models.

## Costs

- Per sandbox: the `Sandbox` object, its endpoints' queues, one device thread per device with a 512 KiB stack, and the kernelet's own cost from the [control half](kernelet-api-control.md).
- Per `ioctl`: a system call on the host; none is on a kernelet's fast path.
- Per byte through an endpoint: one copy into the queue and one out, one wakeup each way when the queue was empty or full.
- Per host: the retry thread; per kind, the boot-time copy of its image ([Builds and images](builds-and-images.md)).

## What this page decides

- **The endovisor is a module of the kernel crate, in safe Rust, over the control half** (register D40), not a module of OSTD and not a component: it holds Linux functionality, files and threads, that OSTD does not have and should not, and that the kernel crate keeps private.
- **The endovisor ABI is one character device with typed `ioctl`s and pollable descriptors** (register D41), the shape of `/dev/kvm`, because a container runtime already knows how to drive that shape and it needs no new syscalls.
- **Closing the last sandbox descriptor destroys the kernelet** (register D42), so that no crashed runtime leaks a sandbox; the price is that a runtime that wants a kernelet to outlive it must hand the descriptor to another process first.
- **`CREATE` configures; `START` creates** (register D52). The alternative, calling `Kernelet::create` at `CREATE`, needs a control-half `add_device` that mutates a created kernelet's boot arguments, against the rule that a kernelet's configuration is fixed at creation.
- **Vsock reachability between sandboxes is deny by default** (register D68); a runtime that owns both descriptors opens a pair. The alternative, allow within one user, is no policy at all on a node where one user owns every sandbox.
- **Possession of a descriptor is the capability** (register D53). The alternative, a per-call owner check, would cost a credential lookup on every `ioctl` and would make a descriptor passed to another process unusable, unlike every other descriptor on the host.
