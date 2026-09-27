# The kernelet runtime

*The program an operator actually runs: a container runtime that gives each container a kernelet. It is ordinary host user space, trusted as a container runtime is trusted, and everything it does to a sandbox it does through the endovisor ABI and a small agent inside. This page is the part that is the same on both hosts; each host chapter gives the operations its endovisor offers and the host administration the runtime does beside them.*

## What it is

The **kernelet runtime** implements the Open Container Initiative's runtime interface, the five verbs `create`, `start`, `kill`, `delete` and `state` over a *bundle* (a directory with a `config.json` and a root file system). That is the interface `runc` implements, so containerd, Podman or Kubernetes can use kernelets by naming a different runtime, with nothing else changed. Its model is the one Kata Containers uses for virtual machines: the sandbox boots at `create`, and the container's process is something an agent inside the sandbox starts later.

No tenant sees the runtime or trusts it. The host trusts it to configure sandboxes correctly, as it trusts `runc`. Every operation it performs on a sandbox is an operation of the endovisor ABI; everything it does inside the sandbox it does through the agent, over a [vsock connection](channels.md).

## What runs inside a sandbox

The kernel proper in its kernelet configuration, with a command line the runtime composes: the virtio device list, `root=/dev/vda rootfstype=ext2`, `rw` unless the bundle asks for a read-only root (the kernel proper mounts the root read-only by default; checked on the tree: `kernel/core/src/fs/rootfs.rs`), and `init=/sbin/kernelet-agent`.

The **agent** is a small static program the runtime places in the root file system. As the sandbox's first process it mounts what `config.json` asks for, sets the hostname, configures `eth0`'s address, route and resolver from what the runtime tells it (through rtnetlink, which the kernel proper has; checked: `RTM_NEWADDR`), sets up the process's environment, working directory, user, capabilities and resource limits from the `process` object, and then takes orders from the runtime: start the container's process, start an `exec` process, deliver a signal, report an exit status, carry each process's standard streams. It is the same role Kata's agent plays; it is small because a real kernel is doing the rest.

## The five verbs {#verbs}

| OCI operation | what the runtime does |
|---|---|
| `create <id> --bundle <dir>` | Checks that the host is configured so that a sandbox can run at all, and refuses with a message naming the setting when it is not; each host chapter lists its own checks. Parses `config.json` and rejects what it cannot honor (below). Builds or finds the root file system image. Runs the `createRuntime` hooks. Opens `/dev/kernelet` and `KERNELET_CREATE`s the sandbox with the kind, the number of virtual CPUs, the memory ceiling and the command line; obtains the log and console endpoints; opens each backend under its own credentials and `KERNELET_ATTACH`es it; listens on the agent's port; and starts the sandbox by whatever means its host offers. Waits for the agent to connect and sends it the environment: mounts, hostname, network, the `process` object without starting it. A failure anywhere fails `create`, as the specification requires. Records the bundle path and starts the holder process. The container is `created`. |
| `start <id>` | Tells the agent to start the container's process; runs the `poststart` hooks. |
| `kill <id> <signal>` | In `created`, destroys the sandbox and the container is `stopped`, as `runc` does. In `running`, sends the signal to the agent, which delivers it to the container's process. Killing the *sandbox* is `KERNELET_KILL`, used when the agent does not answer within a timeout. |
| `delete <id>` | Fails unless the container is `stopped` (or `--force`, which kills first). `KERNELET_DESTROY`, ends the holder, removes the state directory and the uncached image, runs the `poststop` hooks. |
| `state <id>` | The state JSON from the holder: `status` from the kernelet's state and the agent's report of the container's process; `pid` is the holder's, because the container's process has no identifier the host could use — on the host it is not a host process at all, and inside the sandbox it has one only the kernelet knows; `bundle` as recorded at `create`. |
| `exec` (a containerd extension) | A second process started through the agent, with its own streams over the agent connection. |

> **Names that drift.** The two host chapters spell one of these operations differently — `KERNELET_VSOCK_LISTEN` with Asterinas as the host, `KERNELET_LISTEN` with Linux — for the same act of listening on the agent's port. Nothing about either host requires the difference, and the ABI is to use one name.

## The holder process {#holder}

Every invocation after `create` reaches the sandbox through a **holder process**, one per sandbox, over a Unix socket in the state directory. The holder owns two things no later invocation could otherwise reach: the sandbox descriptor, since closing the last descriptor destroys the sandbox, and the vsock connection to the agent. `delete` is what ends the holder.

**Hooks.** The specification's `createRuntime`, `poststart` and `poststop` hooks run on the host as it says; `prestart` is treated as `createRuntime`, as its deprecation note allows. `createContainer` and `startContainer`, which run inside the container's namespaces, have no host to run on, since the sandbox's inside is a separate kernel; a bundle that specifies them is rejected at `create` with an error naming the hook, not silently.

## The agent connection {#agent}

