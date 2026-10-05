# Executive Summary

This book is written for two kinds of reader, and it tries to serve both without shortchanging either.

**Asterinas developers** will build this. For them the book is a specification, [The Blueprint](blueprint/README.md), written to be precise enough that a coding agent can work from it.

**Academic readers** will judge it. For them the book is the long form of a paper, [The Paper](paper/abstract.md), with the design's costs, its limits and what has not been verified stated as plainly as its claims.

The two pitches below are the same idea told to each audience.

## The pitch to industry {#industry}

> **Give every agent its own kernel.**

**Agents are multiplying. Every one of them needs its own computer, one it cannot escape.**

Agents install dependencies, run tests, compile code, drive browsers. They don't need a function runtime; they need a full Linux machine that can be torn down and rebuilt at will. Sandboxing has gone from an ops option to a primitive of AI infrastructure, and every sandbox available today is either not fast enough or not strong enough.

A kernelet is a complete, Linux-compatible kernel light-weight enough to give one to every agent. Asterinas runs many kernelets side by side inside a single host kernel, each written in safe Rust and each serving one tenant, so unmodified Linux applications get a kernel of their own. VMs pay a price for hardware isolation; kernelets don't. Containers share a kernel; kernelets don't.

<figure class="fwd-fig">
<div class="head">
<div class="tag">Three ways to give an agent a computer</div>
<div class="title">Kernelets: speed of containers, security of microVMs</div>
</div>
<div class="cmp">
<div class="col">
<div class="tag">Option A</div>
<div class="name">Containers</div>
<svg viewBox="0 0 300 190" role="img" aria-label="Containers share one Linux kernel">
<g font-family="ui-monospace,monospace" font-size="10.5">
<rect x="14" y="12" width="80" height="40" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<rect x="110" y="12" width="80" height="40" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<rect x="206" y="12" width="80" height="40" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="54" y="36" fill="#9AA0BE" text-anchor="middle">agent A</text>
<text x="150" y="36" fill="#9AA0BE" text-anchor="middle">agent B</text>
<text x="246" y="36" fill="#9AA0BE" text-anchor="middle">agent C</text>
<path d="M54 52 v22 M150 52 v22 M246 52 v22" stroke="#FF5C7A" stroke-width="1.4" stroke-dasharray="3 3"/>
<rect x="14" y="76" width="272" height="70" rx="8" fill="rgba(255,92,122,.10)" stroke="rgba(255,92,122,.45)"/>
<text x="150" y="105" fill="#FF8FA3" text-anchor="middle" font-size="12">ONE SHARED LINUX KERNEL</text>
<text x="150" y="124" fill="#8A6070" text-anchor="middle" font-size="9.5">~30M LoC of C · full syscall surface</text>
<rect x="14" y="156" width="272" height="24" rx="5" fill="rgba(255,255,255,.03)" stroke="rgba(255,255,255,.12)"/>
<text x="150" y="172" fill="#6A6F8C" text-anchor="middle" font-size="9.5">hardware</text>
</g>
</svg>
</div>
<div class="col">
<div class="tag">Option B</div>
<div class="name">microVMs</div>
<svg viewBox="0 0 300 190" role="img" aria-label="microVMs stack a second kernel behind hardware nesting">
<g font-family="ui-monospace,monospace" font-size="10.5">
<rect x="14" y="12" width="80" height="26" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<rect x="110" y="12" width="80" height="26" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<rect x="206" y="12" width="80" height="26" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="54" y="29" fill="#9AA0BE" text-anchor="middle">agent A</text>
<text x="150" y="29" fill="#9AA0BE" text-anchor="middle">agent B</text>
<text x="246" y="29" fill="#9AA0BE" text-anchor="middle">agent C</text>
<rect x="14" y="44" width="80" height="26" rx="5" fill="rgba(127,129,140,.14)" stroke="rgba(255,255,255,.2)"/>
<rect x="110" y="44" width="80" height="26" rx="5" fill="rgba(127,129,140,.14)" stroke="rgba(255,255,255,.2)"/>
<rect x="206" y="44" width="80" height="26" rx="5" fill="rgba(127,129,140,.14)" stroke="rgba(255,255,255,.2)"/>
<text x="54" y="61" fill="#B9BED8" text-anchor="middle">guest krnl</text>
<text x="150" y="61" fill="#B9BED8" text-anchor="middle">guest krnl</text>
<text x="246" y="61" fill="#B9BED8" text-anchor="middle">guest krnl</text>
<rect x="14" y="80" width="272" height="22" rx="4" fill="rgba(255,176,32,.12)" stroke="rgba(255,176,32,.5)"/>
<text x="150" y="95" fill="#FFC65C" text-anchor="middle" font-size="10">EPT · VM EXIT · vCPU SCHEDULING</text>
<rect x="14" y="110" width="272" height="36" rx="7" fill="rgba(255,255,255,.05)" stroke="rgba(255,255,255,.16)"/>
<text x="150" y="133" fill="#9AA0BE" text-anchor="middle">host kernel + VMM</text>
<rect x="14" y="156" width="272" height="24" rx="5" fill="rgba(255,255,255,.03)" stroke="rgba(255,255,255,.12)"/>
<text x="150" y="172" fill="#6A6F8C" text-anchor="middle" font-size="9.5">hardware</text>
</g>
</svg>
</div>
<div class="col win">
<div class="tag">Option C</div>
<div class="name">Asterinas Kernelets</div>
<svg viewBox="0 0 300 190" role="img" aria-label="kernelets put safe-Rust kernel instances side by side over a small sound core">
<defs>
<linearGradient id="fwd-cg" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="#00F7FF" stop-opacity=".22"/>
<stop offset="100%" stop-color="#1937FF" stop-opacity=".22"/>
</linearGradient>
</defs>
<g font-family="ui-monospace,monospace" font-size="10.5">
<rect x="14" y="12" width="80" height="26" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<rect x="110" y="12" width="80" height="26" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<rect x="206" y="12" width="80" height="26" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="54" y="29" fill="#9AA0BE" text-anchor="middle">agent A</text>
<text x="150" y="29" fill="#9AA0BE" text-anchor="middle">agent B</text>
<text x="246" y="29" fill="#9AA0BE" text-anchor="middle">agent C</text>
<rect x="14" y="46" width="80" height="52" rx="6" fill="url(#fwd-cg)" stroke="rgba(0,247,255,.55)"/>
<rect x="110" y="46" width="80" height="52" rx="6" fill="url(#fwd-cg)" stroke="rgba(0,247,255,.55)"/>
<rect x="206" y="46" width="80" height="52" rx="6" fill="url(#fwd-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="54" y="70" fill="#8FF6FC" text-anchor="middle">Kernelet A</text>
<text x="150" y="70" fill="#8FF6FC" text-anchor="middle">Kernelet B</text>
<text x="246" y="70" fill="#8FF6FC" text-anchor="middle">Kernelet C</text>
<text x="54" y="87" fill="#5C93A8" text-anchor="middle" font-size="8.5">safe Rust</text>
<text x="150" y="87" fill="#5C93A8" text-anchor="middle" font-size="8.5">safe Rust</text>
<text x="246" y="87" fill="#5C93A8" text-anchor="middle" font-size="8.5">safe Rust</text>
<rect x="14" y="108" width="272" height="24" rx="5" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="150" y="124" fill="#00F7FF" text-anchor="middle" font-size="10">OSTD: small, sound, partly proven</text>
<text x="150" y="150" fill="#4C5170" text-anchor="middle" font-size="9">no hypervisor · no EPT · no VM exit</text>
<rect x="14" y="156" width="272" height="24" rx="5" fill="rgba(255,255,255,.03)" stroke="rgba(255,255,255,.12)"/>
<text x="150" y="172" fill="#6A6F8C" text-anchor="middle" font-size="9.5">hardware</text>
</g>
</svg>
</div>
</div>
</figure>

