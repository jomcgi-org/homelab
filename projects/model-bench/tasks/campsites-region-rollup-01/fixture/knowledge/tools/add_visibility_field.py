"""Phase-1 mechanical injection of `visibility:` into existing note frontmatter.

Idempotent. No LLM calls. No DB writes. Walks the configured vault dirs,
inserts `visibility:` (empty value) into every .md file that lacks the
key. The resulting frontmatter is parsed by the reconciler on its next
pass and persists null in the DB column — which the serving layer
treats as private.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from knowledge import frontmatter

logger = logging.getLogger("monolith.knowledge.tools.add_visibility_field")

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


@dataclass
class Stats:
    added: int = 0
    already_set: int = 0
    parse_skipped: int = 0


def _insert_visibility_line(block: str) -> str:
    """Insert `visibility:` after `id:` (or at the top if no id)."""
    lines = block.splitlines()
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("id:"):
            insert_at = i + 1
            break
    lines.insert(insert_at, "visibility:")
    return "\n".join(lines)


def _process_file(path: Path) -> str | None:
    """Return the new file content, or None if no change is needed."""
    raw = path.read_text()
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return "PARSE_SKIP"

    block = match.group(1)
    try:
        meta, _ = frontmatter.parse(raw)
    except frontmatter.FrontmatterError as exc:
        logger.warning("parse failure for %s: %s — skipping", path, exc)
        return "PARSE_SKIP"

    if "visibility" in {line.split(":", 1)[0].strip() for line in block.splitlines()}:
        return None

    new_block = _insert_visibility_line(block)
    return raw[: match.start()] + f"---\n{new_block}\n---\n" + raw[match.end() :]


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def run(*, vault_root: Path, dirs: list[str]) -> Stats:
    stats = Stats()
    for d in dirs:
        for md in (vault_root / d).rglob("*.md"):
            outcome = _process_file(md)
            if outcome == "PARSE_SKIP":
                stats.parse_skipped += 1
                continue
            if outcome is None:
                stats.already_set += 1
                continue
            _atomic_write(md, outcome)
            stats.added += 1
    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--dirs", nargs="+", default=["_processed"])
    args = parser.parse_args()
    stats = run(vault_root=args.vault_root, dirs=args.dirs)
    print(
        f"added={stats.added} already_set={stats.already_set} "
        f"parse_skipped={stats.parse_skipped}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
