# Devices

*Part of question 2, and the seam with question 4. Virtualizes `io::IoMem` and the DMA objects' device side; specifies what a device model in the endovisor must do. Discharges invariant I2 for frames handed to a device, and bounds the host work a kernelet's I/O induces (invariant I6).*

A kernelet sees devices the way a guest of a microVM does: virtio devices on an MMIO bus, driven by the kernel proper's own virtio drivers, unchanged. What differs is beneath the register file. A register access is not a trap into a VMM but a function call into the endovisor's **device model**, and the memory a driver hands the device in a virtqueue is read and written by that model through a checked accessor rather than by DMA. The drivers, the virtqueue code and the MMIO transport are the kernel proper's (checked on the tree: `kernel/core/comps/virtio`); the bus enumeration, `IoMem`, and everything behind it are what this page virtualizes.

## Enumeration

The virtio MMIO bus on x86-64 already probes devices from the kernel command line in Linux's `virtio_mmio.device=<size>@<base>:<irq>` form (checked on the tree: `transport/mmio/bus/arch/x86.rs`, `probe_from_kernel_cmdline`). The endovisor composes that line from the kernelet's device descriptors, and the kernelet's bus probe runs the same code with these `cfg` lines, which the [taxonomy](index.md) lists (checked on the tree, each):

- `bus/arch/x86.rs`: `probe_from_microvm_constants`, which counts I/O APICs and scans QEMU's fixed MMIO window, is compiled out; the command-line probe's lookup of `IRQ_CHIP` is compiled out.
- `bus/mod.rs`: `try_register_mmio_device`, which on the host allocates a free line with `IrqLine::alloc` and maps it through the caller's `IRQ_CHIP` closure into a `MappedIrqLine`, takes the line number from the command line and calls `IrqLine::alloc_specific(irq)` under the feature; `bus/common_device.rs` stores the result in its `irq` field, whose type `MappedIrqLine` the kernelet configuration aliases to `IrqLine`, as the LoongArch arch file already does.
- `transport/mod.rs`: the PCI transport (`virtio_pci_init`, the `pci` module, the `BarAccess` field of `VirtioTransport` and the `PortRead`/`PortWrite` bounds on `ConfigManager`) is compiled out, and the `aster-pci` dependency with it, since port I/O and PCI configuration space are the host's.

Each device has a **pseudo-physical MMIO base**, chosen by the endovisor and listed in `BootArgs` beside its size, type and line:

```rust
#[repr(C)] pub struct DeviceEntry {
    pub id: u16, pub kind: u16, pub irq: u8, pub _pad: u8,
    /// The virtual CPU whose worker delivers this device's interrupts.
    pub vcpu: u16,
    pub reg_bytes: u32, pub device_type: u32,
    /// The pseudo-physical address the kernelet's bus probe finds the register file at.
    pub mmio_base: u64,
}
```

The bases lie above the host's highest physical address, 4 KiB-aligned and pairwise disjoint, so that a pseudo-physical address can never be confused with a frame; `create` rejects a descriptor outside that rule with `CreateError::MmioBaseInvalid` ([control half](../kernelet-api-control.md)).

## `IoMem` as a register file

```rust
// vOSTD, the virtualized `IoMem`. Same public API as the host build's.
pub struct IoMem<S = Insensitive> { dev: u16, offset: u32, len: u32, cache: CachePolicy, _s: PhantomData<S> }

impl IoMem {
    /// Finds the device whose register file covers `range` in `BootArgs`; any other range
    /// is `Error::AccessDenied`. No mapping is made.
    pub fn acquire(range: Range<Paddr>) -> Result<IoMem>;
    pub fn slice(&self, range: Range<usize>) -> Self;     // narrows `offset` and `len`
    pub fn paddr(&self) -> Paddr;                          // `BootArgs.devices[dev].mmio_base + offset`
}
impl VmIoOnce for IoMem {
    fn read_once<T: PodOnce>(&self, offset: usize) -> Result<T> {
        let r = services().mmio_read(self.dev, self.offset + offset as u32, size_of::<T>() as u8);   // value in a register
        if r.status < 0 { return Err(Error::AccessDenied); }
        Ok(T::from_bytes(&r.value.to_le_bytes()[..size_of::<T>()]))
    }
    fn write_once<T: PodOnce>(&self, offset: usize, v: &T) -> Result<()> {
        let mut raw = [0u8; 8]; raw[..size_of::<T>()].copy_from_slice(v.as_bytes());
        services().mmio_write(self.dev, self.offset + offset as u32, size_of::<T>() as u8, u64::from_le_bytes(raw)); Ok(())
    }
}
// The inherent `read_fallible` and `write_fallible`, and `VmIo`'s `read`, `write`, `read_bytes`,
// `write_bytes` and `VmIoFill`'s `fill_zeros` over them, are loops of `read_once` and `write_once`
// at the widest aligned width. No driver on the tree makes a bulk access: the transport reads every
// register, and every driver reads its configuration space, one `u32` at a time (checked on the
// tree: `transport/mmio/device.rs`, `device/block/mod.rs`), so a 64-bit configuration field is two
// crossings and is not atomic against a configuration change, as on hardware.
```