## The pitch to academia {#academia}

> **API virtualization: a third way between machine virtualization and OS virtualization.**

Two ways of multiplexing a machine among mutually distrusting tenants have dominated for twenty years. **Machine virtualization** runs an unmodified guest kernel on virtual hardware; the boundary is the hardware's, strong and well understood, and the price is a second layer of address translation, VM exits, a device model, and a whole guest kernel's memory and scheduler per tenant. **OS virtualization** multiplexes one kernel through namespaces and control groups; the price is near zero, and the boundary is the correctness of thirty million lines of C that every tenant can drive through the full syscall table.

Asterinas Kernelets are a third point in that space, and this book calls the idea **API virtualization**: rather than virtualizing the hardware beneath a kernel, or multiplexing one kernel above its syscall table, it virtualizes the interface a kernel is written against. The Asterinas kernel is a *framekernel*: all `unsafe` code lives in a small framework, OSTD, and the kernel above it is safe Rust written against OSTD's API. A **kernelet** is that kernel, unmodified, compiled against **vOSTD**, a build of OSTD's own source in which every operation's effect is confined to the kernelet that makes it, and whatever a kernelet must never have does not exist. The boundary is the language, not the hardware: the crate graph decides what a kernelet can name, and a table of C-ABI calls decides what may cross. Each tenant gets a kernel of its own, and the machine underneath stays real; the price is that the boundary rests on the compiler and on OSTD's soundness.

