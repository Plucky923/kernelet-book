# The kernelet runtime

*Answers question 4, user side: how is an OCI-compatible kernelet runtime implemented on the endovisor ABI?*

The kernelet runtime is a program in host user space that implements the OCI runtime specification's lifecycle, `create`, `start`, `kill`, `delete` and `state`, over a bundle with a `config.json` and a root file system, so that containerd or podman can drive it as they drive `runc` or `kata-runtime`. Every operation it performs on a sandbox is an operation of the [endovisor ABI](endovisor.md); everything it does inside the sandbox it does through a small **agent**, the kernelet's init process, over a vsock connection. It is trusted by the host as a container runtime is, and by no tenant. Its model is Kata's: the sandbox boots at `create`, and the container process is one thing the agent starts inside it.

## What runs inside

A kernelet runs the kernel proper in its kernelet configuration ([Builds and images](builds-and-images.md)) with a command line the runtime composes: the virtio MMIO device list of [Devices](virtualizing-ostd/devices.md), `root=/dev/vda rootfstype=ext2`, `rw` unless the bundle asks for a read-only root (the kernel proper mounts the root read-only by default; checked on the tree: `kernel/core/src/fs/rootfs.rs`), and `init=/sbin/kernelet-agent`. The agent is a static binary the runtime places in the root file system. It mounts what `config.json` asks for, sets the hostname, configures `eth0`'s address, route and resolver from what the runtime tells it (through rtnetlink, which the kernel proper has; checked: `RTM_NEWADDR`), sets up the process's environment, working directory, user, capabilities and resource limits from the `process` object, and then does what the runtime tells it over vsock: start the container process, start an `exec` process, deliver a signal, report an exit status, and carry each process's standard streams. It is the same role Kata's agent plays; it is small because the kernel does the rest.

## Lifecycle

| OCI operation | what the runtime does |
|---|---|
| `create <id> --bundle <dir>` | Parses `config.json` and rejects what it cannot honor (below). Builds or finds the root file system image. Runs the `createRuntime` hooks. Opens `/dev/kernelet`; `KERNELET_CREATE`; `KERNELET_ENDPOINT` for the log, the console and the network; `KERNELET_ATTACH` for the block images, the console, the entropy source, vsock, and the network device if the bundle has a network; `KERNELET_VSOCK_LISTEN` on the agent's port; `KERNELET_START`. Waits for the agent to connect and sends it the environment: mounts, hostname, network, the `process` object without starting it. A failure anywhere fails `create`, as the specification requires. Records the bundle path and starts the holder process. The container is `created`. |
| `start <id>` | Tells the agent to start the container process; runs the `poststart` hooks. |
| `kill <id> <signal>` | In `created`, destroys the kernelet and the container is `stopped`, as `runc` does. In `running`, sends the signal to the agent, which delivers it to the container process. Killing the *sandbox* is `KERNELET_KILL`, used when the agent does not answer within a timeout. |
| `delete <id>` | Fails unless the container is `stopped` (or `--force`, which kills first). `KERNELET_DESTROY`, removes the state directory and the uncached image, runs the `poststop` hooks. |
| `state <id>` | The state JSON from the holder: `status` from the kernelet's state and the agent's report of the container process; `pid` is the holder's pid, since the container process has no host pid; `bundle` as recorded at `create`. |
| `exec` (a containerd extension) | A second process started through the agent, with its own streams over the agent connection. |

Every CLI invocation after `create` talks to a **holder process** per sandbox through a Unix socket in the state directory: the holder owns the sandbox descriptor, since closing the last descriptor destroys the kernelet ([endovisor](endovisor.md), register D42), and the vsock connection to the agent, which no later invocation could otherwise reach. `delete` is what ends the holder.

**Hooks.** The specification's `createRuntime`, `poststart` and `poststop` hooks run on the host as it says; `prestart` is treated as `createRuntime`, as its deprecation note allows. `createContainer` and `startContainer`, which run inside the container's namespaces, have no host to run on, since the sandbox's inside is a separate kernel; a bundle that specifies them is rejected at `create` with an error naming the hook, not silently.