Every register access is one crossing and one hook call, `KerneletHooks::mmio_read` or `mmio_write`, charged to the kernelet. `IoPort` is absent. `CursorMut::map_iomem` fails, so a tenant cannot `mmap` a register file ([Memory](memory.md)).

## What a device model is

The endovisor implements, per attached device, a **virtio MMIO device model**: the register file of the virtio 1.2 MMIO transport (magic, version 2, device and vendor ids, feature words and selectors, queue selection and size, queue ready, the three queue addresses, notify, interrupt status and acknowledge, status, and the device-specific configuration space at offset 256), backed by a host-side struct, offering neither `VIRTIO_F_INDIRECT_DESC` nor `VIRTIO_F_EVENT_IDX`, so that a chain is never a table the driver chose the size of and the notify walk stays bounded by the ring, and behind it a **backend** that does the device's work against host resources. The model's `mmio_read` and `mmio_write` are state manipulation and never sleep, because the driver holds a spin lock across a register write as it would for real hardware ([service half](../kernelet-api-service.md)). The model's queue state is under one spin lock per device, held by the hook and by the device thread only across non-sleeping sections; the interrupt-status word is an atomic. Four writes do more than update state:

- **Queue ready.** The model records the queue's descriptor table, available ring and used ring addresses and its size, and checks that all three lie in the kernelet's grant with `guest_memory`; a queue outside the grant is never marked ready and the driver sees the device fail. A `queue_num_max` read for a queue the device does not have returns 0, which is where the transport's probe of up to 512 queues stops (checked on the tree: `transport/mmio/device.rs`, `num_queues`). The queue size is at most 256, the transport's own ceiling (checked: `queue.rs`); the tree's drivers ask for 64 (checked: `device/block/device.rs`, `device/network`).
- **Notify.** Under the device lock, the model walks the available ring through `guest_memory` from where it last stopped, reading descriptors, at most the queue's size of them in total since the ring holds no more, 16 bytes each; for each chain it checks every buffer address and length against the grant, the chain's total bytes against the request bound, and the head against the set in flight, and copies the chain's description into a slot of the device's **inbox**, a ring of queue-size request slots allocated and charged at attach. The walk stops when the inbox is full and resumes at the next notify or completion. A chain that fails a check is completed at once, and the device thread never sees it. Then the model wakes the device thread and returns. The hook validates and records only; it pins nothing and copies no data. Its worst case is one pass over the whole ring, 4 KiB of descriptor reads and 256 owner checks for a 256-entry queue, *estimated* at a few microseconds, and the driver holds its queue lock, preemption off, for that long, as it does across a real notify.
- **Interrupt acknowledge.** Clears the acknowledged bits with a `fetch_and`.
- **Status zero (reset).** The model marks every queue not ready, discards the inbox, and asks the device thread to abandon what it holds; the status register reads back nonzero until the thread has done so, which is what the driver's reset spin waits for (checked on the tree: `transport/mmio/device.rs`, `lib.rs`, the driver writes 0 and spins until it reads 0). A host I/O already issued completes into nothing.

