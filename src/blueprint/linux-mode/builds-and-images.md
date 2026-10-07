# Builds and images

*How the kernel proper and vOSTD become a file, how the endovisor turns that file into many running kernelets that share one copy of their code, and what the build checks so that the file can be trusted to stay inside its boundary.*

## One source, two builds

The Asterinas kernel is compiled twice from the same source.

- The **host build** is the ordinary kernel for a machine. This chapter does not use it: on Linux, the host kernel is Linux.
- The **kernelet build** compiles the kernel proper against **vOSTD**, which is OSTD's own source with the Cargo feature `kernelet` selected. Under that feature every OSTD item is [identical, virtualized or absent](virtualizing-ostd/README.md), the machine-facing drivers (PCI, NVMe, the serial port, the framebuffer) are left out, and the kernel proper is compiled with `forbid(unsafe_code)`, which turns the project's convention into a compiler error. A second feature, `host-linux`, selects the three host-specific bodies inside vOSTD.

The kernel proper and vOSTD link into one ELF file, the **kernelet image**. A registered image is called a **kind**; a machine typically has one or two kinds and thousands of instances.

## One text, many instances {#sharing}

Thousands of kernelets of one kind should not mean thousands of copies of a 14 MiB kernel text (*measured on the tree*, the debug build). And they cannot each have a private kernel address space to be linked into: on Linux every task shares one kernel half.

So the image is built **position-independent**, and every instance is placed at a different address in Linux's kernel address space, with this layout:

<figure class="fwd-fig">
<div class="head">
<div class="tag">Loading</div>
<div class="title">One physical copy of the text, mapped once per instance, each followed by its own data</div>
</div>
<svg viewBox="0 0 900 270" role="img" aria-label="Physical memory holds one copy of a kind's text and read-only data, and one private data region per instance. In kernel virtual memory, each instance is a contiguous range: first a mapping of the shared text, read and execute only, then that instance's private data, read and write. Because code reaches data by an offset from the program counter, the same instruction executed through instance A's mapping reads instance A's data, and through instance B's mapping reads instance B's data.">
<defs>
<linearGradient id="bi-cg" x1="0" y1="0" x2="1" y2="0">
<stop offset="0%" stop-color="#00F7FF" stop-opacity=".22"/>
<stop offset="100%" stop-color="#1937FF" stop-opacity=".22"/>
</linearGradient>
<marker id="bi-ac" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 0 L8 4 L0 8 z" fill="#00F7FF"/>
</marker>
</defs>
<g font-family="ui-monospace,monospace" font-size="10">
<text x="20" y="22" fill="#00F7FF" font-size="9" letter-spacing="1.4">KERNEL VIRTUAL ADDRESSES</text>
<rect x="20" y="32" width="200" height="50" rx="5" fill="url(#bi-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="120" y="53" fill="#8FF6FC" text-anchor="middle">text + rodata</text>
<text x="120" y="69" fill="#5C93A8" text-anchor="middle" font-size="8.5">read, execute</text>
<rect x="220" y="32" width="130" height="50" rx="5" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="285" y="53" fill="#C9CCE0" text-anchor="middle">data of A</text>
<text x="285" y="69" fill="#5C93A8" text-anchor="middle" font-size="8.5">read, write</text>
<text x="185" y="100" fill="#9AA0BE" text-anchor="middle" font-size="9">instance A, at base A</text>
<rect x="530" y="32" width="200" height="50" rx="5" fill="url(#bi-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="630" y="53" fill="#8FF6FC" text-anchor="middle">text + rodata</text>
<text x="630" y="69" fill="#5C93A8" text-anchor="middle" font-size="8.5">read, execute</text>
<rect x="730" y="32" width="130" height="50" rx="5" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="795" y="53" fill="#C9CCE0" text-anchor="middle">data of B</text>
<text x="795" y="69" fill="#5C93A8" text-anchor="middle" font-size="8.5">read, write</text>
<text x="695" y="100" fill="#9AA0BE" text-anchor="middle" font-size="9">instance B, at base B</text>
<path d="M160 44 Q225 12 270 40" stroke="#00F7FF" stroke-width="1.2" fill="none" marker-end="url(#bi-ac)"/>
<path d="M670 44 Q735 12 780 40" stroke="#00F7FF" stroke-width="1.2" fill="none" marker-end="url(#bi-ac)"/>
<text x="450" y="28" fill="#00F7FF" text-anchor="middle" font-size="9">mov rax, [rip + d]</text>
<text x="450" y="42" fill="#5C93A8" text-anchor="middle" font-size="8.5">same bytes, same d,</text>
<text x="450" y="54" fill="#5C93A8" text-anchor="middle" font-size="8.5">different data</text>
<text x="20" y="160" fill="#9A9DB0" font-size="9" letter-spacing="1.4">PHYSICAL MEMORY</text>
<rect x="330" y="172" width="240" height="50" rx="5" fill="url(#bi-cg)" stroke="rgba(0,247,255,.55)"/>
<text x="450" y="193" fill="#8FF6FC" text-anchor="middle">the kind's text + rodata</text>
<text x="450" y="209" fill="#5C93A8" text-anchor="middle" font-size="8.5">one copy, no addresses inside</text>
<rect x="120" y="172" width="130" height="50" rx="5" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="185" y="201" fill="#C9CCE0" text-anchor="middle">data of A</text>
<rect x="650" y="172" width="130" height="50" rx="5" fill="rgba(25,55,255,.22)" stroke="rgba(0,247,255,.5)"/>
<text x="715" y="201" fill="#C9CCE0" text-anchor="middle">data of B</text>
<path d="M120 84 L400 170 M630 84 L500 170" stroke="rgba(0,247,255,.5)" stroke-width="1.2" stroke-dasharray="4 3"/>
<path d="M285 84 L200 170 M795 84 L720 170" stroke="rgba(255,255,255,.3)" stroke-width="1.2" stroke-dasharray="4 3"/>
<text x="450" y="250" fill="#6A6F8C" text-anchor="middle" font-size="9">the instance is selected by the program counter: no register is reserved, no table is consulted</text>
</g>
</svg>
</figure>

