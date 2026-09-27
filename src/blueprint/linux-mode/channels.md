# Channels

*What Linux adds to the common [Channels](../design/channels.md) design: where the copies happen, and why the host's end is a descriptor of the endovisor's own rather than a Linux socket.*

The vsock device, the addressing, the switch's three jobs, the credit clamp and what crosses between sandboxes are in [Channels](../design/channels.md) and are not repeated. This page is the part that is Linux's.

## Where the copies happen

Both copies are made by [device threads](virtualizing-ostd/devices.md), which on this host are `vhost_task` workers of the sandbox's root carrier and therefore members of its control group: the sender's thread copies a payload out of the sender's buffer, after the [grant check](virtualizing-ostd/memory.md), into a queue entry in endovisor memory charged to the sender's control group, and the receiver's thread copies it into a buffer from the receiver's ring, after the same check. The queue entry is allocated with Linux's accounting flag, so the bytes in flight are the sender's memory, bounded by the credit limit.

**The credit limit on this host is 256 KiB** (*chosen*, equal to what the kernel proper's socket layer advertises). The common page records that the Asterinas chapter chose 64 KiB for no host-specific reason and that the two are to be reconciled.

## The host's end

A connection is a stream file descriptor the runtime obtains from the sandbox descriptor with `KERNELET_CONNECT`, to reach a port a tenant process listens on, or `KERNELET_LISTEN`, to accept connections from inside ([The endovisor](endovisor.md#abi)). It is an ordinary descriptor: it can be read, written, polled and passed to another process.

Linux has its own `AF_VSOCK` family for its virtual machines, and the endovisor could register itself as a transport for it, so that host programs would reach a sandbox with plain sockets. It does not, in this design, because Linux allows one host-to-guest transport at a time and an operator who also runs virtual machines with `vhost-vsock` needs that slot. **[unverified]**: whether the two can coexist has not been examined.

## What this page decides

- **The host's end is a descriptor from the endovisor's device, not Linux's `AF_VSOCK`** (register D39, this host's half), for a reason specific to Linux: the single host-to-guest transport slot.
- The common decisions D37, D38, D51 and D68 are on the [Channels](../design/channels.md) page.
