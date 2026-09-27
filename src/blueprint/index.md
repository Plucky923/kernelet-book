# The Blueprint

This volume is the design document: the specification of Asterinas Kernelets, written to be precise enough that a coding agent can implement from it. Where The Paper keeps only what is novel and what is evidenced, the Blueprint keeps everything a builder needs: every mechanism down to the types, the functions it changes and the checks the build runs, and every claim with its cost, its provenance, and whether it is proven, checked or argued. Nothing in it has run, and the text says so wherever it matters.

- [Overview](overview/index.md): the goals the boundary must meet, the idea in one page, the terminology, and the challenges.
- [Design](design/index.md): the design that does not depend on which kernel is the host — what a kernelet is, and what any host must provide it.
- [Design for Asterinas](asterinas-mode/index.md): the mechanisms, one per resource, each with the obligation it discharges and its cost, with Asterinas itself as the host kernel.
- [Design for Linux](linux-mode/index.md): the same kernelets over Linux instead of Asterinas, what it takes, and what it proves about API virtualization.

> **To be written.** After the design chapters: the implementation plan, the evaluation plan and the limitations. Their pages each end with what they decide, and the [design register](../notes/design-register.md) lists every decision and assumption; the Limitations chapter will collect the assumptions marked **[unverified]** there and the extensions the design chapters defer (the frame move of [Channels](asterinas-mode/channels.md), the tickless kernelet of [Interrupts and time](asterinas-mode/virtualizing-ostd/interrupts-and-time.md), the `virtiofs` backend of [The kernelet runtime](asterinas-mode/kernelet-runtime.md)).