Position-independent code reaches its data by an offset from the address of the running instruction. If each instance's data sits at the same distance after that instance's mapping of the text, the same machine code, executed through mapping A, finds A's data, and through mapping B finds B's. Nothing has to say "which kernelet am I"; the program counter already does.

*Measured on the booted prototype of the mechanism*: one physical page of position-independent text, mapped four times in a Linux 6.12 guest, each mapping followed by a different data page; a call through each mapping returned that instance's value ([the prototype](prototype.md#earlier)).

**What may be shared.** Only bytes that contain no address can be shared, because an address differs per instance. *Measured* on a Rust static library with the shapes a kernel is full of (tables of trait objects, of string slices, of function pointers), linked as the image is: `.text` (274,307 bytes) and `.rodata` (64,914 bytes) contain **no** relocations; all 300 are in `.data.rel.ro` and `.got`, about 5 KiB together, and all are of one type, "add the base". So 98 percent of the read-only material is shareable. **[unverified]** for the real kernelet image, which has not been built.

**The exception table** is the one read-only section that holds addresses on the tree today. The design re-encodes it as pairs of offsets relative to the entry itself, which is how Linux encodes its own, so that it holds no address and can be shared. On Linux nothing consults it (no kernelet instruction is expected to fault), but one image format serves both hosts.

**Per-instance state.** An instance's data region holds, in order: the relocated tables (`.got`, `.data.rel.ro`); `.data` and `.bss`; one copy of the per-CPU section for each [virtual CPU](virtualizing-ostd/tasks.md#vcpus); and vOSTD's own tables. *Measured on the tree*: `.data` 27 KiB, `.bss` 23 KiB and the per-CPU section 2 KiB for the whole kernel, so under 128 KiB of private state per kernelet before the per-CPU copies (*estimated*).

## How the endovisor loads an instance

A kind is registered once: the runtime hands the endovisor the image file, and the endovisor parses its program headers, copies the read-only segments into pages it keeps for the kind's life, and keeps the writable segment as a **data template** together with the relocation list.

Creating an instance is then four steps.

