# Design for Asterinas

<figure class="fwd-fig">
<div class="head">
<div class="tag">The kernelet architecture</div>
<div class="title">One kernel source, two builds, one boundary at OSTD's API</div>
</div>
<svg viewBox="0 0 900 400" role="img" aria-label="The kernelet architecture. Left, the host: Linux apps and the kernelet runtime in user space; the host kernel with its Linux functionality and the endovisor; OSTD exposing the vanilla OSTD API and the kernelet API, whose control half the endovisor uses and whose service half vOSTD calls through the service table. Right, a kernelet: Linux apps in user mode over the kernel proper over vOSTD, the virtualized OSTD API, over its own image and the frames it has been granted. The host enters the kernelet only through the entry table, to start a carrier or to redirect one; devices are virtio over function calls between the endovisor and the kernelet.">
<defs>
<linearGradient id="kad-cg" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="#00F7FF" stop-opacity=".22"/>
<stop offset="100%" stop-color="#1937FF" stop-opacity=".22"/>
</linearGradient>
<marker id="kad-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#9AA0BE"/>
</marker>
<marker id="kad-arrow-cyan" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#00F7FF"/>
</marker>
</defs>
<g font-family="ui-monospace,monospace" font-size="10.5">
<text x="30" y="22" fill="#9A9DB0" font-size="9" letter-spacing="1.6">HOST</text>
<text x="880" y="22" fill="#00F7FF" font-size="9" letter-spacing="1.6" text-anchor="end">KERNELET (ONE OF MANY)</text>
<rect x="30" y="34" width="140" height="36" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="100" y="56" fill="#9AA0BE" text-anchor="middle">Linux apps</text>
<rect x="190" y="34" width="150" height="36" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="265" y="56" fill="#9AA0BE" text-anchor="middle">kernelet runtime</text>
<rect x="520" y="34" width="360" height="36" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="700" y="56" fill="#9AA0BE" text-anchor="middle">Linux apps</text>
<path d="M20 90 H880" stroke="rgba(255,255,255,.28)" stroke-width="1" stroke-dasharray="5 4"/>
<text x="22" y="86" fill="#6A6F8C" font-size="8">user mode</text>
<text x="22" y="101" fill="#6A6F8C" font-size="8">kernel mode</text>
<rect x="20" y="108" width="330" height="82" rx="10" fill="rgba(255,255,255,.025)" stroke="rgba(255,255,255,.12)"/>
<text x="30" y="123" fill="#9A9DB0" font-size="9" letter-spacing="1.4">HOST KERNEL</text>
<rect x="30" y="132" width="140" height="48" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="100" y="152" fill="#9AA0BE" text-anchor="middle">Linux functionality</text>
<text x="100" y="168" fill="#6A6F8C" text-anchor="middle" font-size="8">kernel proper</text>
<rect x="190" y="132" width="150" height="48" rx="6" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="265" y="152" fill="#00F7FF" text-anchor="middle">endovisor</text>
<text x="265" y="168" fill="#5C93A8" text-anchor="middle" font-size="7.5">device models · vsock · policy</text>
<rect x="510" y="108" width="370" height="82" rx="10" fill="rgba(0,247,255,.04)" stroke="rgba(0,247,255,.42)"/>
<text x="520" y="123" fill="#00F7FF" font-size="9" letter-spacing="1.4">KERNELET</text>
<rect x="520" y="132" width="350" height="48" rx="6" fill="url(#kad-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="695" y="152" fill="#8FF6FC" text-anchor="middle">Linux functionality</text>
<text x="695" y="168" fill="#5C93A8" text-anchor="middle" font-size="8">the same kernel proper · forbid(unsafe_code)</text>
<rect x="20" y="204" width="330" height="102" rx="10" fill="rgba(255,255,255,.025)" stroke="rgba(255,255,255,.12)"/>
<text x="30" y="219" fill="#9A9DB0" font-size="9" letter-spacing="1.4">OSTD</text>
<rect x="30" y="228" width="140" height="68" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="100" y="258" fill="#9AA0BE" text-anchor="middle">vanilla</text>
<text x="100" y="274" fill="#9AA0BE" text-anchor="middle">OSTD API</text>
<rect x="190" y="228" width="150" height="68" rx="6" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="265" y="243" fill="#00F7FF" text-anchor="middle">kernelet API</text>
<rect x="198" y="250" width="134" height="18" rx="4" fill="rgba(6,10,36,.55)" stroke="rgba(0,247,255,.35)"/>
<text x="265" y="263" fill="#8FF6FC" text-anchor="middle" font-size="9">control half</text>
<rect x="198" y="272" width="134" height="18" rx="4" fill="rgba(6,10,36,.55)" stroke="rgba(0,247,255,.35)"/>
<text x="265" y="285" fill="#8FF6FC" text-anchor="middle" font-size="9">service half</text>
<rect x="510" y="204" width="370" height="102" rx="10" fill="rgba(0,247,255,.04)" stroke="rgba(0,247,255,.42)"/>
<text x="520" y="219" fill="#00F7FF" font-size="9" letter-spacing="1.4">vOSTD</text>
<rect x="520" y="228" width="350" height="68" rx="6" fill="url(#kad-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="695" y="252" fill="#8FF6FC" text-anchor="middle">virtualized OSTD API</text>
<text x="695" y="269" fill="#5C93A8" text-anchor="middle" font-size="8">identical · virtualized · absent, item by item</text>
<text x="695" y="284" fill="#5C93A8" text-anchor="middle" font-size="8">OSTD's source under the kernelet feature</text>
<rect x="20" y="322" width="330" height="30" rx="5" fill="rgba(255,255,255,.03)" stroke="rgba(255,255,255,.12)"/>
<text x="185" y="341" fill="#6A6F8C" text-anchor="middle" font-size="9.5">hardware</text>
<rect x="510" y="322" width="370" height="30" rx="5" fill="rgba(0,247,255,.05)" stroke="rgba(0,247,255,.3)"/>
<text x="695" y="341" fill="#5C93A8" text-anchor="middle" font-size="8.5">kernelet image: text · data · shared pages, plus granted frames</text>
<path d="M100 70 V132" stroke="#9AA0BE" stroke-width="1.2" marker-end="url(#kad-arrow)"/>
<path d="M265 70 V132" stroke="#9AA0BE" stroke-width="1.2" marker-end="url(#kad-arrow)"/>
<text x="272" y="103" fill="#6A6F8C" font-size="8">endovisor ABI: /dev/kernelet</text>
<path d="M100 180 V228" stroke="#9AA0BE" stroke-width="1.2" marker-end="url(#kad-arrow)"/>
<path d="M265 180 V250" stroke="#9AA0BE" stroke-width="1.2" marker-end="url(#kad-arrow)"/>
<path d="M695 70 V132" stroke="#00F7FF" stroke-width="1.2" marker-end="url(#kad-arrow-cyan)"/>
<text x="702" y="103" fill="#5C93A8" font-size="8">syscalls, through user_run</text>
<path d="M695 180 V228" stroke="#00F7FF" stroke-width="1.2" marker-end="url(#kad-arrow-cyan)"/>
<path d="M695 296 V322" stroke="#00F7FF" stroke-width="1.2" marker-end="url(#kad-arrow-cyan)"/>
<path d="M340 156 H520" stroke="#00F7FF" stroke-width="1.2" stroke-dasharray="4 3" marker-end="url(#kad-arrow-cyan)" marker-start="url(#kad-arrow-cyan)"/>
<text x="430" y="149" fill="#5C93A8" font-size="8" text-anchor="middle">virtio over function calls</text>
<text x="430" y="170" fill="#5C93A8" font-size="8" text-anchor="middle">vsock</text>
<path d="M332 259 H520" stroke="#9AA0BE" stroke-width="1.2" marker-end="url(#kad-arrow)"/>
<text x="430" y="252" fill="#6A6F8C" font-size="8" text-anchor="middle">entry table: enter, and redirect</text>
<path d="M520 281 H332" stroke="#00F7FF" stroke-width="1.2" marker-end="url(#kad-arrow-cyan)"/>
<text x="430" y="298" fill="#5C93A8" font-size="8" text-anchor="middle">service table: 14 C-ABI calls</text>
<text x="450" y="378" fill="#4C5170" text-anchor="middle" font-size="9">same source · two builds · the host schedules carriers · no hypervisor · no VM exit</text>
</g>
</svg>
<figcaption>The kernelet architecture. Left: the host kernel, its Linux functionality and the endovisor beside it, over OSTD, which exposes the vanilla OSTD API and the kernelet API; the endovisor drives the control half and serves the kernelet runtime through the endovisor ABI. Right: a kernelet, the same Linux functionality over vOSTD, whose virtualized OSTD API reaches the host only through the service table, and which the host enters only through the entry table, to start a carrier or to redirect one. The dashed line is the user–kernel boundary; devices and channels are function calls between the endovisor and the kernelet.</figcaption>
</figure>

