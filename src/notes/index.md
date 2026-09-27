# The Notes

Working material behind the Blueprint and the Paper. Nothing here is normative: where a note disagrees with the Blueprint, the Blueprint wins.

- [OSTD API inventory](ostd-api-inventory.md): the public API of OSTD as read from the Asterinas tree, with the commit, and how often the kernel uses each item. The raw material for the design chapter's taxonomy.
- [Design register](design-register.md): every decision the design chapters make where the fixed decisions left a choice, with the alternative rejected and the reason, and every assumption it rests on.
- [I/O microbenchmarks](io-microbenchmarks.md): the unit costs behind the zero-copy I/O argument of [Design for Asterinas](../blueprint/asterinas-mode/zero-copy-io.md) (copies hot and cold, unmaps with TLB shootdown, a thread wakeup), measured on this book's host, with the script and raw output.
