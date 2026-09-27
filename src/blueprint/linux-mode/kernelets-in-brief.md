# Background: kernelets in brief

*Everything about kernelets that the rest of this chapter relies on, for a reader who has read nothing else in the book. It defines the vocabulary; the next page does the same for Linux.*

## The problem kernelets address

A cloud machine runs code from many tenants who do not trust each other. Two ways of separating them dominate. A **virtual machine** gives each tenant a whole kernel on virtual hardware; the boundary is the hardware's and is strong, and the price is a second layer of address translation, exits to a hypervisor, and an entire guest operating system per tenant. A **container** shares one kernel among all tenants; the price is near zero, and the boundary is the correctness of tens of millions of lines of C that every tenant can drive through the full system-call table.

A **kernelet** is a third option: each tenant gets a kernel of its own, as with a virtual machine, but that kernel is an ordinary piece of software loaded into the host kernel, not a guest on virtual hardware. It can be safe to do that because of how the kernel is written.

## A kernel in two layers

[Asterinas](https://github.com/asterinas/asterinas) is a Linux-compatible kernel written in Rust, built as a *framekernel*: it is split into two layers with a hard rule between them.

- **OSTD**, the framework, is small. It is the only part allowed to use Rust's `unsafe`, the escape hatch that permits raw memory access and machine instructions. It wraps the machine (page tables, tasks, interrupts, user-mode entry) in an interface that cannot be misused to corrupt memory.
- **The kernel proper** is everything else: the system calls, file systems, network stack, process and memory management that make the kernel Linux-compatible. It is written entirely in *safe* Rust against OSTD's interface. The compiler guarantees that it cannot forge a pointer, read freed memory or write outside an object. It can have logic bugs; it cannot have memory-safety bugs.

So a kernel proper can do only what OSTD's interface lets it do. That is the opening.

## API virtualization

Give the kernel proper a *different* OSTD: one with the same interface, in which every operation's effect is confined to one tenant's resources, and in which the operations a tenant must never perform (touching hardware, disabling interrupts, naming arbitrary physical memory) do not exist at all. This chapter calls that build **vOSTD**, *virtual OSTD*, named as a virtual CPU is named. The kernel proper, unchanged, compiled against vOSTD, is a **kernelet**.

The book calls the idea **API virtualization**: where a hypervisor virtualizes the hardware beneath a kernel, this virtualizes the programming interface a kernel is written against. The boundary is the language. What a kernelet can name is decided by what its crates export, and what may cross into the host is a fixed table of function calls.

## The vocabulary

| term | meaning |
|---|---|
| **kernelet** | one instance of the Asterinas kernel proper compiled against vOSTD, running in kernel mode inside the host kernel, beside other kernelets |
| **sandbox** | one tenant's environment: a kernelet plus the user-mode processes it serves. What a virtual machine or a container is to its tenant |
| **tenant** | whoever's code runs in a sandbox; assumed hostile |
| **host kernel**, or **host** | the kernel that owns the machine. In this chapter, Linux |
| **endovisor** | the component of the host kernel that creates, serves and destroys kernelets. *Endo-* because it sits inside the host kernel, beside the kernelets, where a hypervisor sits beneath its guests. In this chapter, a Linux kernel module |
| **kernelet runtime** | the host user-space program that configures sandboxes, as a container runtime does |
| **service table** | the fixed set of host functions a kernelet may call; the only way out of a kernelet |
| **grant** | the physical memory a kernelet has been given, in 2 MiB units called **grains** |

## Two hosts, one design

The book's other design chapter, [Design for Asterinas](../asterinas-mode/index.md), specifies kernelets with **Asterinas itself as the host kernel**: the machine boots Asterinas, and kernelets are further instances of the same kernel. This chapter specifies the same kernelets with **Linux as the host**: the operator keeps the kernel they already run, applies one small patch, loads one module, and selected workloads get a kernel of their own.

Nothing in a kernelet says who implements the service table, and that is what makes the second host possible. The kernel proper's source is identical on both. vOSTD is one source with [three host-specific bodies](virtualizing-ostd/index.md). What differs is everything behind the table, and that is what this chapter is about. It is written to be read alone. Where the other chapter treats the same subject it is linked, for comparison and never for a definition.

## What a kernelet needs from any host

The rest of the chapter is organized by these needs, so it helps to have them in one list.

1. **A place to live**: memory for its code and data, with its code shared between instances ([Builds and images](builds-and-images.md)).
2. **Memory to manage**: physical frames for its tenant's processes and its own heap ([Memory](virtualizing-ostd/memory.md)).
3. **Processors to run its tasks on**, which the host shares out among sandboxes while the kernelet decides what runs on its share ([Tasks](virtualizing-ostd/tasks.md), [Scheduling](virtualizing-ostd/scheduling.md)).
4. **A way to run its tenant in user mode** and get control back on every system call and exception ([User mode](virtualizing-ostd/user-mode.md)). This is the hard one on Linux.
5. **Interrupts and time** ([Interrupts and time](virtualizing-ostd/interrupts-and-time.md)).
6. **Devices** and **channels** to the outside ([Devices](virtualizing-ostd/devices.md), [Channels](channels.md)).
7. **To be stopped and cleaned up** when it misbehaves or is no longer wanted, without its cooperation ([Faults, termination, and reclamation](faults-and-reclamation.md)).
