#!/usr/bin/env python3
"""Check every relative link in the oom-inference Markdown files.

    python3 projects/oom-inference/docs/check_links.py

Finds `[text](target)` and `![alt](target)` outside code fences in every `.md`
file under the project (build output excluded), and checks that each relative
target exists and that each `#anchor` matches a heading in the target file
(GitHub's heading slugs). External links (http, mailto) are skipped. Exits 1
and lists the broken links if any.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"target", ".venv", "node_modules", "results"}
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*(```|~~~)")


def markdown_files() -> list[Path]:
    return sorted(
        p
        for p in ROOT.rglob("*.md")
        if not any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts)
    )


def prose_lines(text: str) -> list[str]:
    """Lines outside fenced code blocks."""
    out, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        out.append(line)
    return out


def slug(heading: str) -> str:
    s = re.sub(r"`|\*", "", heading.strip()).lower()
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    result = set()
    for line in prose_lines(path.read_text(encoding="utf-8")):
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        base = slug(m.group(1))
        n = seen.get(base, 0)
        seen[base] = n + 1
        result.add(base if n == 0 else f"{base}-{n}")
    return result


def main() -> int:
    broken = []
    checked = 0
    for md in markdown_files():
        for line in prose_lines(md.read_text(encoding="utf-8")):
            for target in LINK.findall(line):
                if re.match(r"^[a-z]+:", target):
                    continue
                checked += 1
                path_part, _, anchor = target.partition("#")
                dest = (md.parent / path_part).resolve() if path_part else md
                where = f"{md.relative_to(ROOT)}: {target}"
                if not dest.exists():
                    broken.append(f"{where} (missing file)")
                elif anchor and dest.suffix == ".md" and anchor not in anchors(dest):
                    broken.append(f"{where} (missing anchor)")
    for b in broken:
        print(b)
    print(f"{checked} relative links checked, {len(broken)} broken")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
