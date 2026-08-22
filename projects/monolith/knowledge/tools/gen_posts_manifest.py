"""Generate the frontmatter-gated manifest for the public /posts route.

Tracked Markdown files under ``docs/posts`` are considered, except README.md.
Only files that declare the boolean ``public: true`` are parsed and validated
for publication. The committed manifest contains the metadata and post bodies
used by the SvelteKit server routes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

POSTS_PREFIX = "docs/posts/"
MANIFEST_REL = "projects/monolith/frontend/src/lib/public/posts/posts-manifest.json"

_FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)\.md$")
_KEY_VALUE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):[ \t]*(.*?)[ \t]*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PUBLIC_TRUE = re.compile(r"^public:[ \t]*true[ \t]*$")


def _should_index(rel_path: str) -> bool:
    return (
        rel_path.startswith(POSTS_PREFIX)
        and rel_path.endswith(".md")
        and rel_path != f"{POSTS_PREFIX}README.md"
        and "/" not in rel_path[len(POSTS_PREFIX) :]
    )


def make_slug(rel_path: str) -> str:
    """Strip the date prefix and Markdown suffix from a post filename."""
    match = _FILENAME.fullmatch(Path(rel_path).name)
    if not match:
        raise ValueError("post filename must match YYYY-MM-DD-<slug>.md")
    return match.group(2)


def _frontmatter_lines(source: str) -> tuple[list[str], str]:
    lines = source.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ValueError("missing opening frontmatter delimiter")
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == "---":
            return [line.rstrip("\r\n") for line in lines[1:index]], "".join(
                lines[index + 1 :]
            )
    raise ValueError("missing closing frontmatter delimiter")


def _declares_public_true(source: str) -> bool:
    """Find the publication gate without validating an unpublished draft."""
    lines = source.splitlines()
    if not lines or lines[0] != "---":
        return False
    for line in lines[1:]:
        if line == "---":
            break
        if _PUBLIC_TRUE.fullmatch(line):
            return True
    return False


def _parse_value(raw: str):
    if not raw:
        raise ValueError("frontmatter values may not be empty")
    if raw[0] in {"'", '"'}:
        quote = raw[0]
        if len(raw) < 2 or raw[-1] != quote or quote in raw[1:-1]:
            raise ValueError("frontmatter quotes must match")
        return raw[1:-1]
    if raw[-1] in {"'", '"'}:
        raise ValueError("frontmatter quotes must match")
    if raw == "true":
        return True
    if raw == "false":
        return False
    return raw


def parse_frontmatter(source: str) -> tuple[dict[str, object], str]:
    """Parse the deliberately small frontmatter subset accepted for posts."""
    lines, body = _frontmatter_lines(source)
    metadata: dict[str, object] = {}
    for line_number, line in enumerate(lines, start=2):
        match = _KEY_VALUE.fullmatch(line)
        if not match:
            raise ValueError(f"invalid frontmatter on line {line_number}")
        key, raw = match.groups()
        if key in metadata:
            raise ValueError(f"duplicate frontmatter key: {key}")
        metadata[key] = _parse_value(raw)
    return metadata, body


def _validate_public_post(rel_path: str, metadata: dict[str, object]) -> None:
    missing = {"title", "date", "summary", "public"} - metadata.keys()
    if missing:
        raise ValueError(f"missing required frontmatter: {', '.join(sorted(missing))}")
    for key in ("title", "date", "summary"):
        if not isinstance(metadata[key], str) or not metadata[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if metadata["public"] is not True:
        raise ValueError("public must be the boolean true")

    date_value = metadata["date"]
    if not _DATE.fullmatch(date_value):
        raise ValueError("date must use YYYY-MM-DD")
    try:
        date.fromisoformat(date_value)
    except ValueError as exc:
        raise ValueError("date must be a valid calendar date") from exc

    make_slug(rel_path)


def iter_post_paths(root: Path) -> list[str]:
    """Return tracked post paths, excluding the directory README."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "docs/posts/*.md"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return sorted(
        {path for path in result.stdout.split("\0") if path and _should_index(path)}
    )


def build_manifest(root: Path, paths: list[str]) -> list[dict]:
    entries: list[dict] = []
    for rel_path in paths:
        if not _should_index(rel_path) or not (root / rel_path).is_file():
            continue
        source = (root / rel_path).read_text(encoding="utf-8")
        if not _declares_public_true(source):
            continue
        try:
            metadata, body = parse_frontmatter(source)
            _validate_public_post(rel_path, metadata)
        except ValueError as exc:
            raise ValueError(f"{rel_path}: {exc}") from exc
        entries.append(
            {
                "path": rel_path,
                "slug": make_slug(rel_path),
                "title": metadata["title"],
                "date": metadata["date"],
                "summary": metadata["summary"],
                "content": body.replace("\x00", ""),
            }
        )
    return sorted(
        entries,
        key=lambda entry: (
            -date.fromisoformat(entry["date"]).toordinal(),
            entry["slug"],
        ),
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1:
        raise SystemExit("usage: gen_posts_manifest.py [repo-root]")
    root = Path(
        args[0] if args else os.environ.get("BUILD_WORKSPACE_DIRECTORY") or os.getcwd()
    )
    out = root / MANIFEST_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    entries = build_manifest(root, iter_post_paths(root))
    out.write_text(
        json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(entries)} posts to {MANIFEST_REL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