1. **Allocate the data.** Pages from Linux's allocator, sized by the template plus one per-CPU copy per virtual CPU; copy the template in, copy the per-CPU section's initial image into each per-CPU slot (OSTD requires each copy to start as a bitwise copy of the section, not zeroed), and zero the rest.
2. **Build the range.** One call to [`vmap()`](https://elixir.bootlin.com/linux/v6.12/source/mm/vmalloc.c#L3413) with the kind's text pages followed by this instance's data pages. Linux returns one contiguous kernel virtual range: the instance's base.
3. **Make the text executable.** `vmap()` always returns non-executable memory. The endovisor calls [`set_memory_rox()`](https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/pat/set_memory.c#L2118) on the text part of this range, which makes it read-and-execute and never writable-and-executable.
4. **Relocate.** Add the base to each of the few hundred entries in the instance's relocated tables, and mark that part read-only.

Then it fills in the instance's [boot arguments](virtualizing-ostd/the-rest.md) and the carrier of virtual CPU 0 enters the image at its entry point.

**This is where Linux must export something.** A module has no other way to obtain executable memory at an address of its choosing: `vmap()` strips the execute permission, Linux's allocator for executable memory ([`execmem_alloc()`](https://elixir.bootlin.com/linux/v6.12/source/mm/execmem.c#L55)) is not exported, and none of the permission setters are. *Measured*: executing from a plain `vmap()` range faults with Linux's "tried to execute NX-protected page" report. The patch therefore exports `set_memory_rox()`. It must also export `set_memory_rw()`, because the first call makes the same frames read-only in Linux's direct map as a side effect, and they cannot be given back to the page allocator until that is undone. `set_memory_ro()`, for step 4's hardening, is the third. Making a range executable does *not* make the direct-map alias of those frames executable; Linux masks that bit out itself.

**Why not load each instance as a Linux module?** Linux's module loader gives every module a private copy of its text, inside a region that all modules share and that is 1008 MiB on a kernel with address randomization. At 14 MiB of text per instance, that is at most about seventy sandboxes per machine (*arithmetic*), whatever its memory.

Three costs of loading this way are real. `vmap()` maps with 4 KiB pages only, so sharing the text saves memory but not TLB entries. `set_memory_rox()` flushes the TLB on every processor, so creating an instance is a machine-wide event of a few tens of microseconds (*estimated*); a tenant must not be able to cause it at will, which is why the [runtime](kernelet-runtime.md) rate-limits creation. And the first such call on a kind's text frames also write-protects their alias in Linux's direct map, which makes Linux split the large pages that map them there, permanently; the endovisor allocates a kind's text in whole 2 MiB blocks so that the damage is bounded by the size of the text and is done once per kind.

The write protection in the direct map is undone, with `set_memory_rw()`, when the *kind* is unregistered and its frames go back to Linux, never when one instance is destroyed: sibling instances are still executing those frames.

## Entering the image

Control and data cross between the image and the endovisor in exactly two ways.

- **Two tables of C function pointers.** The image exports an **entry table** at a fixed offset (4 KiB from its base): the function at which a secondary virtual CPU enters, the address of the [upcall stub](virtualizing-ostd/scheduling.md#upcall) to which an interrupted virtual CPU is redirected, the bounds of its per-CPU section, and a hash of the source it was built from. The endovisor hands the image a **service table** when it enters it: the [service half](kernelet-api-service.md) of the kernelet API. The image has no undefined symbols. It cannot call Linux, because it cannot name anything in Linux.
- **Five kinds of shared page**, which the endovisor maps after the instance's data: the boot arguments, the grant table, the info page, the clock page, and the virtual CPUs' records ([service half](kernelet-api-service.md#pages)).

## What the build checks {#audit}

The safety of the design rests on properties of the image file, so the build checks them and refuses to produce an image that fails, and the endovisor repeats every check that can be made on the file alone when a kind is registered.

1. The file is position-independent, has no undefined symbol and needs no library.
2. Every relocation is of the one base-relative type, and every relocation lies in the writable region. The text and read-only data contain none.
3. No segment is both writable and executable; the entry point and the entry table are where they should be.
4. The exception table's entries are self-relative.
5. Every crate in the image was compiled with `unsafe` forbidden, except the crates on a named allowlist that is checked in beside the source and changes only by review: vOSTD itself, the Rust core and allocation libraries, the unwinder, and each third-party crate that contains `unsafe`, by name and version. The build computes the set of crates that actually contain `unsafe` and fails if it differs from the list in either direction. The trusted base is therefore a reviewed list, not "whatever the dependency graph pulled in"; what the check cannot see is `unsafe` that an allowlisted crate's macro expands into another crate.
6. The source hash in the entry table matches the vOSTD this endovisor was built to serve.
7. No instruction in the image touches the x87, SSE, AVX or AVX-512 registers (the kernelet target is compiled without them, and the audit disassembles to confirm), because a tenant thread's floating-point state is live in the processor while kernelet code runs on Linux ([User mode](virtualizing-ostd/user-mode.md#fpu)).
8. No function's stack frame exceeds 4 KiB, from the frame sizes the compiler emits on request, because the [entry check](faults-and-reclamation.md#stack) is made before a frame is allocated and the reserve behind it is 16 KiB.

One property is still open. A Linux kernel built with **type-checked indirect branches** (kCFI) verifies, at every indirect call, a hash placed before the target function. The endovisor calls into the image through the entry table, so the image's entry functions would need hashes that Linux's compiler agrees with. The compiler side is *measured* to work (the markers can be emitted), and Linux already requires its C and Rust compilers to agree on these hashes. Whether a separately built image satisfies a kCFI kernel is **[unverified]** (assumption A19). The option needs a kernel built with Clang, which x86-64 distributions do not do today, and can be disabled at boot.

## What this page decides

- **The image is position-independent and each instance is a `vmap()` range of shared text followed by private data** ([register D3](../../notes/design-register.md#decisions), kept). The alternatives are a private kernel address space per kernelet, which Linux cannot provide, and Linux's module loader, which cannot share text.
- **The patch exports `set_memory_rox`, `set_memory_rw` and `set_memory_ro`** ([register D80](../../notes/design-register.md#decisions), revised). Nothing else about loading needs Linux's cooperation.
- **The exception table is self-relative** ([register D87](../../notes/design-register.md#decisions), kept).
