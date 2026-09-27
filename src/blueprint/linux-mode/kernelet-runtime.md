# The kernelet runtime

*What Linux adds to the common [kernelet runtime](../design/kernelet-runtime.md) design: what it must check about the host before it will create a sandbox, the control group and credentials it prepares, and the process it hands to the endovisor's program loader.*

The OCI verbs, the agent inside, the holder process, the hooks, the `config.json` mapping, the root file system image and what a tenant sees are in [The kernelet runtime](../design/kernelet-runtime.md) and are not repeated. This page is the part that is Linux's: the host administration the runtime does beside the ABI calls, all of it with Linux's own tools.

## `create`, step by step

Each step is either an operation on the [endovisor's device](endovisor.md#abi) or ordinary Linux administration, and the order is what makes the [inheritance](virtualizing-ostd/tasks.md#root) work.

1. **Check the host.** Refuse if `/proc/sys/kernel/panic_on_oops` is set ([why](faults-and-reclamation.md#fault)), if `fs.suid_dumpable` is not 0 ([why](endovisor.md#abi)), if `debug.exception-trace` is on ([why](virtualizing-ostd/user-mode.md#exceptions)), if the running kernel is a real-time one (`PREEMPT_RT`, which the endovisor reports at load), if high-resolution timers are not active (which the endovisor also reports, and without which the [2 ms bound](virtualizing-ostd/scheduling.md#cooperative) is a tick's worth), or if the endovisor's device is missing. Refuse, too, if the bundle's `cpuset` overlaps a processor on which the operator has placed real-time or otherwise latency-sensitive work, since a sandbox may hold any processor it runs on for about 2 ms at a time and sends flushes and kicks to processors in its `cpuset` ([why](virtualizing-ostd/scheduling.md#cooperative)); the runtime knows such work only as the operator declares it, in a list of reserved processors, so the rest is the operator's to arrange.
2. **Parse the bundle**, and reject what cannot be honored.
3. **Make a control group** for the sandbox (cgroup v2) and write the bundle's processor and memory limits into it (`cpu.weight`, `cpu.max`, `cpuset.cpus`, `memory.max`). These are Linux's own controls, the processor ones are the [first level of scheduling](virtualizing-ostd/scheduling.md), and they bound the kernelet itself, not just the tenant's processes: every task of the kernelet and every page of its grant will belong to this group. Three settings are not optional. `pids.max` is set to the number of Linux tasks a sandbox has, which is fixed and small: the root carrier, one carrier per virtual CPU and one thread per device. A tenant can create as many threads as its kernel allows without Linux seeing one more task. `memory.max` is set, and the operator keeps the sum over all sandboxes within the machine's memory, because a grant is kernel memory that Linux's *global* out-of-memory killer does not attribute to the carriers, so global pressure would otherwise be paid by other processes. And `memory.oom.group` is set, so that when the group's own limit is hit Linux ends the whole sandbox and not one carrier.
4. **Open `/dev/kernelet`** and `KERNELET_CREATE` with the kind, the number of virtual CPUs, the memory ceiling, the maximum numbers of tasks and of address spaces (from the bundle, or by default 4,096 tasks and 1,024 address spaces, *chosen*, and never more stacks than a quarter of `memory.max` would hold) and the command line. Ask for the sandbox's **exec descriptor** with `KERNELET_EXEC_FD`.
5. **Open the backends** under the runtime's own credentials (the disk image, a TAP interface for the network) and `KERNELET_ATTACH` each, passing the descriptor. Obtain the log and console endpoints. `KERNELET_LISTEN` on the agent's port.
6. **Fork the process that will become the root carrier.** In the child: move into the control group; enter the namespaces and the user the sandbox should run as, a user identity dedicated to this sandbox, so that no other process on the host may signal or inspect its carriers; leave the address-space limit unlimited (protection against `ptrace` and `/proc` inspection by same-user processes comes from the exec descriptor being [execute-only](endovisor.md#abi), since anything the child set for itself would be reset by the `exec`); set *no new privileges* and install the seccomp filter that closes the [legacy vsyscall page](virtualizing-ostd/user-mode.md); and execute the exec descriptor with `execveat()`. The endovisor's program loader takes over from there, and the child never returns to user space.
7. **Wait for the agent** to connect, and send it the container's configuration. If anything has failed, `create` fails and the sandbox is destroyed, as the specification requires.
8. **Start a holder process** that keeps the sandbox descriptor and the agent connection open, and listens on a Unix socket in the state directory. Closing the last sandbox descriptor destroys the sandbox, so something must hold it between invocations of the runtime.

The seccomp filter is worth writing out, because it is the only thing between a tenant and three Linux system calls:

```c
/* allow everything; trap a call into the legacy vsyscall page */
BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, instruction_pointer) + 4),  /* high word */
BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 0xffffffff, 0, 3),
BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, instruction_pointer)),      /* low word  */
BPF_JUMP(BPF_JMP | BPF_JGE | BPF_K, 0xff600000, 0, 1),
BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRAP),
BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
```

Every ordinary system call of a carrier passes this filter with *allow* and then meets the [gate](virtualizing-ostd/user-mode.md), which hands it to the kernelet. **[unverified]**: the filter is written from the documented layout of seccomp's input and has not been run.

## What the other verbs add here

- **`delete`** destroys the sandbox (`KERNELET_KILL` if it is still running, a wait for it to exit, then `KERNELET_DESTROY`), ends the holder, and removes the control group and the state directory.

One policy belongs to the runtime because only it can enforce it, and it is a policy this host needs and the other does not: **creation is rate-limited per tenant.** Creating an instance changes page permissions, which flushes the TLB of every processor on the machine ([Builds and images](builds-and-images.md)), so a tenant that could make its sandbox restart in a tight loop could tax every other workload. The runtime spaces restarts out, and refuses them beyond a budget. With Asterinas as the host, one physical copy of a kind's text is mapped into each instance's own range and no permission on a mapped frame changes, so nothing machine-wide happens at creation.

## What the operator sees on the host

A sandbox appears as a control group containing the root carrier with its device threads and one process per virtual CPU, all descended from the root carrier, none of which can be traced or signaled usefully one at a time ([why](virtualizing-ostd/tasks.md)). Its memory appears as kernel memory charged to the group. Its log is a stream from the endovisor; its console is another. `cgroup.kill` on the group ends the sandbox, which is the same thing `KERNELET_KILL` does by another route.

## Networking

The first version gives a sandbox a TAP interface, which the runtime creates and connects to whatever the container network expects (a bridge, a veth pair into a network namespace). This is simpler than on the Asterinas host, which has no such machinery and falls back on a user-space translator in the runtime.

## What this page decides

- **The runtime prepares a process and lets it execute the sandbox file** (the runtime's side of register D105, which the [endovisor page](endovisor.md#abi) decides): control group, credentials, namespaces and seccomp filter are set with Linux's own tools and inherited by every carrier.
- **The network backend is a TAP interface** (register D110), replacing the other host's user-space translator.
- **Creation is rate-limited per tenant**, because on this host creating an instance is a machine-wide event.
- The common decisions D43, D54 and D44 are on the [kernelet runtime](../design/kernelet-runtime.md) page.