## A growth strategy: kernelets in Linux {#growth}

> **Ship in Linux, before replacing Linux.**

<figure class="fwd-fig">
<div class="head">
<div class="tag">The near-term target</div>
<div class="title">Kernelets in Linux: nothing replaced, something added</div>
</div>
<svg viewBox="0 0 900 318" role="img" aria-label="One machine running one Linux kernel. Above it, on the left, today's workloads continue unchanged on Linux's own subsystems. On the right, selected tenants each get a kernelet, a safe-Rust kernel of their own, which lives inside the same Linux kernel alongside its subsystems and is managed by the endovisor, a loadable module, with a small patch to the kernel it runs in. Both halves sit on the same hardware.">
<defs>
<linearGradient id="fwd-lg" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="#00F7FF" stop-opacity=".22"/>
<stop offset="100%" stop-color="#1937FF" stop-opacity=".22"/>
</linearGradient>
</defs>
<g font-family="ui-monospace,monospace" font-size="11">
<text x="24" y="14" fill="#6A6F8C" font-size="9.5">TODAY'S WORKLOADS, UNCHANGED</text>
<text x="876" y="14" fill="#00F7FF" font-size="9.5" text-anchor="end">TENANTS THAT WANT THEIR OWN KERNEL</text>
<rect x="24" y="22" width="120" height="30" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<rect x="160" y="22" width="120" height="30" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<rect x="296" y="22" width="120" height="30" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="84" y="41" fill="#9AA0BE" text-anchor="middle">app</text>
<text x="220" y="41" fill="#9AA0BE" text-anchor="middle">app</text>
<text x="356" y="41" fill="#9AA0BE" text-anchor="middle">container</text>
<rect x="484" y="22" width="120" height="30" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(0,247,255,.30)"/>
<rect x="620" y="22" width="120" height="30" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(0,247,255,.30)"/>
<rect x="756" y="22" width="120" height="30" rx="5" fill="rgba(255,255,255,.06)" stroke="rgba(0,247,255,.30)"/>
<text x="544" y="41" fill="#9AA0BE" text-anchor="middle">agent A</text>
<text x="680" y="41" fill="#9AA0BE" text-anchor="middle">agent B</text>
<text x="816" y="41" fill="#9AA0BE" text-anchor="middle">agent C</text>
<line x1="16" y1="64" x2="884" y2="64" stroke="rgba(255,255,255,.18)" stroke-dasharray="4 4"/>
<text x="16" y="60" fill="#4C5170" font-size="8.5">user mode</text>
<text x="16" y="76" fill="#4C5170" font-size="8.5">kernel mode</text>
<rect x="16" y="84" width="868" height="168" rx="10" fill="rgba(255,255,255,.025)" stroke="rgba(255,255,255,.12)"/>
<text x="450" y="240" fill="#9A9DB0" text-anchor="middle" font-size="10.5">the Linux kernel the operator already runs, already patches, already trusts</text>
<rect x="36" y="104" width="380" height="106" rx="6" fill="rgba(255,255,255,.06)" stroke="rgba(255,255,255,.16)"/>
<text x="226" y="152" fill="#9AA0BE" text-anchor="middle">Linux's own subsystems</text>
<text x="226" y="172" fill="#6A6F8C" text-anchor="middle" font-size="8.5">patched: one small gate</text>
<rect x="484" y="104" width="120" height="62" rx="6" fill="url(#fwd-lg)" stroke="rgba(0,247,255,.55)"/>
<rect x="620" y="104" width="120" height="62" rx="6" fill="url(#fwd-lg)" stroke="rgba(0,247,255,.55)"/>
<rect x="756" y="104" width="120" height="62" rx="6" fill="url(#fwd-lg)" stroke="rgba(0,247,255,.55)"/>
<text x="544" y="130" fill="#8FF6FC" text-anchor="middle">Kernelet A</text>
<text x="680" y="130" fill="#8FF6FC" text-anchor="middle">Kernelet B</text>
<text x="816" y="130" fill="#8FF6FC" text-anchor="middle">Kernelet C</text>
<text x="544" y="148" fill="#5C93A8" text-anchor="middle" font-size="8.5">safe Rust</text>
<text x="680" y="148" fill="#5C93A8" text-anchor="middle" font-size="8.5">safe Rust</text>
<text x="816" y="148" fill="#5C93A8" text-anchor="middle" font-size="8.5">safe Rust</text>
<rect x="484" y="182" width="392" height="28" rx="5" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="680" y="200" fill="#00F7FF" text-anchor="middle" font-size="10">endovisor: one loadable module</text>
<rect x="16" y="266" width="868" height="28" rx="5" fill="rgba(255,255,255,.03)" stroke="rgba(255,255,255,.12)"/>
<text x="450" y="284" fill="#6A6F8C" text-anchor="middle" font-size="9.5">hardware</text>
<text x="450" y="310" fill="#4C5170" text-anchor="middle" font-size="9">same machine &#183; same kernel &#183; no hypervisor &#183; adopt one workload at a time</text>
</g>
</svg>
</figure>

