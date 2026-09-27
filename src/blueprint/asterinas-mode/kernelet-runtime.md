# The kernelet runtime

*Answers question 4, user side: how is an OCI-compatible kernelet runtime implemented on the endovisor ABI? What the Asterinas host adds to the common [kernelet runtime](../design/kernelet-runtime.md) design: the order of the ABI calls at `create`, what a bundle's resource limits become here, and why the network is a translator in the runtime's own process.*

The OCI verbs, the agent inside, the holder process, the hooks, the `config.json` mapping, the root file system image and what a tenant sees are in [The kernelet runtime](../design/kernelet-runtime.md) and are not repeated. This page is the part that is this host's.

## The ABI calls at `create`, in order

Every operation the runtime performs on a sandbox is an operation of the [endovisor ABI](endovisor.md): open `/dev/kernelet`; `KERNELET_CREATE`; `KERNELET_ENDPOINT` for the log, the console and the network; `KERNELET_ATTACH` for the block images, the console, the entropy source, vsock, and the network device if the bundle has a network; `KERNELET_VSOCK_LISTEN` on the agent's port; `KERNELET_START`.

The order matters at one point. The kernel boot inside the kernelet begins at `START` ([The endovisor](endovisor.md)), so the runtime attaches every backend and listens on the agent's port *before* it starts the sandbox, and then waits for the agent to dial out ([Channels](channels.md)). `KERNELET_STATUS` is what reports the exit status whose message is the last line of the log.

## What a bundle's processor limits become here

`num_vcpus` and `max_grains` are the common mapping. The processor share is not:

| `config.json` | on this host |
|---|---|
| `linux.resources.cpu.shares` | `nice` by the cgroup convention (`shares` 1024 is `nice` 0, each doubling one step lower, clamped to −20 to 19), which the kernelet applies to every thread; it is a per-thread weight, not a share for the sandbox ([control half](kernelet-api-control.md)) |
| `linux.resources.cpu.quota`, `.period` | `cpu_quota_us` is `quota` when positive and 0, uncapped, when absent or `-1`; `cpu_period_us` as given |
| `linux.resources.cpu.cpus` | the size gives `num_vcpus`; the host picks which CPUs, by the policy the endovisor applies at `START` |

That `shares` becomes a per-thread weight and not a share for the sandbox is the one place where this host delivers less than the other: proportional share between sandboxes needs a group scheduler in the host kernel, which it does not yet have. The property is not held here until one exists ([Scheduling](virtualizing-ostd/tasks.md)).

## Building the image on this host

The host's user space has no `e2fsprogs` of its own (checked: the tree's images take it only as a build-host tool), so the runtime ships a static `mke2fs` and runs it. The sparse file it writes is supported by the host's own ext2 (checked: `fs_impls/ext2/inode/io_range.rs`).

## The network

A kernelet's `virtio-net` device is user-space backed ([Devices](virtualizing-ostd/devices.md), register D25): the runtime holds the network endpoint descriptor and bridges Ethernet frames between it and the host. In the first version the bridge is a user-space NAT in the runtime's own process, in the style of `slirp`: frames from the kernelet are terminated in a user-space TCP/IP stack and forwarded over ordinary host sockets, and replies are framed back. CNI plugins that expect to configure a network namespace and a `veth` pair do not apply, since the host kernel has no such objects (checked on the tree); the runtime instead assigns the address, route and resolver it tells the agent, and offers port mapping and outbound connectivity from `config.json` annotations. **[unverified]** (register A11): that user-space NAT throughput is acceptable for the workloads the design targets; assumption A7's in-kernel backend is the alternative.

What a tenant sees of it: TCP is terminated twice, so the host stack's window and reset behavior show through; ICMP is not forwarded unless the NAT emulates it, so `ping` fails; peers see the host's address; CNI and `podman network` do not apply; only port mapping and outbound connectivity are offered.

## Costs this host adds

Per network frame: two copies and two wakeups on the endpoint, plus the user-space stack.

## What this page decides

- **User-space NAT for the network in the first version** (register D45), following D25; the host has no tap or bridge.
- The common decisions D43, D54 and D44 are on the [kernelet runtime](../design/kernelet-runtime.md) page.