**Completion, and what a failed chain gets.** The device thread, after a request, writes the data and then the used-ring entry through `guest_memory`, sets the interrupt-status bit with a `Release` store after the used-ring write, and calls `Kernelet::raise_irq`. The driver's interrupt path reads the status, acknowledges, and only then pops the used ring (checked on the tree: `transport/mmio/multiplex.rs`), so a completion the driver has seen is one whose used entry is already visible; a completion that lands between the driver's read and its acknowledge is re-signaled by the next `raise_irq`, which is what makes the tree's ordering race-free against a model that follows this order. A chain that fails a check is completed by the hook, under the same rules: if the chain's device-writable tail, which for `virtio-blk` is the one-byte status (checked: `device/block`, `RESP_SIZE`), lies in the grant, the model writes the device's error status there and a used length covering it, and the driver fails one request with `EIO`. If that tail is itself the unowned buffer, the model has nowhere to put a status: it completes the head with used length 0 and sets `DEVICE_NEEDS_RESET` in the status register. The tree's block driver then logs an invalid used length and leaves the request in flight (checked: `queue.rs`, `pop_used_with_min_bytes`; `device/block/device.rs`, `handle_irq`), which is a hung request and a leaked descriptor inside the tenant's kernel, the same outcome a real device behind an IOMMU gives a driver that handed it an address it does not own. Neither outcome touches the host or another kernelet, which is what register D24 promises; the promise is not that the tenant's driver is protected from itself.

The **device thread** is a kernel thread of the host kernel proper (`ThreadOptions` on the tree, which is crate-private to the kernel crate, one reason the [endovisor](../endovisor.md) is a module of it), spawned per device and adopted with `adopt_current_task` so its time is charged to the kernelet, holding the host objects the device needs, taken from the runtime's own file table at attach ([control half](../kernelet-api-control.md), assumption A5). It takes requests from the inbox, performs them, writes results into the kernelet's buffers and the used ring through `guest_memory`, which pins the grains for the duration of each write, and signals as above. The kernelet's worker then runs the driver's queue callback, which pops the used ring and completes the I/O ([Interrupts and time](interrupts-and-time.md)). Cancellation, at `on_dying`, sets a flag and wakes the thread; a host I/O in progress cannot be interrupted on the tree, so the thread finishes it, abandons the rest, disowns itself and exits, and the kernelet is a `Zombie` for exactly that one I/O's duration ([Faults, termination, and reclamation](../faults-and-reclamation.md)).

**Memory movement.** A read from a block device is one copy: the device thread reads the backing file's data through the host's file `read_at` into the kernelet's buffer inside `guest_memory`, through the host's linear map, with the host's page cache as the intermediate; there is no bounce buffer unless the host path needs one, and where it does, the bounce memory is bounded per request and charged with `charge_host_bytes`. A write is the mirror. This is the same count a VMM pays when it `pread`s into guest memory it has mapped, and one fewer than a device model that bounces through a private buffer.

**Bounds, for invariant I6.** Per device: queue size at most 256 entries; requests in flight at most the queue size, with a head repeated while in flight failed at once; bytes in flight per request at most the endovisor policy's `max_request_bytes` ([The endovisor](../endovisor.md)), 4 MiB by default for block and 64 KiB for network (chosen; the tree's network buffers are 4 KiB, checked: `device/network/buffer.rs`), and per device the queue size times that; one device thread and one inbox. Host memory a request holds is charged. Host CPU time on the device thread is charged through adoption. What is not charged is the host's physical interrupt handling for the real device beneath the backing file, exactly as a hypervisor's is not charged to a guest, nor the host page cache the backing file occupies, and I/O bandwidth is metered only by these queue bounds.

## The devices of the first version

| device | virtio type | backend in the endovisor | what it costs |
|---|---|---|---|
| block | `virtio-blk` | a host file or block device, read and written with the host's file API on the device thread; flush maps to `sync` | one copy per request |
| console | `virtio-console` | an endpoint to the kernelet runtime through the [endovisor ABI](../endovisor.md), moved by the device thread; the runtime attaches a terminal | one copy per byte run, plus the endpoint's copy to user space |
| entropy | `virtio-rng` | the host's random source, filled on the device thread | none beyond the copy |
| vsock | `virtio-vsock` | the host-side vsock model of [Channels](../channels.md): connections to the runtime and to other kernelets | see Channels |
| network | `virtio-net` | a user-space backend: frames are shuttled to and from the kernelet runtime over an endpoint, and the runtime's user-space NAT, in the style of `slirp`, terminates them | two copies and two wakeups per frame, plus the user-space stack |

