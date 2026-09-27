# Channels

*What the Asterinas host adds to the common [Channels](../design/channels.md) design: where the switch lives, how the two copies are made safe, and how host user space gets an end of a channel.*

The vsock device, the addressing, the switch's three jobs, the credit clamp and what crosses between sandboxes are in [Channels](../design/channels.md) and are not repeated. This page is the part that is this host's.

## Where the copies happen, and what checks them

Both of the switch's copies run under [`guest_memory`](kernelet-api-control.md), the control half's checked accessor, on the respective kernelet and with the grains pinned: the sender's device thread copies a payload out of the sender's transmit buffer into a host queue entry and completes the transmit descriptor; the switch wakes the receiver's device thread, which pops one of the receive chains the receiver's driver has posted, copies into it, writes the used ring and calls `Kernelet::raise_irq`. The accessor's owner check on each copy is what discharges invariant I2 here, and the bytes queued in between are charged with `charge_host_bytes` to the sending kernelet — or, on a host-endpoint connection, to the kernelet at its other end in both directions.

The switch itself is `vsock_switch.rs` in the endovisor ([The endovisor](endovisor.md)), one per host, and its table is one of the host-wide tables that name a kernelet: `destroy` asserts it holds no entry for the identifier ([Faults, termination, and reclamation](faults-and-reclamation.md)). The teardown at `on_dying` sleeps nowhere and touches no peer's memory, which is what lets it run on the reaper.

**The credit cap on this host is 64 KiB** (*chosen*), rewritten into `buf_alloc` in every forwarded header. The common page records that the Linux chapter chose 256 KiB for no host-specific reason and that the two are to be reconciled.

## The host endpoint

A connection to CID 2 has the switch itself as the peer, so the switch originates that side's credit: `buf_alloc` is the endpoint's bound, `fwd_cnt` its read counter, a credit request is answered at once, and a credit update is sent whenever a read frees a quarter of the bound, which is what keeps the kernel proper's sender from blocking.

The connection surfaces in host user space as an endpoint descriptor from the [endovisor ABI](endovisor.md). `KERNELET_VSOCK_LISTEN` on a sandbox descriptor listens on a host port scoped to that sandbox, not on a host-wide port space, for one connection from the kernelet; `KERNELET_VSOCK_CONNECT` sends a request to the kernelet's port, which the switch holds until the kernelet's receive queue is ready and which the tenant's kernel answers with a reset if nothing listens, under the caller's own timeout. The runtime therefore listens and lets its agent dial out ([The kernelet runtime](../design/kernelet-runtime.md#agent)).

The descriptor's stream semantics: a peer shutdown with the send flag is end-of-file on read; a peer reset is `ECONNRESET` on the next read or write; closing the descriptor sends a shutdown with both flags and, if the peer has not reset within the socket layer's eight-second close timeout, a reset; `KERNELET_VSOCK_SHUTDOWN` sends a send-side shutdown for a half-close.

## Why not the host's own vsock layer

The host kernel has an `AF_VSOCK` layer, but it is a *guest's*: bound at initialization to the single virtio-vsock device if one exists, with no transport abstraction, and every socket operation fails with `ENODEV` without one (*measured on the tree*). If the host itself runs under a hypervisor, that layer talks to the hypervisor, and a kernelet's CID 2 is the endovisor rather than the machine's host. The endovisor does not add a second transport to it; it hands out endpoint descriptors backed by the switch, which is what the runtime needs and no more.

## What this page decides

- **Host-side vsock endpoints are stream descriptors from the endovisor ABI, not a second transport for the host's own `AF_VSOCK` layer** (register D39, this host's half): teaching a guest-side socket layer to route to a switch as well as to a device is more surface than the runtime needs.
- The common decisions D37, D38, D51 and D68 are on the [Channels](../design/channels.md) page.