**The hardest argument is "replace your kernel".** An operator asked to put Asterinas underneath a fleet is being asked to bet the machine on a young code base, and no amount of safe Rust makes that an easy signature. It is the right argument to win eventually and the wrong one to need first — and we do not need it first, because of a property of the design rather than a concession in it. A kernelet is not built on a machine; it is built on an interface. It calls a table of functions and never touches hardware, and nothing in that arrangement says who implements the table. If the answer can be Linux, the host underneath is a replaceable part, which is what [Design for Linux](blueprint/linux-mode/README.md) sets out to test. That chapter gives the design, and a prototype runs a small kernel on it; the full endovisor is not built.

That changes the question an operator is asked. The host stays the kernel they already run, with one small patch to the path a system call takes; what changes is that selected workloads stop sharing it with their neighbors and get a kernel of their own, in safe Rust, inside the same machine. Nothing is replaced, and the decision is reversible — a workload that does not suit a kernelet keeps running the way it runs today, beside one that does, so the cost of being wrong is one workload rather than one fleet. It also gives the project somewhere real to grow, because the same source, the same framework and the same kernel proper compile against a second implementation of one table: the work is not done twice and the maturity is not earned twice.

A second implementation pays a dividend a single one cannot. Porting the interface to a host we do not control exposed a requirement our own API had never stated, which no amount of reading our own kernel would have revealed. So there are two targets and they are the same design. The near one is a foothold, with real tenants on a kernel they can already deploy. The far one is the host role itself, and the point of building on an interface rather than on a machine is that arriving at the second does not mean starting the first again.

## How to read this book {#how-to-read}

You need to know Rust and roughly how an operating-system kernel is put together. Nothing else is assumed. The book is three volumes, and they are meant to be read in the order that suits the reader rather than the order they were written in.

- [**The Paper**](paper/abstract.md) is the idea in its most concise form, written as a research paper.
- [**The Blueprint**](blueprint/README.md) is the design document, written so that a coding agent can implement from it.
- [**The Notes**](notes/README.md) hold the working material behind the other two.

Logically the Notes came first, the Blueprint was built from them, and the Paper was distilled from the Blueprint.

If you read only one volume, read the Paper. If you will build it, read the Blueprint.
