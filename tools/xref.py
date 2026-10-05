#!/usr/bin/env python3
"""Cross-reference tool for the Asterinas Kernelets book.

    python3 tools/xref.py convert    # turn bare "§N" / "§N.M" tokens into links (one-off, after a split)
    python3 tools/xref.py renumber   # rewrite the text of every [§…](path) link from SUMMARY.md order
    python3 tools/xref.py check      # verify every internal link resolves, no bare "§", no orphan or missing chapter

Section numbers are never typed by hand. They are derived from the order of chapters in
`src/SUMMARY.md` (numbered list items only; the prefix chapter and part headers do not count)
and, for anchors inside a single-file chapter, from the order of its `##` headings.
So after inserting a chapter or a `##` heading, run `renumber` and every "§" link text is right again.

Run from the book's root directory (or pass `--root <book dir>`).
"""

import argparse
import os
import re
import sys

LINK_RE = re.compile(r"\[([^\[\]]*)\]\(([^)\s]+)\)")  # link text may not contain brackets, so `[a, b)` … `[§1](x)` is not one match
SECTION_LINK_RE = re.compile(r"\[§[0-9]+(?:\.[0-9]+)*\]\(([^)\s]+)\)")
BARE_REF_RE = re.compile(r"§([0-9]+(?:\.[0-9]+)*)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*(?:\{#([A-Za-z0-9_-]+)\})?\s*$")
SUMMARY_ITEM_RE = re.compile(r"^(\s*)[-*]\s+\[([^\]]*)\]\(([^)]+)\)\s*$")
SUMMARY_PREFIX_RE = re.compile(r"^\[([^\]]*)\]\(([^)]+)\)\s*$")


def slugify(text):
    """Approximate mdBook's heading id: strip code ticks, lowercase, spaces to dashes,
    keep alphanumerics, '-' and '_'."""
    text = re.sub(r"<[^>]*>", "", text)
    text = text.replace("`", "")
    out = []
    for ch in text.lower():
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        elif ch.isspace():
            out.append("-")
    return "".join(out)


def iter_lines_outside_fences(lines):
    """Yield (index, line, in_fence)."""
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            yield i, line, True
            continue
        yield i, line, in_fence


def headings_of(path):
    """Return the list of (level, title, id) for headings in a file, outside code fences."""
    result = []
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    for _, line, in_fence in iter_lines_outside_fences(lines):
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2)
            hid = m.group(3) or slugify(title)
            result.append((level, title, hid))
    return result


def parse_summary(src):
    """Return ordered list of (number_or_None, relpath, title) from SUMMARY.md.

    Numbering follows mdBook: numbered list items get 1, 1.1, 1.1.1 …; prefix/suffix
    chapters (bare links outside the list) are unnumbered; part headers are skipped.
    """
    entries = []
    counters = []
    with open(os.path.join(src, "SUMMARY.md"), encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            m = SUMMARY_ITEM_RE.match(line)
            if m:
                indent, title, path = m.groups()
                depth = len(indent.replace("\t", "    ")) // 2  # two spaces per level (four also works)
                depth = min(depth, len(counters))  # a jump of more than one level counts as one
                if depth == len(counters):
                    counters.append(0)
                else:
                    counters = counters[: depth + 1]
                counters[depth] += 1
                number = ".".join(str(c) for c in counters)
                entries.append((number, path, title))
                continue
            m = SUMMARY_PREFIX_RE.match(line.strip())
            if m:
                title, path = m.groups()
                entries.append((None, path, title))
    return entries


def build_number_map(src):
    """Map section number -> (relpath, anchor_or_None), including '##' headings of
    numbered chapters as N.M anchors when the chapter has no numbered children."""
    entries = parse_summary(src)
    numbered = [(n, p, t) for n, p, t in entries if n]
    has_children = set()
    for n, _, _ in numbered:
        if "." in n:
            has_children.add(n.rsplit(".", 1)[0])
    num_to_target = {}
    path_to_num = {}
    for n, p, _ in numbered:
        num_to_target[n] = (p, None)
        path_to_num[p] = n
        if n not in has_children:
            subs = [h for h in headings_of(os.path.join(src, p)) if h[0] == 2]
            for i, (_, _, hid) in enumerate(subs, 1):
                num_to_target["%s.%d" % (n, i)] = (p, hid)
    return entries, num_to_target, path_to_num


def all_md_files(src):
    out = []
    for dirpath, _, files in os.walk(src):
        for fn in files:
            if fn.endswith(".md") and fn != "SUMMARY.md":
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def rel_link(from_file, to_relpath, anchor, src):
    target = os.path.join(src, to_relpath)
    rel = os.path.relpath(target, os.path.dirname(from_file))
    if anchor:
        rel += "#" + anchor
    return rel


def cmd_convert(src):
    _, num_to_target, _ = build_number_map(src)
    total = 0
    for path in all_md_files(src):
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
        changed = 0
        for i, line, in_fence in iter_lines_outside_fences(lines):
            if in_fence:
                continue

            def repl(m):
                num = m.group(1)
                # already a link text? the character before '§' would be '['
                start = m.start()
                if start > 0 and line[start - 1] == "[":
                    return m.group(0)
                if num not in num_to_target:
                    print("WARN %s: no target for §%s" % (os.path.relpath(path, src), num))
                    return m.group(0)
                to_path, anchor = num_to_target[num]
                return "[§%s](%s)" % (num, rel_link(path, to_path, anchor, src))

            new = BARE_REF_RE.sub(repl, line)
            if new != line:
                changed += new.count("](") - line.count("](")
                lines[i] = new
        if changed:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            total += changed
    print("convert: %d references linked" % total)


def resolve_target(from_file, href, src):
    """Return (abs_path, anchor) for an internal link, or None for external links."""
    if href.startswith(("http://", "https://", "mailto:")):
        return None
    path, _, anchor = href.partition("#")
    if path == "":
        return from_file, anchor or None
    return os.path.normpath(os.path.join(os.path.dirname(from_file), path)), anchor or None


def cmd_renumber(src):
    _, num_to_target, path_to_num = build_number_map(src)
    target_to_num = {}
    for n, (p, a) in num_to_target.items():
        target_to_num[(os.path.normpath(os.path.join(src, p)), a)] = n
    total = 0
    for path in all_md_files(src):
        with open(path, encoding="utf-8") as f:
            text = f.read()

        def repl(m):
            nonlocal total
            href = m.group(1)
            resolved = resolve_target(path, href, src)
            if resolved is None:
                return m.group(0)
            key = resolved
            if key not in target_to_num:
                # a link to a page that is not a numbered target (e.g. the title page): leave it
                return m.group(0)
            new = "[§%s](%s)" % (target_to_num[key], href)
            if new != m.group(0):
                total += 1
            return new

        new_text = SECTION_LINK_RE.sub(repl, text)
        if new_text != text:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_text)
    print("renumber: %d link texts updated" % total)


