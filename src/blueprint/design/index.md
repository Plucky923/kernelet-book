# Design

*The design of kernelets that does not depend on which kernel is the host: what a kernelet is, what it may assume, and what any host must provide it. The two chapters after this one say how [Asterinas](../asterinas-mode/index.md) and [Linux](../linux-mode/index.md) provide it.*

> **Being written.** This chapter is being assembled from the two host chapters, which said the same things twice, page by page ([the plan](https://github.com/tatetian/kernelet-book/blob/host-alignment/ALIGNMENT_PLAN.md)). Until a page appears here, the host chapters still carry its subject in full. The list at the foot of this page is what has moved so far.

## Why there is a common chapter

Nothing in a kernelet says who implements the service table. The kernel proper's source is the same whichever kernel is the host; vOSTD is one source with a handful of host-specific bodies; the image ABI, the grant, the invariants and the life cycle are the same on both. Only what stands behind the table differs.

Written as two chapters, that shared material was written twice — and the two copies drifted, which is how one host came to have a mechanism the other lacked for no reason but the order in which they were revised. This chapter is the single place the common design lives, so that a difference between the hosts is a deliberate one.

## The rule this chapter is held to

> **Does a sentence name a facility of a particular host** — a Linux `mm_struct`, an Asterinas `Thread`, a control group, a hook? Then it belongs to that host's chapter. If it does not, it belongs here.

And the converse, which keeps this chapter from becoming a table of contents: **it states the contract and the kernelet's own side of it.** Where a host must provide something, this chapter says *what*, and with which obligations; the host chapters say *how*. It never says "the host does this somehow".

## What a kernelet needs from any host

The design is organized by these needs.

1. **A place to live**: memory for its code and data, with its code shared between instances.
2. **Memory to manage**: physical frames for its tenant's processes and its own heap.
3. **Processors to run its tasks on**, which the host shares out among sandboxes while the kernelet decides what runs on its share.
4. **A way to run its tenant in user mode**, and to get control back on every system call and exception.
5. **Interrupts and time.**
6. **Devices**, and **channels** to the outside.
7. **To be stopped and cleaned up** when it misbehaves or is no longer wanted, without its cooperation.

## In this chapter

- [Channels](channels.md): vsock through a switch, addressing, credit, and what crosses between sandboxes.
- [The kernelet runtime](kernelet-runtime.md): the OCI verbs, the agent inside a sandbox, and what a bundle becomes.
