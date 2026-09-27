# Working on the Asterinas Kernelets book

This file is for any agent or person editing the book in this directory. Read it before touching `src/`. Where a rule needs a reason, the reason is given with it; the history of the text is in git.

## What the book is

A design specification that a coding agent can implement from, and the long form of a research paper. Both readings have to hold at once: every mechanism is stated concretely enough to build, and every claim carries its evidence.

## Vocabulary

Use exactly these words, and use them consistently:

| term | meaning |
|---|---|
| **kernelet** | one instance of the Asterinas kernel compiled against the OSTD-shaped API, in kernel mode, in the same address space as every other kernelet |
| **sandbox** | one tenant's environment: a kernelet plus the user-space processes it serves; what a VM or a container is to its tenant |
| **host kernel (or host)** | the kernel that boots the machine, runs init and the host user space, and hosts the kernelets. With Asterinas as the host it is the first instance of the same source as a kernelet, built against OSTD instead of vOSTD; with Linux as the host it is Linux |
| **endovisor** | the component inside the host kernel that creates, schedules, destroys and mediates kernelets; *endo-* because it sits inside the host kernel and beside the kernelets, whereas a hypervisor sits beneath its guests |
| **kernelet runtime** | the user-space program on the host that creates and configures sandboxes, as a container runtime does |
| **virtual CPU** | one of the fixed number of processors a sandbox is given when it is created; the unit the host schedules a sandbox's share in |
| **carrier** | a task of the host kernel that carries one of a kernelet's virtual CPUs: what the host schedules, and what the kernelet runs its own tasks on. Defined in the Overview's terminology; used by the Linux chapter today, and by the Asterinas chapter after the back-port `ALIGNMENT_PLAN.md` describes |

## Voice and honesty

- American spelling: virtualization, behavior, artifact.
- Every number says where it came from: *measured on the booted prototype*, *measured on the tree* (Asterinas at commit `ab9a4cfdc`), *measured in a model*, or *estimated*. A load-bearing claim that is unverified is marked **[unverified]**.

## Structure

- `src/SUMMARY.md` is the single source of order and numbering. The Executive Summary is an unnumbered prefix chapter; after it come three parts, **The Paper** (`src/paper/`), **The Blueprint** (`src/blueprint/`) and **The Notes** (`src/notes/`), numbered continuously by mdBook in order.
- The Paper is the concise, research-paper form of the idea. It is self-contained: a page under `src/paper/` may link only to other pages under `src/paper/` (the checker enforces this), it cites external work by number against `paper/references.md`, and it should convert to LaTeX with little effort, so keep it to plain Markdown, one table and one Mermaid figure. The Blueprint is the design document a coding agent implements from. The Notes hold working material, outdated baselines and surveys; where a Note disagrees with the Blueprint, the Blueprint wins, and where either disagrees with the Paper, the Paper wins.
- The Blueprint's design is **three chapters, not two**. `src/blueprint/design/` ("Design") is the host-independent design: what a kernelet is, what it may assume, and what any host must provide it. `src/blueprint/asterinas-mode/` ("Design for Asterinas") and `src/blueprint/linux-mode/` ("Design for Linux") are how each host meets it, in the same page order. A host chapter **may link into Design for anything host-independent and must not restate it**; where Design has a page on a subject, the host page carries only its own host's part and says so in its opening line. Terms are defined once, in Design or the Overview's terminology. The test, stated on `src/blueprint/design/index.md`: *does a sentence name a facility of a particular host?* Then it belongs to that host's chapter; otherwise it belongs in Design. Design is authoritative where a host chapter disagrees with it.
- The Linux chapter gives its Linux source references as links to elixir.bootlin.com pinned at v6.12, and defines the terms that are Linux's own (root carrier, gate, kernelet stack, model and cache, the mirror, seat, eviction). The Asterinas chapter cites the Asterinas tree at commit `ab9a4cfdc`.
- Design is being assembled from the two host chapters page by page, so until a subject has a page there the host chapters still carry it in full. The plan and its status are in `ALIGNMENT_PLAN.md` at the repository root.
- Every chapter directory has an `index.md`. Where the original section had a preamble, that is the page; otherwise it is a short summary plus the list of subsections (mdBook does not generate a child list on the parent page, so the list is written by hand and must be kept in step with `SUMMARY.md`).
- Files are named for their content, never numbered. Numbers live only in `SUMMARY.md` order.
- Unwritten material is a `> **To be written.** …` blockquote stating scope and sources, never invented content.

