# Channels

*How a kernelet talks to the host and to other kernelets, given that it shares memory with neither. It discharges the ownership rule for data that moves between sandboxes, and the rule that what the host remembers about a connection outlives no kernelet.*

## A socket family the tenant already has

A kernelet has no shared memory with the host or with its neighbors, and the design does not add any. It communicates the way a guest in a virtual machine does: over **vsock**, the socket family (`AF_VSOCK`) made for talking across a virtualization boundary, in which an endpoint is named by a **context identifier** (CID) and a port rather than an address and a port.

Nothing in the kernelet changes for it. The kernel proper already has a vsock socket layer and a virtio-vsock driver (*measured on the tree*: 865 lines of driver, plus the socket layer under `net/socket/vsock`), and both are unchanged; what the kernelet runtime attaches is one virtio-vsock device, whose model in the endovisor is a **vsock switch**. A sandbox without such a device has no channel but its console.

One mechanism therefore serves three purposes: the runtime's control connection to the agent inside the sandbox, any stream a tenant wants to the host, and traffic between sandboxes where the operator's policy allows the pair.

## Addresses

A sandbox is given a CID when it is created, a `u32` in `[3, u32::MAX)` drawn from a counter that is never reused while the machine is up, so that a CID a peer has cached can never reach a successor. CID 2 is the host, as the specification says; 0 and 1 are reserved, and `u32::MAX` means *any*. The driver reads the CID from the device's configuration space, which the model serves, and the device has the three queues the specification requires. The driver posts one buffer on the event queue at initialization and resets every connection if that buffer is ever completed; the model accepts it and never completes it, because a kernelet's CID never changes.

## The receiver's contract {#contract}

The switch has to interoperate with the kernel proper's own socket layer, whose behavior fixes what the switch must do (*measured on the tree*): it advertises a receive buffer of 256 KiB in every header; it sends a credit update after consuming a quarter of that; it sends a credit request when its view of the peer's credit reaches zero, and blocks the sender until an update arrives; and it resets a connection whose queued bytes would exceed its own buffer. Its receive buffers are 4,096 bytes including the 44-byte header, sixty-four of them, and a transmit packet carries at most 4,052 payload bytes. Its connect timeout is two seconds and its close timeout eight.

This is the kernelet's side of the contract, and it is the same on every host.

## The switch {#switch}

Behind the device model sits the switch, one per machine, in the endovisor. It holds a table of **connections**, each keyed by the four-tuple of CIDs and ports, and each holding the two endpoints, the per-direction credit, and a bounded queue of payload bytes in flight in endovisor memory, charged to the sandbox that sent them. The number of connections a sandbox may hold is bounded by policy (256, *chosen*), each entry charged to it; a request beyond the bound is answered with a reset.

It does three things.

**It routes, and it does not believe headers.** A packet is taken from a sandbox's transmit queue by that sandbox's *device thread*, the endovisor task that drives a virtual device's backend. Its header was written by the tenant's kernel, so the switch overwrites the source CID with the sender's real one before it looks at anything else; without that, one sandbox could speak as another or as the host. It then delivers by destination CID: to the host endpoint for CID 2; to another sandbox's receive queue if that sandbox is alive and the policy allows the pair; and with a reset otherwise, including for the sender's own CID, for CID 1, and for a CID marked dead. Connection setup and teardown are forwarded as the specification says.

**It copies, and it enforces credit.** The payload of a data packet is copied out of the sender's buffer by the sender's device thread, after the check against the sender's grant that every device access makes, into a queue entry in endovisor memory; the receiver's device thread copies it from there into a buffer the receiver's driver has posted, re-segmented so that no packet exceeds one receive chain, and raises the receiver's interrupt line. Two copies per packet between sandboxes, one between a sandbox and the host, and at no moment is one sandbox's frame visible to another.

Credit is flow control the tenant's kernel writes, so the switch does not believe it either: it **rewrites the advertised buffer down to a policy limit** in every header it forwards, and answers with a reset a sender whose bytes in flight exceed that limit. Rewriting, rather than simply refusing to drain the sender's transmit queue, is what makes the bound part of the protocol: a sender that honored the larger number it advertised would otherwise put more in flight than the host will hold, and a stalled transmit queue would stall every connection on that device, the runtime's control connection included.

