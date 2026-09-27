# Builds and images

This page fixes how the two builds of OSTD are produced, how the kernel proper becomes a kernelet image, where that image's bytes live in memory, how it is entered, and what the build checks. It takes the three mechanism decisions that the Overview's fixed decisions do not settle; each is stated with the alternative it rejects and recorded in the [design register](../../notes/design-register.md). Two terms are defined here and used throughout: a **kind** is a registered kernelet image, and a **grain** is the 2 MiB unit in which memory is granted to a kernelet.

## Two builds from one source {#two-builds}

**Decision D1.** The vOSTD is the `ostd` crate compiled with a Cargo feature named `kernelet`. The host build is the same crate without it. The kernelet image and the host image are separate Cargo builds, so feature unification, which would merge the two feature sets inside one build, never applies. Because Cargo features are additive, the kernelet build is always invoked as `--no-default-features --features kernelet`, which is how `cvm_guest` stays off.

```toml
# ostd/Cargo.toml
[features]
default = ["cvm_guest"]
cvm_guest = ["dep:tdx-guest"]
# The kernelet build: the API a kernelet's kernel is compiled against.
kernelet = []
```

```rust
// ostd/src/lib.rs
#[cfg(all(feature = "kernelet", feature = "cvm_guest"))]
compile_error!("`kernelet` and `cvm_guest` are mutually exclusive");
```

Inside OSTD the feature selects implementations, not modules. An item that is *identical* in the taxonomy has no `cfg` at all. A *virtualized* item keeps its public signature and gets a second body under `#[cfg(feature = "kernelet")]`, usually in a sibling file (`task/kernelet.rs` beside `task/mod.rs`) so that the two bodies sit next to each other in review. An *absent* item is `#[cfg(not(feature = "kernelet"))]` on its definition, so that a kernel that uses it fails to compile in vOSTD with an unresolved name rather than at run time. Which of the three an item is can therefore be read from the source, and a change to OSTD that forgets the kernelet side fails the kernelet build in continuous integration. Whole modules are absent too: `arch::x86::boot`, which carries the multiboot headers and the `bsp_boot` and `ap_boot` assembly with their physical-address segments, is `cfg(not(feature = "kernelet"))`, since a kernelet is entered by the host, not by a bootloader.

The kernel proper is built the same way. The kernel crates gain a `kernelet` feature that turns off the components that face the machine and turns on `forbid(unsafe_code)`:

```toml
# kernel/Cargo.toml (excerpt)
[features]
default = ["cvm_guest"]
kernelet = ["aster-core/kernelet", "ostd/kernelet"]

# kernel/core/Cargo.toml (excerpt): components present only in the host kernel
[dependencies]
aster-pci = { path = "comps/pci", optional = true }
# ... nvme, uart, i8042, framebuffer, and the machine-facing half of time

[features]
kernelet = ["ostd/kernelet"]
host = ["dep:aster-pci", "dep:aster-nvme", "dep:aster-uart", "dep:aster-i8042", "dep:aster-framebuffer"]
default = ["host"]
```

```rust
// kernel/core/src/lib.rs
#![no_std]
#![deny(unsafe_code)]
#![cfg_attr(feature = "kernelet", forbid(unsafe_code))]
```

The `virtio` component stays in both builds; its PCI transport is behind the `host` feature and its MMIO transport is what a kernelet uses ([Devices](virtualizing-ostd/devices.md)). The [OSTD API inventory](../../notes/ostd-api-inventory.md) lists which components face the machine; those six (`pci`, `nvme`, `uart`, `i8042`, `framebuffer`, and the TSC and CMOS parts of `time`) are host-only, and the kernel proper's own files that touch machine items, 16 files under `kernel/core/src` (counted on the tree, [Virtualizing OSTD](virtualizing-ostd/index.md)), get a `cfg` on the offending lines rather than on the file.

*Rejected alternative.* A second package over the same source, `ostd-kernelet` with `[lib] path = "../ostd/src/lib.rs"` and a build script setting `--cfg kernelet`. It would let one Cargo build contain both variants, which was the reason to consider it, but nothing needs that once the two images are separate builds; it doubles the manifests to maintain and makes `cargo` treat the two as unrelated crates for versioning and publishing. The feature is the simpler mechanism and the two-build structure removes its only drawback.

*Cost.* Two full builds of OSTD and of the kernel proper per boot image, roughly doubling build time (*estimated*); two copies of the compiled text in the boot image, the kernelet's shared among its kernelets.

