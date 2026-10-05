# Asterinas Kernelets

The design book for **Asterinas Kernelets**: many light-weight, safe-Rust kernel instances running side by side inside one Asterinas host kernel, one per sandbox, mediated by a component inside the host kernel called the *endovisor*. No hypervisor, no shared kernel state.

The book is an [mdBook](https://rust-lang.github.io/mdBook/) and serves two audiences at once:

- **Asterinas developers**, for whom it is a specification: architecture, the design layer by layer, the OSTD changes, the enforcement checks and the staged plan, written to be precise enough for a coding agent to implement from.
- **Academic readers**, for whom it is the long form of a paper: motivation, the mechanisms and their costs, an honest list of where the design loses and what has not been verified, and the prior art it is compared with.

Start with [`src/README.md`](src/README.md), which pitches the idea to each audience.

## Prerequisites

- **mdBook**: The documentation is rendered with mdBook, which can be installed with Cargo:

  ```sh
  cargo install --locked mdbook@0.5.2
  ```

- **Python 3** (3.8 or later, standard library only) for Python-based tools under [`tools/`](tools/).
- **GNU Make**: the `Makefile` wraps the commands to build and test the documentation.
- **A headless Chrome**, optional, only for `make render`, which screenshots a built page and checks that its Mermaid diagrams rendered. `tools/render.sh` uses `$CHROME` if set, else a Chrome under `.cache/puppeteer` or `~/.cache/puppeteer`, else `chromium` or `google-chrome` on the path. If it finds none, it fetches one into `.cache/puppeteer` (gitignored) with `npx @puppeteer/browsers`, which needs **Node.js**; `make install_browser` does that fetch ahead of time.

## Build

```sh
make                  # check and build the book into `book/`
make serve            # live preview at port 3000
make clean            # remove the built HTML
```

## Publishing

Every push to the `main` branch deploys the Book to GitHub Pages at <https://tatetian.github.io/kernelet-book/>.