> **A difference between the hosts, and not a host-dependent one.** The two chapters chose different limits for that clamp — 64 KiB with Asterinas as the host, 256 KiB with Linux — and nothing about either host requires the difference. It is drift of exactly the kind this chapter exists to prevent. The limit is policy either way; the two defaults are to be reconciled, and the number recorded here once.

**It forgets.** When a sandbox is marked dying, every connection with it as an endpoint is torn down: the entries are marked dead and the CID with them, in a step that sleeps nowhere and touches no peer's memory, and each peer's device thread delivers a reset into that peer's receive queue, frees the queued bytes and uncharges them. Destroy later asserts that the switch holds no entry naming the sandbox.

## What crosses, and what the host remembers

Nothing but bytes crosses between sandboxes: no frame changes owner, no pointer is carried, and the driver on each side sees a device that delivered or accepted a packet. The switch's table holds CIDs, ports, credits and byte queues, never an address inside a kernelet and never a task's name, so a kernelet's death leaves nothing dangling; and the owner check each copy makes is what keeps one sandbox's descriptor from naming another's frame.

## The ownership-transfer extension

Two copies per packet is the price of sharing no memory. Moving *ownership* of the buffer instead is the obvious alternative, and an early experiment measured it: 104, 177 and 94 cycles to create, send and destroy a reference between instances, and a 4-byte round trip of 9.4 µs against 36.9 µs for virtio-vsock through a microVM (*measured on the booted prototype of the mechanism*).

In this design the same idea is a **frame move**: instead of copying a payload, the switch takes a whole grain out of the sender's grant and appends it to the receiver's, rewriting the owner array and both grant tables, after which the receiver's driver finds the packet in memory it now owns. It needs a payload that fills a grain, a check that the sender no longer maps the frame into any of its processes, and a way to tell the receiver that its grant has grown; and it withdraws, for the sender, the assumption that a kernelet's memory only grows. It is left out of the first version and listed for the Limitations chapter, with the copy path as the baseline it has to beat.

The second version halves the copy path instead: zero-copy I/O copies once, frame to frame, in the sender's notify call, and sets the bar a frame move must clear — about 3 to 4 µs to unmap a grain with the translation shootdown it forces (*estimated* from a 4 KiB measurement) against 156 µs to copy one cold (*measured in a model*).

## What a tenant sees

- `AF_VSOCK` stream sockets, and only streams: datagram and sequenced-packet sockets fail, as they do on the kernel proper's own host build.
- Its own CID, the host at CID 2, and other sandboxes by their CIDs where policy allows the pair. A connection to its own CID or to CID 1 is refused, since the socket layer has no loopback.
- When a peer sandbox dies its connections are reset, which the socket layer surfaces as hang-up and end-of-file rather than as an error.
- Throughput bounded by the credit limit per connection; latency of two copies and two device-thread hops per packet between sandboxes, one of each to the host.

## Costs

- **Per packet**: one or two owner-checked copies of at most 4 KiB, one device-thread wakeup per hop, one virtual interrupt at the receiver, plus the endpoint's copy for a host endpoint; two table lookups and one header rewrite. *Measured in a model*: a 4 KiB copy is 33 ns hot and 457 ns cold and a thread wakeup about 3 µs, so for small messages the wakeups dominate and the copies are noise.
- **Per connection**: one table entry and a bounded queue, charged to the sender.
- **Per sandbox death**: one reset per connection, delivered by each peer's device thread, and the queues freed.

## What this page decides

- **All communication is vsock over the ordinary device path** (register D37). The alternative, a dedicated cross-sandbox channel with service calls of its own, would be a second mechanism to secure and a second thing for tenants to learn; vsock is what their software already speaks.
- **The switch copies in the first version** (register D38). The frame move is an extension with its preconditions stated, because it changes the ownership model and must be measured against the copy path before it is adopted.
- **The switch rewrites advertised credit down to a policy limit and resets a sender that exceeds it** (register D51). The alternative, back-pressure by leaving the sender's transmit queue undrained, stalls every connection on the device.
- **Reachability between sandboxes is denied by default** (register D68); a runtime that holds both sandbox descriptors opens a pair.
- **The host's end of a channel is a stream descriptor from the endovisor, not a second transport for the host's own vsock layer** (register D39). Each host chapter gives its own reason; neither is about what a tenant sees.
