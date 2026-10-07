# Devices

*How a kernelet does I/O without hardware: virtio devices whose register accesses are function calls, and device models in the endovisor that turn requests into work for Linux's own subsystems. It discharges the ownership rule for memory handed to a device, and bounds the work a tenant's I/O can induce in Linux.*

## What a kernelet sees

A kernelet sees what a guest in a small virtual machine sees: a handful of **virtio** devices (a disk, a network interface, a console, a socket to the host) on a memory-mapped bus, described on its command line in the form Linux itself uses (`virtio_mmio.device=<size>@<base>:<irq>`). The kernel proper's virtio drivers are unchanged. virtio is a good fit because it has almost no registers: a driver places requests in rings in its own memory, writes one register to say "look", and gets an interrupt when there are answers.

What differs is below the register file. In a virtual machine a register access traps into a hypervisor. In a kernelet it is a function call.

## A register access is a function call

OSTD's `IoMem` type represents a range of device registers. In vOSTD it is not a mapping: it is a device number, an offset and a length. `IoMem::acquire(range)` succeeds only if the range is one of the register files listed in the kernelet's boot arguments, and maps nothing. Each `read_once` or `write_once` is one service call, `mmio_read(dev, offset, width)` or `mmio_write(dev, offset, width, value)`, which the endovisor routes to the **device model** for that device.

A device's "physical" register addresses are chosen by the endovisor above the machine's highest real physical address, so no value a kernelet can name as a register is also a frame. Port I/O does not exist in a kernelet, and a tenant cannot map a register file into its address space.

## A device model has two halves

**The register half** runs inside the service call, on the kernelet's own carrier, charged to the sandbox. It implements the virtio MMIO register file as state in endovisor memory. It never sleeps, because a driver may hold a spin lock across a register write, as it would on hardware. The one register write that does real work is *notify*: the model walks the driver's ring, copying each request's *description* into a bounded inbox first and checking, in the copy, that every buffer it names lies in the kernelet's grant, using the [owner array](memory.md); the ring is kernelet memory that another virtual CPU may be rewriting, so nothing is validated in place and the device thread later uses only what the inbox holds, never a re-read of the ring. It reaches the ring and the buffers through Linux's direct map, because they are frames, not tenant addresses. It returns having moved no data. The walk is bounded by the ring's size, which the model, not the driver, limits: a driver's write to the queue-size register is refused above 256.

**The backend half** runs on a **device thread**, one per device, a Linux task that runs only endovisor code, which sleeps until the inbox is non-empty. It does the I/O against a Linux object, writes the results into the kernelet's buffers and the completion into the used ring, and calls `raise_irq` ([Interrupts and time](interrupts-and-time.md)). This is the half that may sleep.

| virtual device | Linux object behind it | how the device thread drives it |
|---|---|---|
| block | a file or block device the runtime opened | [`vfs_iter_read()`](https://elixir.bootlin.com/linux/v6.12/source/drivers/block/loop.c#L283) and `vfs_iter_write()`, as Linux's loop driver does |
| network | a TAP interface the runtime opened | the TAP device's socket, from [`tun_get_socket()`](https://elixir.bootlin.com/linux/v6.12/source/drivers/net/tun.c#L3787), with `sock_sendmsg()` and `sock_recvmsg()`, as vhost-net does |
| console | the endovisor's log | a bounded ring the runtime reads |
| socket to the host and to other sandboxes | the endovisor's own switch | [Channels](../channels.md) |

All of those functions are exported. Driving a file or a socket from in-kernel code with no user program behind it is established practice in Linux; the loop driver and vhost are the precedents. That settles, for this host, a question the design leaves open on the other one.

**Where the objects come from.** A device thread has no file descriptors. The [kernelet runtime](../kernelet-runtime.md) opens each backing file and TAP interface itself, under its own credentials and Linux's own permission checks, and passes the descriptors to the endovisor when it configures the sandbox. The endovisor takes a reference to the open file. A sandbox can therefore reach exactly what its runtime could open, and nothing is named by path inside the kernel.

**Who pays.** Register-half work is on the sandbox's carrier, so it is charged already. A device thread must be charged too, and an ordinary kernel thread cannot be: moving one into a control group fixes its processor accounting, but Linux never charges kernel memory to a kernel thread, whatever group it is in. So device threads are not kernel threads. They are created by the root carrier, when the sandbox starts, with [`vhost_task_create()`](https://elixir.bootlin.com/linux/v6.12/source/kernel/vhost_task.c#L118), the exported helper Linux's own vhost uses for the same purpose: it makes a worker that runs only kernel code but is a thread of its creator, with the creator's address-space descriptor and control group, so that its processor time and the memory Linux allocates for its I/O are the sandbox's. The helper asks two things of its caller: a function to run if the thread group is killed, which here tells the device model to cancel its I/O and return, and that the worker be ended with the matching `vhost_task_stop()`, which is also what frees it; the root carrier does that for each device thread [before the kernelet is *exited*](../faults-and-reclamation.md#stopping). Being a copy of the root carrier's task structure, the worker also inherits the root's gate pointer; its start function clears it, since a device thread is not a carrier. Work in Linux's interrupt handlers for the physical device underneath is charged to whatever it interrupts, as for any process; the design bounds it by the depth of the rings, since a tenant can have no more requests in flight than its rings hold.

## Buffers handed to a device

A driver names its buffers by physical address. On a real machine a device would reach them by DMA, and an IOMMU would confine it. Here the "device" is endovisor code that reads and writes the frames through the direct map, after checking each against the grant. The check is what the ownership invariant requires: a kernelet can make the endovisor touch only memory the kernelet owns. OSTD's DMA types (`DmaCoherent`, `DmaStream`) keep their interface and, in vOSTD, do nothing but hand out the frame's physical address, since there is no IOMMU mapping to make and no cache to synchronize.

A device thread's use of a kernelet's frames outlives the service call that queued the request. What makes that safe is an ordering, not a count: a grant is freed only by [destroy](../faults-and-reclamation.md#destroy), destroy needs the kernelet to have *exited*, and a kernelet has not exited until its device threads have finished or cancelled their I/O and been joined.

## Costs

- **Per register access**: one service call and one indirect call into the model. A virtio request needs one such access, the notify.
- **Per request**: a walk of its descriptors with one owner check per buffer; one wakeup of the device thread; the backend's I/O; one virtual interrupt.
- **Per byte**: one copy between the kernelet's buffer and Linux's page cache or socket buffer. [Zero-copy I/O](../zero-copy-io.md) is about removing it.

## What a tenant sees

A virtio disk, a virtio network interface, a console and a vsock device, with the performance of a paravirtual device whose "hypervisor exit" costs a function call. It cannot see or name any physical device.

## What this page decides

- **Devices are virtio over function calls, with models in the endovisor** ([register D6](../../../notes/design-register.md#decisions) and D23, kept). Both hosts share this; only the backends differ.
- **Backends run on device threads created by the root carrier as `vhost_task` workers, against objects the runtime opened** ([register D102](../../../notes/design-register.md#decisions)). A plain kernel thread moved into the control group was the first choice, and is wrong: Linux does not charge kernel memory to kernel threads. The alternative, doing backend I/O inside the service call, would make a carrier sleep in Linux for the length of a disk request while holding the driver's lock.