The runtime listens before it starts the sandbox and the agent dials out when it is ready, so that the connection never races the boot: a request to a kernelet whose driver is not yet up would be held by the switch and answered with a reset by a kernel with no listener ([Channels](channels.md)). The connection carries a length-prefixed message protocol with one stream identifier per process and per standard stream, so that `stdout` and `stderr` are separate, `stdin` can be closed on its own, and `terminal=false` processes work; with `terminal=true` the agent allocates a pseudo-terminal inside and the runtime forwards it to the pseudo-terminal it passes back over `--console-socket`, as the specification's CLI contract requires. The kernelet's own kernel output goes to the log endpoint, not to the container's standard output.

## `config.json` to a sandbox {#config}

| `config.json` | what becomes of it |
|---|---|
| `linux.resources.cpu.cpus` | `num_vcpus` from the cpuset's size when present, otherwise the runtime's configured default, 2 (*chosen*); which processors a sandbox may run on is the host's to arrange |
| `linux.resources.cpu.shares`, `.quota`, `.period` | the sandbox's processor share and cap, in whichever of the host's own controls carries them. This is the first level of scheduling, and what property it delivers is each host chapter's to state |
| the bundle's task ceiling | `max_tasks`, or the runtime's default of 4,096 (*chosen*). How many address spaces a sandbox may hold is each host chapter's, because what one costs the host differs |
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

## The root file system {#rootfs}

The specification gives the runtime a directory. A kernelet of the first version has no shared file system with the host, only block devices, so the runtime turns the directory into an **ext2 image**, the one root file system type the kernel proper supports (checked on the tree: `rootfs.rs`, `SUPPORTED_ROOTFS_TYPES`): a sparse file, written by `mke2fs -d`. The cost is proportional to the root file system's size and is paid at `create`; the runtime caches images by content, keyed on the shim path by containerd's snapshot key and on the CLI path by a hash of the directory, which costs a read of it, so that the second sandbox from the same image pays nothing for the build. A cached image is attached read-only, and the agent mounts an overlay with a `tmpfs` upper over it (the kernel proper has `overlayfs`; checked: `fs_impls/overlayfs`), so that two sandboxes never write one image; the upper's memory is the sandbox's.

This is the largest practical gap between a kernelet and a container in the first version and is stated as such: a shared file system removes the image build and is the planned replacement. The kernel proper already has the whole client side, a mountable `virtiofs` over FUSE (checked on the tree: `fs_impls/virtiofs`, `comps/virtio/src/device/filesystem`); what is deferred is only the server backend in the endovisor, serving a host directory through the host's own file API. Until then, `mounts` that bind host directories are supported only as read-only images.

## The console and the log {#console}

The console endpoint is the virtio console's host end, which the runtime keeps for a tenant that opens `hvc0` directly; the container's streams travel over the agent connection as above. The log endpoint carries the kernelet's kernel log, its early-console output and its oops reports to a file in the state directory, which is where an operator looks when a sandbox dies with a panic; the message in the exit status the endovisor reports is the last line of it.

## Driving it from containerd

The runtime ships as an OCI runtime binary, `kernelet-runtime`, usable with `runc`-style invocation, and as a containerd shim v2, `containerd-shim-kernelet-v2`, which is the same code behind the shim's ttrpc API with one shim process per sandbox as the holder. Both are user-space programs and may be written in any language; the design's only requirement is the ABI they speak.

## What a tenant sees

- A Linux machine: its own kernel, `/dev/vda` as its root, `eth0` with the address the runtime assigned, a console, `nproc` virtual CPUs, and a memory total that is the initial grant, growing as grants arrive. `uname -r` reports the kernelet's kernel.
- Bind mounts from the host are files copied at creation or read-only images; a change on the host after `create` is not seen.
- What its network looks like from outside, and whether the container network's own tooling applies to it, is the host's; each chapter says.

## Costs

- `create`: the image build, proportional to the root file system and cached by content, or a read of the directory to key the cache; host disk for each uncached image; the sandbox's creation and its kernel's boot, which the Evaluation chapter measures against a microVM's boot; a handful of `ioctl`s; the agent handshake.
- Per sandbox while it runs: the holder process; the image's host page cache, which is host memory not charged to the kernelet, an exception to the charged-work invariant of the same kind as the host's physical interrupt work; the overlay's `tmpfs` upper, inside the sandbox; the log file.
- `start`: one message to the agent.
- `delete`: destroy, and the image's removal unless cached.

## What this page decides

- **A guest agent as the kernelet's init, over vsock, dialing out to a port the runtime listens on** (register D43). The alternative, having the runtime drive the container's process without an agent, has no way to start a process inside a kernel it cannot enter; every VM-based runtime has an agent for this reason. Connecting inward from the runtime races the boot.
- **The sandbox boots at OCI `create`, and `start` only starts the process** (register D54). The alternative, booting at `start`, cannot fail `create` on a bad environment, cannot `kill` a `created` container, and hides the boot inside `start`, all against the specification.
- **The root file system is an ext2 image in the first version, cached read-only under an overlay, with a `virtiofs` server backend as the planned replacement** (register D44). The alternative of shipping that backend in the first version puts the largest device model on the critical path of the first working sandbox; the client side already exists.

Four things are each host chapter's: the network backend, the host's own resource controls, what the runtime must check about the host before it will create a sandbox at all, and whether it must rate-limit creation — which depends on what creating an instance costs the rest of the machine on that host.