This chapter is the design of Asterinas Kernelets, built from the figure above. It answers four questions, and each page says which one it serves.

| question | pages |
|---|---|
| 1. What API does OSTD provide so that the endovisor can manage the lifecycle of kernelets and customize their behavior? | [The kernelet API: control half](kernelet-api-control.md) |
| 2. How is the API of vOSTD virtualized, item by item? | [Virtualizing OSTD](virtualizing-ostd/index.md) and its pages on [memory](virtualizing-ostd/memory.md), [tasks](virtualizing-ostd/tasks.md), [interrupts and time](virtualizing-ostd/interrupts-and-time.md), [user mode](virtualizing-ostd/user-mode.md), [devices](virtualizing-ostd/devices.md) and [the rest](virtualizing-ostd/the-rest.md) |
| 3. What API does OSTD expose to vOSTD, and how does a call cross? | [The kernelet API: service half](kernelet-api-service.md) |
| 4. How is an OCI-compatible kernelet runtime built on the endovisor's user-space ABI, and how is the endovisor built on OSTD? | [The endovisor](endovisor.md) and [The kernelet runtime](kernelet-runtime.md) |

Two pages come before the questions, because every answer depends on them: [Boundaries and trust](principles.md) names the interfaces and states the invariants, and [Builds and images](builds-and-images.md) fixes how the two builds are produced and how the kernelet image is laid out and entered. Four pages come after: [Faults, termination, and reclamation](faults-and-reclamation.md) and [Channels](channels.md) cut across the resources, and [The endovisor](endovisor.md) and [The kernelet runtime](kernelet-runtime.md) answer the fourth question.

