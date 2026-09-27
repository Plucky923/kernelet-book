# Zero-copy I/O: the lending device model

*Cuts across questions 2 and 4. Specifies the second version of devices: a paravirtual device model (one the tenant's kernel knows is a software interface, not emulated hardware), built for kernelets rather than borrowed from virtio, whose block, network and inter-kernelet paths move data without the host copying it. The first version on the [Devices](virtualizing-ostd/devices.md) and [Channels](channels.md) pages stays as written. The unit costs the argument rests on were measured on this book's host in one run (an Intel Xeon E3-1270 v6, Linux 6.8) and are labeled **measured here**; the script and raw output are in [I/O microbenchmarks](../../notes/io-microbenchmarks.md).*

## Where the time goes {#where-the-time-goes}

A kernelet today sees virtio devices: rings in the tenant's memory where the driver writes **descriptors** (the address and length of a buffer) and notifies the device, which fills or drains the buffers and writes a completion. In a microVM the notification is a **VM exit**, a hardware transition out of the guest, and the work is done by a thread of the virtual machine monitor, the user-space program that runs the VM (Firecracker is one), which copies data between the guest's buffers and the host's files or sockets. In the first version of kernelets the notification is a function call, but the rest has the same shape: a host **device thread** wakes, copies, and raises an interrupt.

What does a copy cost against the other things a request pays for? Measured here:

| what | cost | note |
|---|---|---|
| copying one 1,500-byte packet | 14 ns hot in cache; **231 ns cold** | |
| copying one 4 KiB page | 33 ns hot; **457 ns cold** | cold is the realistic case for I/O data: 9 GB/s |
| copying 64 KiB | 1.2 µs hot; 6.4 µs cold | 10 GB/s cold |
| copying a 2 MiB grain | 61 µs hot; 156 µs cold | 13 GB/s cold |
| waking a thread on another CPU and being woken back | 5.8 µs round trip | **about 3 µs per wakeup**, an upper bound: a user-space round trip with four system calls |
| unmapping one 4 KiB page while three other CPUs run this process | 2.9 µs; 4.0 µs for a protect-and-restore pair | a **TLB shootdown**: the other CPUs must be told to forget the mapping, which any change of ownership pays |

Published: on 2013 hardware a VM exit with the device emulation it triggers cost 3–10 µs. A device register access cost 1.2 µs when the kernel part of the hypervisor handled it and 3.7 µs when a user-space monitor did, against 100–200 ns natively (Rizzo, Lettieri and Maffione, ANCS 2013, sections 2.1 and 2.4). Firecracker's block device does about 13,000 I/O operations per second where the same hardware does 340,000 natively (Agache et al., NSDI 2020, evaluation).

Two things follow. **For small requests the copy is noise**: 14–231 ns against a 3 µs wakeup or a 1–10 µs exit, so a "zero-copy" packet path that still wakes a thread per packet has saved nothing measurable. **For bulk the copy is the bill**: one host core copies about 10 GB/s cold, so a sandbox moving 10 GB/s costs a core in copying alone. The metric this page optimizes is therefore **host CPU per request as a function of request size**: a fixed term (exits, wakeups, crossings, validation) plus a per-byte term (copies). The design attacks both; [the argument](zero-copy-io.md#the-argument) is made for both.

## The idea {#the-idea}

A **lending device** is a device you lend frames to. Its rings live in the kernelet's memory like virtio's, and the host reads and writes them only through `guest_memory`, the [control half](kernelet-api-control.md)'s checked accessor, which refuses any range outside the kernelet's grant; nothing is mapped read-write on both sides. What an entry carries is different: not addresses into some buffer, but a list of the kernelet's own **frames**, lent to the device for the life of one request. The host checks in constant time that each frame's [grain](virtualizing-ostd/memory.md) belongs to the kernelet, marks it lent, and hands the frame's address straight to the host's own device driver as a **DMA** target, the address at which a device reads or writes memory without the CPU's help. The physical device (a NIC, the network card, or an NVMe drive, the common solid-state disk), or on a cloud host the hypervisor beneath the host's virtio driver, reads from or writes into the tenant's frame. When the device completes, the host marks the frames returned, writes a completion entry and raises the kernelet's interrupt line.

```mermaid
flowchart LR
  subgraph kl ["kernelet"]
    drv[lending driver] -->|writes entry, lends frames| sq[(submit ring)]
    cq[(complete ring)] -->|reads, returns frames| drv
    fr[tenant frames]
  end
  subgraph hk ["host kernel, endovisor"]
    hook[notify hook: copy entry out,<br/>validate, lend, post] --> hd[host driver:<br/>NVMe or NIC, or virtio]
    hd -->|completion| cw[completion writer:<br/>return frames, entry, raise_irq]
  end
  sq -.->|guest_memory| hook
  cw -.->|guest_memory| cq
  hd <-->|DMA| fr
  drv -->|dev_notify| hook
```

Three rules carry the design. **Lend**: a frame in flight is still the tenant's, but the tenant's kernel cannot free or reuse it, enforced twice ([lending](zero-copy-io.md#lending)). **The host acts only on bytes it copied out**: an entry is read whole into host memory and validated there, and every header the host, a device or a receiver interprets is the host's copy. **No thread on the host's fast path**: submission happens in the notify call, bounded and without sleeping, and completion comes from the host driver's completion path.

## Rings, entries and notifications {#rings}

A lending device has a **submit ring** and a **complete ring**, each a power-of-two array of fixed-size entries in the kernelet's grant, at most 256 entries (chosen, virtio's ceiling). The driver registers them at probe with `dev_ring_set`, which is refused while any request is in flight. The kernelet writes the submit ring and reads the complete ring; the host does the reverse; the counters each side publishes sit in its ring's first entry.

```rust
// ostd::kernelet::abi. Submit: kernelet-written, host-read through guest_memory. 536 bytes.
#[repr(C)] pub struct Submit {
    pub id: u32,          // below the ring size, unique among requests in flight; echoed back
    pub op: u16,          // READ, WRITE, FLUSH, TX, RX_POST
    pub nseg: u16,        // segments lent, 1 to 16; 0 for a header-only channel packet
    pub sector: u64,      // block ops: the first sector
    pub hdr_len: u16,     // TX: bytes of `hdr` that are the packet's headers
    pub csum_start: u16,  // TX offload: where the checksum starts, and where it is stored
    pub csum_off: u16,
    pub gso_size: u16,    // TX offload: segment size, 0 for none
    pub hdr: [u8; 256],   // TX: the packet's headers. Channel: the vsock header; on RX_POST, the connection it is for
    pub seg: [Seg; 16],   // each a run of physically contiguous frames inside one grain
}
#[repr(C)] pub struct Seg { pub paddr: u64, pub off: u16, pub len: u32 }
// Complete: host-written, kernelet-read.
#[repr(C)] pub struct Complete { pub id: u32, pub status: u16, pub _pad: u16, pub bytes: u64 }
```

A segment is a run of physically contiguous frames inside one grain. Page-cache pages are single frames on the tree (checked: `vm/page_cache/mod.rs`), so an entry carries up to sixteen pages, 64 KiB, and `lend-blk` splits larger requests into several entries. `id` is below the ring size, so the host's table of requests in flight is a preallocated array. **Offload** fields ask the NIC to compute checksums and to split one large segment into packets itself.

**Notify.** `dev_notify(dev)` is a service call with no pointer, like every other ([service half](kernelet-api-service.md)). The hook runs under the device's lock, taken with interrupts disabled because completions take it too, and never sleeps. It copies each new entry out of the submit ring **whole** through `guest_memory` and validates the copy:

- `op` valid for the device; `nseg` at most 16; every `len` nonzero; `id` below the ring size and not in flight (a duplicate is a driver fault that stops the device until reset);
- every segment: `paddr` page-aligned, `off + len` inside the grain; the entry's total bytes at most the endovisor's request bound;
- block: `off` and `len` sector multiples; `[sector, sector + bytes)` inside the device's **extent** (its range of the host's disk), in checked arithmetic;
- `TX` on a network device: the EtherType (the field that says which protocol the packet carries) is IPv4, IPv6 or ARP, with no VLAN tag (a virtual-network label the host does not honor). Every header the host parses, including the transport header the offload fields point into, lies within `hdr_len`, at most 256. `csum_start + csum_off + 2` is inside the packet. `gso_size` is zero or at least 512 (chosen: one entry yields at most 128 packets);
- `RX_POST` on a network device: the frame is at least the device's maximum frame length;
- on a channel device: a `TX`'s vsock header names this kernelet as source, and an `RX_POST`'s names it as destination and a live connection of the switch table, or is all zero for the control pool ([between kernelets](zero-copy-io.md#between-kernelets)).

For each segment it then advances the grain's lend count with one compare-and-swap on the **owner-and-lend word** ([lending](zero-copy-io.md#lending)); if a swap fails, the earlier ones are undone and the entry is completed with an error. Then it **posts the request to the host driver**: a block request over the lent frames, or a transmit whose first segment is a host-owned buffer holding the validated header, followed by the lent frames. A request the host driver cannot take now waits in a per-device **inbox** of ring-size slots preallocated at attach, the first version's inbox with the copy removed (register D50). The hook handles at most 32 entries per call (chosen; 8 on a channel device, whose entries copy) and leaves the rest for the next notify or completion. Its worst case, 32 entries and 512 swaps, is *estimated* at 20–60 µs with cold owner words, with interrupts off: the host's interrupt-latency cost of a full ring, which a tenant can incur at will and the cap bounds.

**Complete.** The host driver's completion path, often an interrupt handler and by contract holding no driver lock (A14), calls the model's `complete(dev, id, status, bytes)`. It takes each lent grain's count down, always, even for a kernelet that is dying and whose ring it may no longer write; writes a `Complete` entry through `guest_memory`, which sleeps nowhere; and calls `Kernelet::raise_irq`, with a `Release` store ordering the entry before the line. The kernelet's worker runs the driver's handler, which drains the ring.

**Interrupt suppression.** The submit ring's first entry holds a **polling bit** the kernelet writes. While it is set the host writes completions and raises nothing. The bit is the OR of two conditions the driver tracks. For requests the kernelet initiates (`READ`, `WRITE`, `FLUSH`, `TX`): more than four are in flight (chosen); the driver then spins on the complete ring from a thread it already owns, the kernel proper's per-device block thread (`device/registry/block.rs`) or the interface's poll thread (`kernel/core/src/net/iface/poll.rs`), until fewer than four remain. For receives: an interrupt arrived and the ring is not yet drained. After clearing the bit the driver reads the ring once more, and host and driver each put a full `SeqCst` fence between writing their flag and reading the other's, the classic two-flag hand-off; without it a completion can be missed, and stays missed until the next one. The poll loops are new code in the kernel proper's drivers.

## Lending, and what enforces it {#lending}

A lent frame is still the tenant's: its processes may read and write it. What must not happen is that it **changes hands** while the device holds its address: freed and reallocated inside the kernelet, or returned to the host and granted to another tenant. Two independent mechanisms prevent this.

**In the kernelet, a reference held by the driver.** A `BioSegment` holds a `DmaStream` that keeps its frames allocated for as long as any reference lives (checked: `bio.rs`, `dma_stream.rs`), and the lending driver keeps the request in its table of requests in flight until the completion arrives; a network driver holds the frame it lent the same way. That is reference counting plus driver discipline, the rule Linux relies on too, in a kernel proper that is `deny(unsafe_code)`.

**In the host, a count.** The owner array's entry becomes an **owner-and-lend word**: the kernelet's 16-bit slot, its 32-bit generation, and a 16-bit lend count in one 64-bit word. The hook advances the count by one compare-and-swap that fails if the owner is not this kernelet or the count is at its ceiling (reachable only with sixteen devices lending the same grain, and then the entry is refused), and completion takes it down. A grain with a nonzero count is not moved and not freed at destroy until the count is zero: the count joins the pin count in the first step of destroy's drain list, and a host driver's in-flight request joins the structures the drain must find empty ([Faults, termination, and reclamation](faults-and-reclamation.md)). At `on_dying` the endovisor unpins the rings, cancels what the host driver can cancel, and **resets the kernelet's receive queue**, which at tier 2 is the kernelet's own and returns its posted frames at no one else's cost; the frames a channel device posted are entries in the switch's tables and are dropped there. Destroy never waits (register D36); the kernelet stays a `Zombie` until the last initiated request completes, which is bounded only if the host drivers time requests out, which the tree's do not (assumption A14, **[unverified]**). The count is what protects other tenants if the kernelet's kernel is wrong: it can corrupt its own data through a reused frame, and cannot make the host grant that frame to anyone else while a device may still write it.

**What a tenant can still do, and why it does not matter.** It can write a page while the device reads it and get torn data, as on every operating system with a page cache; the effect stays inside the tenant, because everything the host or the device decides on comes from the host's copy. It cannot program the device: a frame is written only because a validated entry named it and host code posted it. On bare metal the host's IOMMU, the unit that bounds what a device may address, protects against a faulty device and is not part of the tenant argument (register D77); on a cloud host the hypervisor bounds every DMA to the host's memory.

## Block {#block}

The kernelet's block layer already works in frames: a read is a `Bio` whose segments are page-cache frames (checked on the tree: `bio.rs`, `vm/page_cache/mod.rs`; writeback still copies each page into a pool segment, which the kernel proper could stop doing on both sides of the comparison). The `lend-blk` driver implements the same `enqueue` the virtio-blk driver does but writes one `Submit` per sixteen pages. What the model removes is the host side.

```mermaid
sequenceDiagram
  participant P as tenant process
  participant K as kernelet (page cache, lend-blk)
  participant H as endovisor (notify hook)
  participant D as host block driver
  participant S as device (NVMe, or virtio-blk under a hypervisor)
  P->>K: read()
  K->>K: page-cache frames, Bio, Submit entry, frames lent
  K->>H: dev_notify (service call)
  H->>H: copy entry out, validate, lend
  H->>D: submit_nowait over the lent frames
  D->>S: post, DMA target = tenant frames
  S-->>D: completion interrupt (the host's)
  D->>H: complete(id)
  H->>K: Complete entry, raise_irq
  K->>P: data, in place
```

**Two tiers.** At **tier 2**, the hardware tier, the device is backed by an extent of a host block device or a partition. A host file is not an extent until the host file system can pin a file's blocks against truncation and reallocation, as a swap file is pinned on Linux; the tree's file systems cannot, and a file whose blocks moved would let a tenant's DMA land on another tenant's blocks. The hook posts the request to the host's block driver as a DMA over the lent frames; the device writes into the tenant's page cache, and the host's own page cache is not involved. **Tier 1** is the first version's file-backed device behind a lending ring: a device thread reads the file into the lent frames with `read_at`, one copy from the host's page cache.

The host code tier 2 needs does not exist on the tree, whose block layer submits through a sleeping lock and a per-device thread and whose NVMe driver issues one command at a time; assumption A14 lists it, `submit_nowait` first. Until it exists, tier 2 block pays one host-thread wakeup per batch, the same fixed cost as a kernel `vhost` backend, and still no copy.

## Network {#network}

On the tree the stack copies each packet into a heap buffer the driver supplies, and the virtio driver copies it again into a DMA pool (checked: `network/src/driver.rs`; `buffer.rs`). A `lend-net` driver must copy a heap buffer into a frame too, so on the kernelet side it saves nothing until the kernel proper hands drivers frame-backed buffers, which is that side's work and the same on both sides of every comparison here. What this page removes is the host's copy. The user ABI is unchanged.

```mermaid
flowchart LR
  subgraph kl ["kernelet"]
    st[network stack] -->|packet| tx[lend-net TX entry:<br/>headers in entry, payload lent]
    rxq[RX_POST entries:<br/>empty frames lent] --> st2[network stack]
  end
  subgraph hk ["host kernel"]
    hook[notify hook: validate headers,<br/>host header buffer] --> nic[host NIC driver TX:<br/>host header + tenant frames]
    rq[receive] -->|steered queue per kernelet| rz[zero copy: the NIC DMAs<br/>into the lent RX frame]
    rq -->|shared queue| rc[one copy: host frame to<br/>the lent RX frame, by MAC]
  end
  tx -->|dev_notify| hook
  rz --> rxq
  rc --> rxq
```

**Transmit.** The driver writes the packet's Ethernet, IP and transport headers, up to 256 bytes, into the entry's `hdr`, fills the offload fields, and lends the payload that follows the headers as segments. The hook validates the copy. The source MAC (the link-layer address) must be the device's and the source IP one the sandbox was given. An ARP packet (the protocol that maps IP to MAC addresses) must carry those same addresses in its sender fields, and an IPv6 neighbor-discovery packet (ARP's IPv6 counterpart) in its source and target fields, or it is dropped, so a tenant cannot poison its neighbors' caches. Router advertisements and redirects, which would let a tenant hijack its neighbors' routes, are refused. The hook then builds the offload header from the validated fields and posts a transmit to the host NIC driver, virtio-net on a cloud host: the host header first, then the lent payload. A packet whose headers exceed 256 bytes is lent whole in one frame; the hook copies its headers out, validates them, and transmits its own copy first with the frame offset past them.

**Receive, two tiers.** A packet lands in the host's receive buffers first, so the tier depends on whether the tenant's frames can *be* those buffers. **Tier 2** needs a **per-kernelet receive queue**: a NIC that steers by destination MAC to a queue of its own (a virtual function, which is a slice of a NIC that behaves as a separate device, or a filter per kernelet on a multi-queue NIC), or on a cloud host one virtio-net device per kernelet. Then the frames the kernelet lends with `RX_POST` are what the host driver posts to that queue, the device DMAs the packet into the tenant's frame, and the host completes the entry with the length: zero copies. A device that writes its own header in-band, as virtio-net does, gets a host-owned descriptor at the head of the chain for it; a lent frame is never the first descriptor, so the host driver never parses tenant memory (A14). **Tier 1** is a shared queue. The host receives into its own frame and looks the destination up: a unicast MAC in its table; a multicast group (one address, several receivers) to the kernelets whose drivers subscribed; a broadcast to every kernelet on the segment, at a rate the endovisor caps per segment (chosen: each broadcast is a copy per kernelet charged to no one); anything else dropped. It copies the packet into that kernelet's next lent frame (231 ns cold for 1,500 bytes), completes it, and recycles its own frame. On a cloud host that allows one MAC per instance, tier 1 also rewrites the source MAC on transmit and demultiplexes receives by IP. That copy is real, 12–77 % of the polling row's fixed cost in the argument below.

## Between kernelets {#between-kernelets}

The Channels page sends every inter-kernelet byte through a vsock switch that copies twice and hops through two device threads. The `lend-chan` device keeps the vsock socket layer, whose packets are a 44-byte header naming a connection by its two addresses and two ports plus an optional payload, and replaces the transport beneath it.

**Frames are credit.** Channel frames are 16 KiB (chosen, the per-entry copy cap). A receiver lends empty frames with `RX_POST`, its `hdr` naming the connection the frame is for, and its socket layer advertises to the peer one frame of credit per posted frame, four per connection (chosen, the Channels page's 64 KiB). The sender's socket layer charges a whole frame per data `TX`, fills a frame before starting another, and puts at most one frame's payload in one. So a data `TX`, the vsock header in `hdr` and the payload lent, is never sent within its credit without a posted frame. The switch, in the sender's notify hook, validates the header as today and takes the connection's next posted frame through the receiver's `guest_memory`, which refuses a receiver that is dying. It writes the validated header at the head of the frame, **copies the payload once, frame to frame** after it, the layout the tree's socket layer already reads, and completes both entries. A data `TX` with no posted frame is a protocol violation, completed with an error, not queued, so one slow connection cannot stall the others: the head-of-line rule the Channels page keeps (register D51). When the switch tears a connection down, the receiver's posted frames for it are completed with an error. A connection to a host endpoint, the runtime's control connection or a console, is the Channels page's: the switch copies the payload into the endpoint's byte queue, and the endpoint's bound is the credit.

**Control packets**, the header-only ones that open, close and reset connections and update credit, are `TX` entries with no segments. They land in a **control pool** of small frames every channel device posts with an all-zero `hdr`, bounded and charged to the receiver, of which one slot per live connection and direction is reserved so that a credit update or a reset for a live connection is never dropped; connection requests, which precede any switch entry, draw on the rest under a per-sender quota (chosen), and a sender past its quota has its entry refused. A host queue of 64 headers per device (chosen) holds control packets only while the pool is empty. Posted frames, control pools and each channel device's complete-ring cursor are kept in the switch's own tables under the switch's lock, so a channel hook takes no other kernelet's device lock, and the payload copy runs outside that lock. The receiver's credit advances when it re-posts a frame, not when a process reads it, which with fill-before-charge is a change to the kernel proper's socket layer.

```mermaid
flowchart LR
  sa[kernelet A, sender:<br/>TX entry, payload frames lent] --> cp[host switch, in A's notify hook:<br/>validate header, write it, copy payload once] --> rb[kernelet B, receiver:<br/>RX_POST entry for the connection]
```

## The argument {#the-argument}

The metric: host CPU per request, a fixed term plus a per-byte term. The first table counts what each path pays per request; the microVM rows are on bare metal, as Firecracker's paper measured, and a kernelet host that is itself a cloud VM pays its hypervisor's exits like every process on that host. *Vhost* is a Linux host-kernel backend for virtio that takes the user-space monitor off the data path; *vhost-user*, *SPDK* and *DPDK* are user-space backends that spin on a dedicated core so that no notification is needed.

| per request | VM exits | thread wakeups | host copies of the payload | source of the counts |
|---|---|---|---|---|
| microVM, user-space monitor, block (Firecracker) | 2–3: the notify, handled in the kernel through an event descriptor Firecracker registers per queue (**[unverified]** against its source); the interrupt; an acknowledge that returns to the monitor | 1–2 (the monitor's thread; the guest's CPU thread if idle) | 1 (from the host page cache) | virtio's memory-mapped transport; Firecracker's paper |
| microVM, kernel `vhost`, network | 1–2 | 1–2 (the vhost thread; the guest's CPU thread if idle) | 1 | vhost's design: the notification still exits |
| microVM, polling backend (`vhost-user`, SPDK, DPDK) | 0 under load | 0 | 1 | the backend spins on a dedicated core |
| microVM, virtual function passed through | 0 under load | 0 | 0 | the guest drives the NIC slice itself |
| **kernelet, lending, interrupt-driven** | **0** | **1** (the worker, at completion); 2 for block until A14 exists | **0** at tier 2; 1 at tier 1 | this page |
| **kernelet, lending, polling** | **0** | **0** | **0** at tier 2 | this page |

Unit costs: an exit that returns to a user-space monitor, **2–5 µs**, and one the kernel part of the hypervisor handles, **0.5–2 µs** (assumption A15, **[unverified]**, scaled from Rizzo's 2013 figures above); a wakeup about 3 µs measured here, an upper bound; the hook, up to sixteen owner words that may be cold plus one host driver submission, **0.3–2 µs** *estimated*.

| fixed term per request | microVM, user-space monitor | microVM, `vhost` | kernelet, interrupt-driven | kernelet, polling |
|---|---|---|---|---|
| exits | 2.5–9 µs | 0.5–4 µs | 0 | 0 |
| wakeups | 3–6 µs | 3–6 µs | 3 µs | 0 |
| validation and posting | inside the monitor's time | inside the vhost thread's time | 0.3–2 µs | 0.3–2 µs |
| **total** | **5.5–15 µs** | **3.5–10 µs** | **3.3–5 µs** | **0.3–2 µs** |

Left out on purpose: the kernel proper's own hop to its block request thread, inside the tenant's budget on both sides; the reschedule interrupt `raise_irq` sends when a kernelet task occupies the worker's CPU; the host driver's interrupt handling, which a native process pays too. The completion writer's work inside that handler is uncharged, where the first version charged its device thread: a gap in invariant I6 to close. The per-byte term is one cold copy (457 ns per 4 KiB) for every microVM row but the virtual function, and none for the kernelet at tier 2: **0.1 core per GB/s**, nothing at a sandbox's usual rate and a whole core at 10 GB/s.

The claim, with its limits. **Per request, interrupt-driven, the lending model costs 1.1–4.5× less than a user-space monitor and about the same as `vhost`**: it removes every exit, and its one wakeup is the wakeup vhost pays too. **With polling it costs roughly 2–30× less than `vhost` and 3–50× less than a user-space monitor**, because the poller is the kernelet's own thread on the tenant's own core: charged to the tenant, spinning only while that tenant has requests in flight. **Per byte, at tier 2, it removes the host copy**, which matters above a few GB/s per sandbox and is what SPDK and DPDK exist to remove; the kernelet gets it without a user-space driver, because the host's frames are the tenant's frames. **What it does not claim**: to beat a polling backend or a passed-through virtual function per request; that tier 1 receive is zero-copy; or that the hook's cost is measured or the block fast path exists on the tree, neither of which is true.

## Costs {#costs}

- Per device: two rings in the grant, the inbox, and the host driver's queue entries the device may hold, bounded by the ring size; for tier 1 block, one device thread; for a channel device, its control pool and header queue.
- Per request: one crossing per notify batch, one entry copy and validation, one compare-and-swap per segment, one host driver submission, one completion write, one `raise_irq` per completion batch or none under polling.
- Per grain: two bytes of lend count inside the owner word; one IOMMU mapping at grant on bare metal.
- Code that does not exist: host side, assumption A14 and steering (A16); kernelet side, the three drivers, the poll loops, frame-backed transmit buffers, and a socket layer that charges credit in frames and advances it at re-post.

## What this page decides {#decides}

- **Lending devices: rings read through `guest_memory`, entries that lend the kernelet's own frames, host drivers that take them as DMA targets** (register D70).
- **The host, the device and a receiver act only on the host's validated copy of an entry's bytes; a lent frame is never a device's first descriptor** (register D71).
- **Lending is enforced by a driver-held reference and by a lend count in the owner-and-lend word, one compare-and-swap with the owner check; the count is in destroy's drain list** (register D72).
- **Submission in the notify hook, at most 32 entries per call, non-sleeping, interrupts off, into `submit_nowait`; completions hold no driver lock** (register D73).
- **One polling bit with `SeqCst` fences, the OR of an initiated-request threshold and an undrained receive interrupt** (register D74).
- **Receive is zero-copy only with a per-kernelet queue; a shared queue costs one copy; transmit is refused unless every address the packet asserts is the sandbox's** (register D75).
- **Inter-kernelet transfer copies once into a posted 16 KiB frame, credit being posted frames; control packets take a reserved pool; the switch's tables are under the switch's lock** (register D76).
- **Grains are mapped into the host's IOMMU domain at grant, on the device side only** (register D77).

Published figures: L. Rizzo, G. Lettieri, V. Maffione, "Speeding up packet I/O in virtual machines", ANCS 2013; A. Agache et al., "Firecracker: lightweight virtualization for serverless applications", NSDI 2020. Measured-here figures, with the script and raw output: [I/O microbenchmarks](../../notes/io-microbenchmarks.md).