**The agent connection.** The runtime listens before `START` and the agent dials out when it is ready, so that the connection never races the boot: a `Request` to a kernelet whose driver is not up would be held by the switch and answered with `Rst` by a kernel with no listener ([Channels](channels.md)). The connection carries a length-prefixed message protocol with one stream identifier per process and per standard stream, so that `stdout` and `stderr` are separate, `stdin` can be closed on its own, and `terminal=false` processes work; with `terminal=true` the agent allocates a pseudo-terminal inside and the runtime forwards it to the pseudo-terminal it passes back over `--console-socket`, as the specification's CLI contract requires. The kernelet's own kernel output goes to the log endpoint, not to the container's standard output.

## `config.json` to a kernelet

| `config.json` | kernelet configuration |
|---|---|
| `linux.resources.cpu.cpus` | `num_vcpus` from the cpuset's size when present, otherwise the runtime's configured default, 2 (chosen); the host picks which CPUs |
| `linux.resources.cpu.shares`, `.quota`, `.period` | `nice` from `shares` by the cgroup convention (`shares` 1024 is `nice` 0, each doubling one step lower, clamped to −20 to 19), which the kernelet applies to every thread; it is a per-thread weight, not a share for the sandbox ([control half](kernelet-api-control.md)); `cpu_quota_us` is `quota` when positive and 0, uncapped, when absent or `-1`; `cpu_period_us` as given |
| `linux.resources.memory.limit` | `max_grains = limit / 2 MiB` when present; the user's policy cap when absent or `-1`; `initial_grains` is the runtime's policy, by default a quarter of `max_grains` or 16 grains, whichever is larger (*estimated*; to be tuned against the floor the Evaluation chapter measures) |
| `process` | sent to the agent at `create` and started at `start`: args, env, cwd, user, capabilities, rlimits, terminal |
| `root.path`, `root.readonly` | the block image below, attached read-only if asked |
| `mounts` | a bind mount of a *file* is sent to the agent at `create`, which writes it into the overlay's upper layer inside, so that it never enters the shared, content-keyed image and a secret mounted into one pod cannot surface in another's; it is stale afterward, which is stated; a read-only bind of a directory is a second image; a read-write bind of a directory, `rbind` and mount propagation are rejected at `create`; the standard pseudo-file-systems are mounted by the agent inside |
| `hostname` | set by the agent |
| `linux.namespaces` | ignored: the sandbox is the namespace |
| `linux.uidMappings`, `gidMappings` | rejected: there is no user namespace to map into |
| `linux.seccomp`, `linux.devices`, cgroup paths | ignored in the first version; a kernelet's isolation does not rest on them |
| `hooks` | as above |

Bind-mounted files matter because containerd's CRI mounts `/etc/hosts`, `/etc/hostname`, `/etc/resolv.conf` and a termination log into every pod, plus secret and config directories; the copy rule is what lets a Kubernetes pod start at all in the first version, and the staleness is its price; copying through the agent rather than into the image is what keeps the image cache shareable and keeps one pod's secrets out of another's root.

## The root file system