def cmd_check(src):
    ok = True
    entries = parse_summary(src)
    summary_paths = {os.path.normpath(os.path.join(src, p)) for _, p, _ in entries}
    for p in sorted(summary_paths):
        if not os.path.exists(p):
            print("MISSING chapter file: %s" % os.path.relpath(p, src))
            ok = False
    # Index pages are README.md, which GitHub shows when a directory is opened; that is safe only
    # while book.toml turns off mdBook's default `index` preprocessor (see the comment there).
    with open(os.path.join(os.path.dirname(src), "book.toml"), encoding="utf-8") as f:
        readme_links_work = re.search(r"^\s*use-default-preprocessors\s*=\s*false\b", f.read(), re.M) is not None
    for path in all_md_files(src):
        if os.path.basename(path) == "index.md":
            print("INDEX %s (name a directory's index page README.md)" % os.path.relpath(path, src))
            ok = False
        if os.path.normpath(path) not in summary_paths:
            print("ORPHAN (not in SUMMARY.md): %s" % os.path.relpath(path, src))
            ok = False
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
        for i, line, in_fence in iter_lines_outside_fences(lines):
            if in_fence:
                continue
            for m in LINK_RE.finditer(line):
                href = m.group(2)
                resolved = resolve_target(path, href, src)
                if resolved is None:
                    continue
                target, anchor = resolved
                rel_from = os.path.relpath(path, src)
                rel_to = os.path.relpath(target, src)
                if rel_from.startswith("paper" + os.sep) and not rel_to.startswith("paper" + os.sep):
                    # The Paper is self-contained: it cites external work and refers to nothing else in the book.
                    print("PAPER %s:%d -> %s (pages under paper/ may link only within paper/)"
                          % (rel_from, i + 1, href))
                    ok = False
                if os.path.basename(target) == "README.md" and not readme_links_work:
                    # With mdBook's default `index` preprocessor on, a README.md chapter is rendered as
                    # index.html but a link to it is rewritten as README.html, which does not exist.
                    print("README %s:%d -> %s (book.toml must keep use-default-preprocessors = false)"
                          % (os.path.relpath(path, src), i + 1, href))
                    ok = False
                if not os.path.isfile(target):
                    print("BROKEN %s:%d -> %s (no such file)" % (os.path.relpath(path, src), i + 1, href))
                    ok = False
                    continue
                if anchor:
                    ids = {h[2] for h in headings_of(target)}
                    if anchor not in ids:
                        print("BROKEN %s:%d -> %s (no heading with id '%s')"
                              % (os.path.relpath(path, src), i + 1, href, anchor))
                        ok = False
            for m in BARE_REF_RE.finditer(line):
                if m.start() == 0 or line[m.start() - 1] != "[":
                    print("UNLINKED %s:%d: §%s" % (os.path.relpath(path, src), i + 1, m.group(1)))
                    ok = False
    print("check: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["convert", "renumber", "check"])
    ap.add_argument("--root", default=None, help="book directory (default: the directory containing tools/)")
    args = ap.parse_args()
    root = args.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(root, "src")
    if args.command == "convert":
        cmd_convert(src)
    elif args.command == "renumber":
        cmd_renumber(src)
    else:
        sys.exit(cmd_check(src))


if __name__ == "__main__":
    main()