## The kernelet image {#window}

The kernel proper and vOSTD link into one ELF, the **kernelet image**. The host kernel is a separate ELF, the **host image**. One OSDK invocation builds the kernelet image first and the host image second, embedding the kernelet image's bytes into the host image; the result is one **boot image**, and nothing else is loaded at run time.

**Decision D3 (revised again).** The kernelet image is **position-independent**, and the host loads each instance at an offset of its own choosing in the **shared** kernel address space. There is no per-kernelet kernel page table and no fixed window. One physical copy of the image's read-only segments serves every instance of a kind; each instance gets private frames for its writable segment, mapped immediately after its own text at the same distance in every instance. Program-counter-relative addressing therefore reaches the running instance's data with no register, no table and no lookup: the same instruction, executed through instance A's mapping, computes A's data, and through B's, B's. This is [measured](../linux-mode/prototype.md#earlier), and the reasoning is on [One text, many instances](../linux-mode/builds-and-images.md#sharing).

The image's regions keep their names and their order; what changed is that they are now **offsets from the instance's base** rather than absolute addresses, and that the physical window is gone.

| region | where | contents | mapping |
|---|---|---|---|
| `KW_TEXT` | offset 0 | `.kernelet_entry_table` at offset 4 KiB, then `.text`, `.ex_table`, `.rodata`, `.eh_frame_hdr`, `.eh_frame`, `.gcc_except_table`, `.ktest_array` — **everything that holds no address**, `.ex_table` included once it is encoded as D87 requires | the kind's shared frames, **one physical copy for all instances of the kind**, mapped read-only and executable in each instance's range |
| `KW_DATA` | after the read-only segments, page-aligned | everything that holds an address and must therefore be relocated per instance: `.got`, `.data.rel.ro`, `.init_array`; then the writable segment in the tree's order, `.data`, `.cpu_local`, `.bss`; then `.cpu_local` replicated once per virtual CPU; then vOSTD's own tables | private frames per kernelet, copied from the image's data template at creation, relocated for this instance, read-write and non-executable |
| `KW_SHARED` | after `KW_DATA` | the boot arguments, the info page, the grant table, the host-wide clock page, the task records and the per-virtual-CPU records | host frames, mapped read-only or read-write as the page requires |
| `KW_META` | a separate range, base in `BootArgs` | the 64-byte `MetaSlot` of every granted frame ([Memory](virtualizing-ostd/memory.md)) | host frames dedicated to the kernelet, charged to it, zeroed, mapped read-write when the grain is granted |

Granted frames are **not** mapped into the image's range at all. vOSTD reaches them through the host's own linear map, whose base the host writes into `BootArgs` at creation and which the instance holds in its own data; `paddr_to_vaddr` is that base plus the physical address, one add, as on the tree ([Memory](virtualizing-ostd/memory.md)).

Kernel stacks are not in the image either. A kernelet's tasks are host-owned tasks ([Tasks](virtualizing-ostd/tasks.md)), and their stacks live where every task's stack lives; host code that runs on a kernelet's behalf runs on the same stack.

*Why position-independent.* It is the only arrangement in which many kernelets share one kernel address space, and a shared kernel address space is not a preference: on a host whose kernel half is common to every process, which is every host but one we write ourselves, it is the only thing on offer. Taking it also removes, in both modes, the two top-level page-table entries per kernelet and everything beneath them, the rule that the window's entries cannot be Global and the translation refill that rule costs after every address-space switch, and the assumption that the machine's physical memory fits in a 512 GiB window.

**Decision D87.** The exception table is encoded as pairs of **self-relative offsets**, not of absolute addresses. On the tree it is the latter: its entries are `Vaddr` fields (`ostd/src/mm/fault/ex_table.rs`) filled by `.quad` directives in the four fallible-copy stubs (`ostd/src/arch/x86/mm/*_fallible.S`), eight of them in all. Under D3 every one of those is a relocation, so as written the section holds addresses, belongs on the private side by the audit's own rule, and would fail [audit item 8](#audit) — the audit doing its job while the region table claims otherwise.

Re-encoded, the table holds no address, is shareable, and is identical in every instance, which is how Linux encodes its own. It is a small change to the four stubs and to the walker that reads them; it is worth making on **both** hosts; and it is what lets one image format serve a Linux host, where nothing consults the table because no kernelet instruction is expected to fault ([Builds and images](../linux-mode/builds-and-images.md)).

*Code model.* The kernelet build uses `-C relocation-model=pic -C code-model=small` with every symbol at hidden visibility, and links a position-independent image whose **shared regions carry no relocations**: every reference from code is program-counter-relative, and every relocation in the file lands in a region the host copies and fixes up per instance. That the shared regions are relocation-free is what lets one physical copy serve every instance, and the [audit](#audit) checks it. Code and data must stay within ±2 GB of each other for the small model, which the layout above guarantees.

Which sections hold addresses is not a matter of opinion, so it was measured. A Rust staticlib built with these flags for `x86_64-unknown-none`, containing the shapes a kernel image is full of — tables of trait objects, of string slices and of function pointers — puts **zero** relocations in `.text` (274,307 bytes) and **zero** in `.rodata` (64,914 bytes), and puts all 300 of them in `.data.rel.ro` (181) and `.got` (119); all 300 are of one type, `R_X86_64_RELATIVE`. A C build of the same shapes adds one in `.init_array`. So 98 percent of the read-only material is shareable and the relocated part is a few kilobytes. The measurement is in [The prototype](../linux-mode/prototype.md#earlier).

*Rejected alternative.* Fixed addresses with a private kernel page table per kernelet, which is what D3 said before. It kept the loader trivial, since an image linked at fixed addresses needs no relocation processing, and it gave kernelet-versus-kernelet separation in the page tables. It is rejected because it cannot be done on a host whose kernel half is shared, and because the separation it appeared to give was not real: the host's linear map is shared into every kernelet's page table, so a kernelet could already address every physical frame on the machine, as the paragraph below has always said. What it really cost was two level-3 tables and their descendants per kernelet, non-Global window mappings, and a bound on the machine's physical memory.

*Cost.* A relocation processor in the trusted base: a loop over the writable segment's fixups, adding the instance's base to each. It is new trusted code and it is the price of the scheme. And kernelets are now mutually **addressable**: a stray kernel pointer in one kernelet that named another's data would no longer meet an unmapped page. As above, such a pointer could already reach any frame through the linear map, so this removes a backstop that caught a narrow class of accident rather than a boundary; the security argument rests, as [Boundaries and trust](principles.md) has always said it does, on the kernel proper being safe Rust and vOSTD being correct.

## The data template and per-kernelet state

At creation the control half copies the image's `.data` bytes and zeroes `.bss` into a fresh 2 MiB-aligned run of frames mapped at `KW_DATA` as one 2 MiB page, in the segment's own order, so that a CR3 write costs the kernelet one translation for its data rather than dozens. The template is the image's writable segment, read from the embedded ELF; nothing depends on section globs or on link-time optimization preserving per-static sections. The `.cpu_local` section is replicated once per virtual CPU from the next page boundary after the segment, and vOSTD addresses a replica by virtual-CPU index ([Tasks](virtualizing-ostd/tasks.md)).

The measured sizes on the tree's own debug image (`target/x86_64-unknown-none/debug/asterinas-osdk-bin`, built 2026-08-28) are `.data` 27,464 bytes, `.bss` 23,944 bytes and `.cpu_local` 1,977 bytes. That is the whole host kernel; the kernelet image's writable segment will differ, larger by vOSTD's tables and smaller by the host-only components. *Estimated*: under 128 KiB of private writable state per kernelet before the per-CPU replicas, plus 2 KiB per virtual CPU. The heap is granted memory and the frame metadata is host memory charged per grain, and neither is part of this floor.

## Embedding and registration

The kernelet image is embedded into the host image by the endovisor module of the kernel crate:

```rust
// kernel/core/src/endovisor/images.rs
static LINUX_KERNELET: &[u8] = include_bytes!(env!("KERNELET_IMAGE_LINUX"));
```

OSDK sets `KERNELET_IMAGE_LINUX` to the path of the kernelet image it built in the previous step. At boot the endovisor registers each embedded image with the control half, which parses the ELF's program headers and *copies* each `PT_LOAD` segment into fresh frames: the read-only segments become the kind's shared frames, the writable segment becomes the data template. Copying, rather than mapping the embedded bytes in place, is what lets `include_bytes!` be an ordinary byte string with no alignment requirement, and it costs one copy per kind at boot, about 14 MiB for the tree's debug image (`.text` 13.8 MB plus `.rodata` and `.eh_frame`; measured with `size -A`) and less for a release build. The parser needs the `PT_LOAD` headers, the `PT_DYNAMIC` header and the three dynamic entries that locate the relocation table within it, the ELF entry address `e_entry`, and the entry table, which lies at a fixed offset (below). It reads no symbol table and no section headers, which is important because section headers are not part of what a loader sees.

*Rejected alternative.* Passing kernelet images as bootloader modules, which OSTD already supports as `MemoryRegionType::Module`. It would allow a kind to be replaced without relinking the host image, and it is the natural extension when that is wanted. It is not the default because it makes the boot image two files, and because the build's attestation that the kernel proper contains no `unsafe` outside the allowlist is only as good as the build that produced *both* files together.

## The entry point and the two tables {#entry}

**Decision D2.** Control and data cross the ELF boundary only through two tables of `extern "C"` function pointers and the shared pages. The kernelet image's linker script names `_kernelet_entry` as the ELF entry point (`ENTRY(_kernelet_entry)`, so `e_entry` locates it) and places the entry table in its own output section 4 KiB from the image base, so the host finds both without a symbol table. Both are offsets from the base the loader chose, not absolute addresses.

```rust
// vOSTD, ostd/src/kernelet_side/entry.rs

/// The one function the host calls to start a kernelet.
/// Runs on the kernelet's boot task, on the kernelet's kernel page table,
/// with `KW_DATA` already populated from the template. Never returns.
#[unsafe(no_mangle)]
pub extern "C" fn _kernelet_entry(
    services: &'static ServiceTable,
    boot: &'static BootArgs,
) -> ! { /* store `services`; init vOSTD; call `__ostd_main` */ }

/// The kernelet image's entry points and the facts about the image the host
/// needs. Read by the host from `KW_TEXT + 4 KiB` at registration; never written.
#[unsafe(no_mangle)]
#[unsafe(link_section = ".kernelet_entry_table")]
pub static KERNELET_ENTRY_TABLE: EntryTable = EntryTable { /* … */ };
```

The **service table** is a `#[repr(C)]` struct whose first field is its size in bytes, followed by function pointers; it is built by OSTD once at boot and the same table is handed to every kernelet. The **entry table** is the mirror image: size, the function through which the host enters kernelet code (the body of a spawned task, by index), the bounds of the image's exception table for kernel-mode page faults, the bounds of its `.cpu_local` section, and the hash of the OSTD source and toolchain the image was built from, which is the one version check: the host compares it with its own at registration, and `BootArgs` carries the host's so that `_kernelet_entry` can compare again and stop if they differ. Both tables are specified in full on [The kernelet API: service half](kernelet-api-service.md).

Everything else a kernelet needs at boot, its identifier, its virtual-CPU count, its initial grant, its command line and the layout of its shared pages, is in `BootArgs`, a `#[repr(C)]` struct in a host frame mapped read-only at the start of `KW_SHARED`, described with the service half.

*Rejected alternative.* Resolving host symbols by relocation when the image is combined, as a Linux kernel module does against exported symbols. It gives direct calls instead of indirect ones, but it needs either a final link that merges two symbol namespaces, with every `core` and `alloc` symbol defined twice, or a relocation processor in OSTD at boot; and it spreads the import surface over the symbol table instead of one struct checked by a source hash. The indirect call costs an estimated one to two cycles when predicted; a whole crossing was measured on the booted prototype at 35 cycles against 34 for a plain call, and the design accepts that order of cost.

## The linker script

OSDK emits a second template for the kernelet build, `x86_64-kernelet.ld.template`, beside the host's. It has three `PT_LOAD` program headers, `text` (R E), `rodata` (R) and `data` (RW), no header or boot segments and no physical load addresses; it places `.kernelet_entry_table` at `KW_TEXT + 0x1000`, then the read-only sections in the tree's order, and the writable sections at `KW_DATA` in the tree's order; and it provides the symbols OSTD's unwinder needs to find frame-description entries, `__executable_start`, `__etext` and `__GNU_EH_FRAME_HDR` (the tree selects the `fde-gnu-eh-frame-hdr` mode of the `unwinding` crate, so without them `catch_unwind` in a kernelet finds nothing), plus `__ex_table`, `__ex_table_end`, `__cpu_local_start`, `__cpu_local_end`, `__sinit_array` and `__einit_array` as the host's script does.

## The audit {#audit}

After every build, before the host image is linked, OSDK checks the kernelet image and fails the build on any of these:

1. The link ran without `--unresolved-symbols` or `-z undefs`, and `readelf --syms` shows no `UND` entry: **the image imports nothing**. `e_type` is `ET_DYN`, which is what a position-independent link produces; since the image imports nothing, the only dynamic-linking machinery it may carry is what its own relocations need, so `.dynsym` holds no undefined symbol and there is no `DT_NEEDED` entry.
2. Every relocation is `R_X86_64_RELATIVE`, and the loader applies each as *store the instance's base plus the entry's own addend*, which is what the platform's specification says the type means. The linker also writes that value into the target word, so reading the word instead happens to work; the loader does not rely on it. This is the only type a position-independent image with no imports needs, and it is the only type the measured image contains, so the relocation loop has a single case. Two other things fail the build: any relocation of another type, `R_X86_64_IRELATIVE` in particular, which resolves by calling a function and which a flat loop cannot implement; and a link that packed the relative relocations into the compressed section a linker produces when asked to, since an audit that counted only the ordinary section would then see no relocations at all, pass, and yield an image the loader never relocates.
3. Every `PT_LOAD` segment lies inside the `KW_TEXT` or `KW_DATA` **offset ranges**, in that order; the writable one inside `KW_DATA` only; no segment is both writable and executable; `e_entry` lies inside `KW_TEXT`; and the distance from the image base to the writable segment is the same for every instance by construction, since it is a property of the file. The check reads program-header flags, not section flags: under the PIC model a `static` of function pointers such as the entry table carries section flags `WA` even in the read-only segment, and only the segment's flags say what is mapped writable.
4. `.kernelet_entry_table` lies at offset `0x1000` from the image base, at the start of `KW_TEXT`, and its `size` field is the one OSTD was compiled against.
5. `.ex_table` and `.cpu_local` lie where the entry table's bounds say; `.cpu_local` ends the writable `PROGBITS` run, with only `.bss` after it.
6. Every crate in the kernelet image's dependency closure is compiled with `-F unsafe_code` (`--forbid`, which a crate cannot override with an inner `allow`, unlike `-D`) unless it is on an allowlist checked into the tree, each entry with a reason. The mechanism is a `RUSTC_WRAPPER` OSDK installs for the kernelet build, which appends the flag for every crate not on the list and prints the list of crates it exempted into the build log; the list is *derived* by the build, not counted by hand, because a hand count is wrong as soon as a dependency changes (a first count on the tree missed `lock_api`, `bitflags`, `scopeguard`, `once_cell`, `heapless`, `foldhash` and `indexmap`, all of which carry `unsafe`, and listed `aster-util`, which does not). The allowlist holds `ostd` (kernelet build), `core`, `alloc`, `compiler_builtins`, `unwinding`, `ostd-pod`, and the third-party crates the closure carries `unsafe` in; the kernel proper's own crates are never on it. So the property the audit establishes is "no `unsafe` outside a named allowlist", not "no `unsafe`". One loophole is accepted and named: the lint does not see `unsafe` that a macro from an allowlisted crate expands to in a `forbid` crate.
7. The entry table's source hash equals the host build's, recorded by OSDK from the OSTD source tree and the toolchain version, and compared again at registration.
8. Every relocation in the image lies inside the `KW_DATA` offset range, which the host copies and fixes up per instance; `.text` and `.rodata` carry none. This is what lets one physical copy of the read-only segments serve every instance, and it replaces the old check that the image carried no relocations at all, which a position-independent image cannot satisfy.
9. `.ex_table`'s entries are self-relative, as D87 requires: no entry holds an absolute address, and the section therefore appears in the relocation-free region. An absolute entry fails the build, which is what item 8 would otherwise catch after the fact.
10. Every indirect-branch target carries the marker that a processor with indirect-branch tracking requires, so the image runs on such a machine. The design turns on indirect calls through the entry and service tables, so this is not optional. The compiler emits the markers when asked, which was [measured](../linux-mode/prototype.md#earlier), and the whole image needs the standard library rebuilt with the same setting. A host whose own build enforces the stricter, type-checked form of the same idea asks more of the image than this check verifies; assumption A19 states what, and it is the one place where a host might reject a kernelet outright. **[unverified]**

The first five checks are what make invariants I1 and I3 properties of the artifact rather than of the source; the sixth is what makes "the kernel proper contains no `unsafe`" a statement about the build.