In this chapter:

- [Boundaries and trust](principles.md)
- [Builds and images](builds-and-images.md)
- [The kernelet API: control half](kernelet-api-control.md)
- [The kernelet API: service half](kernelet-api-service.md)
- [Virtualizing OSTD](virtualizing-ostd/index.md)
  - [Memory](virtualizing-ostd/memory.md)
  - [Tasks, virtual CPUs, and carriers](virtualizing-ostd/tasks.md)
  - [Scheduling](virtualizing-ostd/scheduling.md)
  - [Interrupts and time](virtualizing-ostd/interrupts-and-time.md)
  - [User mode](virtualizing-ostd/user-mode.md)
  - [Devices](virtualizing-ostd/devices.md)
  - [Boot, power, panic, and the rest](virtualizing-ostd/the-rest.md)
- [Faults, termination, and reclamation](faults-and-reclamation.md)
- [Channels](channels.md)
- [Zero-copy I/O](zero-copy-io.md)
- [The endovisor](endovisor.md)
- [The kernelet runtime](kernelet-runtime.md)

The decisions and assumptions the chapter makes are collected in the [design register](../../notes/design-register.md), and the OSTD API it classifies is enumerated in the [OSTD API inventory](../../notes/ostd-api-inventory.md), both in The Notes.