The specification gives the runtime a directory. A kernelet of the first version has no shared file system with the host, only block devices, so the runtime turns the directory into an **ext2 image**, the one root file system type the kernel proper supports (checked on the tree: `rootfs.rs`, `SUPPORTED_ROOTFS_TYPES`): a sparse file, which the host's ext2 supports (checked: `fs_impls/ext2/inode/io_range.rs`), written by a static `mke2fs -d` the runtime ships, since the host's user space has no `e2fsprogs` of its own (checked: the tree's images take it only as a build-host tool). The cost is proportional to the root file system's size and is paid at `create`; the runtime caches images by content, keyed on the shim path by containerd's snapshot key and on the CLI path by a hash of the directory, which costs a read of it, so that the second sandbox from the same image pays nothing for the build. A cached image is attached read-only, and the agent mounts an overlay with a `tmpfs` upper over it (the kernel proper has `overlayfs`; checked: `fs_impls/overlayfs`), so that two sandboxes never write one image; the upper's memory is the sandbox's. This is the largest practical gap between a kernelet and a container in the first version and is stated as such: a shared file system removes the image build and is the planned replacement. The kernel proper already has the whole client side, a mountable `virtiofs` over FUSE (checked on the tree: `fs_impls/virtiofs`, `comps/virtio/src/device/filesystem`); what is deferred is only the server backend in the endovisor, serving a host directory through the host's own file API. Until then, `mounts` that bind host directories are supported only as read-only images.

## The network

A kernelet's `virtio-net` device is user-space backed ([Devices](virtualizing-ostd/devices.md), register D25): the runtime holds the network endpoint descriptor and bridges Ethernet frames between it and the host. In the first version the bridge is a user-space NAT in the runtime's own process, in the style of `slirp`: frames from the kernelet are terminated in a user-space TCP/IP stack and forwarded over ordinary host sockets, and replies are framed back. CNI plugins that expect to configure a network namespace and a `veth` pair do not apply, since the host kernel has no such objects (checked on the tree); the runtime instead assigns the address, route and resolver it tells the agent, and offers port mapping and outbound connectivity from `config.json` annotations. **[unverified]** (register A11): that user-space NAT throughput is acceptable for the workloads the design targets; assumption A7's in-kernel backend is the alternative.

## The console and the log

The console endpoint is the virtio console's host end, which the runtime keeps for a tenant that opens `hvc0` directly; the container process's streams travel over the agent connection as above. The log endpoint carries the kernelet's kernel log, its early-console output and its oops reports to a file in the state directory, which is where an operator looks when a sandbox dies with a panic; the exit status's message from `KERNELET_STATUS` is the last line of it.

## Driving it from containerd

The runtime ships as an OCI runtime binary, `kernelet-runtime`, usable with `runc`-style invocation, and as a containerd shim v2, `containerd-shim-kernelet-v2`, which is the same code behind the shim's ttrpc API with one shim process per sandbox as the holder. Both are user-space programs and may be written in any language; the design's only requirement is the ABI they speak.

## What a tenant sees

- A Linux machine: its own kernel, `/dev/vda` as its root, `eth0` with the address the runtime assigned, a console, `nproc` virtual CPUs, and a memory total that is the initial grant, growing as grants arrive ([The rest](virtualizing-ostd/the-rest.md)). `uname -r` reports the kernelet's kernel.
- Bind mounts from the host are files copied at creation or read-only images; a change on the host after `create` is not seen.
- The network is behind a user-space NAT: TCP is terminated twice, so the host stack's window and reset behavior show through; ICMP is not forwarded unless the NAT emulates it, so `ping` fails; peers see the host's address; CNI and `podman network` do not apply; only port mapping and outbound connectivity are offered.

## Costs

- `create`: the image build, proportional to the root file system and cached by content, or a read of the directory to key the cache; host disk for each uncached image; the kernelet's creation and its kernel's boot, which the Evaluation chapter measures against a microVM's boot; a handful of `ioctl`s; the agent handshake.
- Per sandbox while it runs: the holder process; the image's host page cache, which is host memory not charged to the kernelet, an exception to invariant I6 of the same kind as the host's physical interrupt work; the overlay's `tmpfs` upper, inside the sandbox; the log file.
- `start`: one message to the agent.
- Per network frame: two copies and two wakeups on the endpoint, plus the user-space stack.
- `delete`: destroy, and the image's removal unless cached.

## What this page decides

- **A guest agent as init, over vsock, dialing out to a port the runtime listens on** (register D43). The alternative, having the runtime drive the container process without an agent, has no way to start a process inside a kernel it cannot enter; every VM-based runtime has an agent for this reason. Connecting inward from the runtime races the boot.
- **The sandbox boots at OCI `create`, and `start` only starts the process** (register D54). The alternative, booting at `start`, cannot fail `create` on a bad environment, cannot `kill` a `created` container, and hides the boot inside `start`, all against the specification.
- **The root file system is an ext2 image in the first version, cached read-only under an overlay, with a `virtiofs` server backend as the planned replacement** (register D44). The alternative of shipping that backend in the first version puts the largest device model on the critical path of the first working sandbox; the client side already exists.
- **User-space NAT for the network in the first version** (register D45), following D25; the host has no tap or bridge.