## Cross-references and links

- Section references are links whose text is a `§` number: `[§4.1.3](../asterinas-mode/process/switch-policy.md)`. **Never type the number by hand.** After any change to `SUMMARY.md`, or to the `##` headings of a chapter that has no child pages, run `make renumber`; it re-derives every `§` text from the table of contents.
- A term used on a page but defined on another may be linked on its first use, with the term itself as the link text: `[owner array](../memory/frames.md)`. That is the preferred way to make a page self-contained; do not rewrite sentences for it.
- Links go to files (`…/switch-policy.md`), or to explicit `{#id}` heading anchors. Give a heading an explicit id whenever something links to it; do not rely on mdBook's slug rules.
- Index pages are `index.md`, never `README.md`: mdBook 0.5 renders a `README.md` chapter as `index.html` but rewrites links to a `README.html` that does not exist. The checker rejects such links.

## Theme

`assets/kernelets.css` (via `additional-css`) rebrands mdBook's `navy` theme with the figure palette and `assets/mermaid-init.js` gives Mermaid a matching `base` theme for dark modes; both are described in the header comment of `assets/kernelets.css`. Light themes are left as mdBook ships them. When adding a component style, scope it under `.navy` and use the `--k-*` variables; do not override `theme/` templates.

## Diagrams and figures

- Diagrams in the design chapters are Mermaid in ```` ```mermaid ```` fences, rendered by `mdbook-mermaid`. Node ids must not be words the checker maps (the host-kernel node is `hk`, not `host`).
- The two figures on the Executive Summary are inline SVG inside `<figure class="fwd-fig">`, styled by `assets/fig.css`. Inside an HTML block there must be **no blank line**, or CommonMark resumes Markdown parsing mid-block. Prefix SVG gradient ids (`fwd-…`) so the single-page `print.html` has no id collisions.

## Workflow

```sh
# from the repository root; each target is one command, listed in the Makefile
make renumber   # after editing SUMMARY.md or ## headings in single-file chapters
make check      # must print "check: OK"
make build      # must exit 0; the mdbook-mermaid 0.5.0-vs-0.5.2 version warning is known and harmless
make            # check, then build
```

`check` verifies that every internal link resolves to an existing file and anchor, that no bare `§` is left unlinked (except inside code fences), that no chapter file is orphaned or missing, and that no link targets a `README.md`.

To confirm that Mermaid or an SVG figure actually renders, run `make render PAGE=<path under book/>` (for example `PAGE=design/process/two-windows.html`). It builds the book, screenshots the page to `.cache/render/`, and fails if any Mermaid block on the page did not render; look at the screenshot. The script finds a headless Chrome or fetches one into the gitignored `.cache/` directory, and installs nothing outside the repository. Pass `OUT=<file.png>` to put the screenshot elsewhere, such as the scratchpad.

**Adding a section**

1. Write the file under the right directory, starting with `# Title`. Refer to other sections as `[§](path.md)` with any or no number in the text.
2. Add one line to `src/SUMMARY.md` at the right place and depth; if it is a subsection, add it to the parent's `index.md` list too.
3. Run `make renumber`, then `make`.

**Changing existing text.** Prefer adding a link or a new paragraph. If an existing sentence must change, say which sentence and why in the commit message, or ask; the owner reviews wording closely.

**Before finishing a pass.** The commit message says what changed, what was verified (quote the actual command output) and what the next pass should attack. When a decision in this file is made or reversed, edit this file in the same commit.

## Do not touch

- The built `book/` directory: generated, gitignored.