The network backend is user-space in the first version because the host kernel's stack has no tap device, no bridge and no packet socket family: its interfaces are the loopback and the ones `aster-network` devices provide (checked on the tree: `net/iface/init.rs`, `syscall/socket.rs`, which offers `AF_UNIX`, `AF_INET`, `AF_INET6`, `AF_NETLINK` and `AF_VSOCK`), so frames cannot be injected into it from an endpoint. **[unverified]** (register A7): whether a host-side software device implementing `aster-network`'s `AnyNetworkDevice`, whose frames the host's stack routes, would serve instead, which would save the two wakeups and the second stack. A shared file system (`virtiofs`) is the natural way to give a kernelet a root file system without a block image and is left for a later version; `mlsdisk` and the other host-only device components never appear inside a kernelet.

## `DmaStream` and `DmaCoherent` on the device side

The driver side of the DMA objects is on the [Memory](memory.md) page: frames from the grant, `daddr` equal to `paddr`, no synchronization. On the device side there is no DMA engine at all: a descriptor's address is a physical address in the kernelet's grant, the model checks it against the owner array through `guest_memory`, and the device thread reads or writes it through the linear map with the grain pinned. The owner array is written before a run is published in the grant table ([control half](../kernelet-api-control.md)), so a ring the driver places in a freshly granted run passes the check as soon as the driver can address it. `sync_to_device` and `sync_from_device` are no-ops because the CPU that wrote the buffer and the CPU that reads it are coherent. This is what "virtio over function calls" means: the descriptor format, the rings and the drivers are the specification's, and the device is a thread.

## What a tenant sees

- A virtio MMIO bus with the devices the runtime attached: `/dev/vda` and its partitions, `hvc0`, `/dev/hwrng`, `eth0`, `/dev/vsock`. `lspci` shows nothing, since there is no PCI.
- Device performance is the device thread's plus one worker wakeup per interrupt, which the Evaluation chapter measures against virtio in a microVM.
- A block request above `max_request_bytes` fails with `EIO`. The tree's ext2 builds one segment per contiguous extent with no size cap (checked: `fs_impls/ext2/inode/file.rs`, `read_direct_blocks`), so a large direct read of a contiguous file can hit the bound; the default is chosen so that it does not for ordinary files, and it is a policy.
- A driver that hands a device a buffer it does not own gets an I/O error, or, when the unowned buffer is the status byte itself, a request that never completes.
- A device's registers cannot be mapped into a process, and no device offers memory to map.

## Costs

- Per register access: the crossing, its prologue and epilogue, and the hook's dispatch, *estimated* at 60 cycles over the 35-cycle measured crossing ([service half](../kernelet-api-service.md)); a virtio driver makes a handful per request outside the notify.
- Per notify: the available-ring walk under the device lock and `guest_memory`, bounded by the queue size, and one device-thread wakeup.
- Per completion: the used-ring write under `guest_memory`, one `raise_irq`, one worker wakeup, the driver's two register crossings to read and acknowledge the interrupt status, and, for block, the kernel proper's own hop to the thread that drives its request queue (checked on the tree: `device/registry/block.rs`), a park and an unpark.
- Per device: one host kernel thread with its 512 KiB stack, one model struct, the inbox of queue-size slots, and the host objects behind the backend, charged at attach.
- Per request: bounded host memory, charged.

## The second version

The copies and the device-thread hops this page counts are what the second version removes: [Zero-copy I/O](../zero-copy-io.md) specifies a lending device model in which entries lend the kernelet's own frames and the host's drivers take them as DMA targets, with the first version's virtio models kept for images that still carry the virtio drivers.

## What this page decides

- **Devices are enumerated from the kernel command line the endovisor composes** (register D23), so the kernel proper's MMIO bus probe is reused with `cfg` lines for the interrupt and PCI steps. The alternative, a kernelet-specific bus that reads `BootArgs` directly, is a second probe to maintain.
- **Bad descriptors fail the request, never the kernelet** (register D24). A guest driver that hands a device an address it does not own is a bug in the tenant's kernel, and the microVM answer, an I/O error, is the right one; killing the sandbox would let a driver bug become a denial of service the tenant can trigger at will. What the tenant's own driver does with the failure is the tenant's.
- **The first version's network is user-space backed** (register D25), because the host has no tap, bridge or packet socket; the in-kernel alternative is A7.
- **The notify hook validates and records into a preallocated inbox; the device thread pins and copies** (register D50). The alternative, copying in the hook, would put unbounded work under the driver's spin lock; allocating the inbox in the hook would put an allocation there that can fail.
