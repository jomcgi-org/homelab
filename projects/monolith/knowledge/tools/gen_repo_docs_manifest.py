"""Generate the repo-docs manifest baked into the monolith image.

Walks the working tree (under BUILD_WORKSPACE_DIRECTORY when run via `bazel run`,
mirroring bazel/images/generate-home-cluster.sh) and writes one NDJSON line per
indexed markdown file, sorted by repo-relative path, to
projects/monolith/knowledge/repo_docs_manifest.ndjson.

Run manually when indexed docs change:
`bazel run //projects/monolith:gen_repo_docs_manifest`, then commit the diff. It
is intentionally NOT in the `//bazel/tools/format:format` multirun: that aggregate
drives only sh_binary generators, and a py_venv_binary does not resolve its main
module under rules_multirun (wiring auto-regen via a hermetic interpreter is a
follow-up). The private monolith's reconcile job reads the committed manifest from
the image, so a stale manifest only delays indexing of newly changed docs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterator

MANIFEST_REL = "projects/monolith/knowledge/repo_docs_manifest.ndjson"

# Top-level dir prefixes / filenames we index.
_INCLUDE_GLOBS = ("docs/**/*.md", "projects/**/*.md")
_INCLUDE_NAMES = ("CLAUDE.md",)  # matched by name anywhere (root + nested)

# Path segments that mark generated / vendored / irrelevant trees. All entries
# are slash-wrapped so they match whole path segments via the ``/{rel_path}/``
# trick in ``_excluded``, never bare substrings (so e.g. ``docs/vendoring.md`` or
# ``.github/*.md`` are not dropped by a ``vendor`` / ``.git`` substring match).
_EXCLUDE_SEGMENTS = (
    "/node_modules/",
    "/.git/",
    "/_trash/",
    "/build/",
    "/dist/",
    "/.svelte-kit/",
    "/vendor/",
)


_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def derive_title(content: str, rel_path: str) -> str:
    m = _H1.search(content)
    return m.group(1).strip() if m else rel_path


def _excluded(rel_path: str) -> bool:
    p = f"/{rel_path}/"
    return any(seg in p for seg in _EXCLUDE_SEGMENTS) or rel_path == MANIFEST_REL


def iter_doc_paths(root: Path) -> Iterator[str]:
    seen: set[str] = set()
    for pattern in _INCLUDE_GLOBS:
        for fp in root.glob(pattern):
            if not fp.is_file():
                continue
            rel = fp.relative_to(root).as_posix()
            if not _excluded(rel):
                seen.add(rel)
    # Named files anywhere (root + nested), which the project-globs may miss at
    # the repo root (e.g. CLAUDE.md).
    for name in _INCLUDE_NAMES:
        for fp in root.rglob(name):
            if not fp.is_file():
                continue
            rel = fp.relative_to(root).as_posix()
            if not _excluded(rel):
                seen.add(rel)
    yield from sorted(seen)


def build_manifest_lines(root: Path) -> list[str]:
    lines: list[str] = []
    for rel in iter_doc_paths(root):
        content = (root / rel).read_text(encoding="utf-8")
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        obj = {
            "path": rel,
            "sha256": sha,
            "title": derive_title(content, rel),
            "content": content,
        }
        # sort_keys for a stable, diff-friendly serialization.
        lines.append(json.dumps(obj, ensure_ascii=False, sort_keys=True))
    return lines


def main() -> int:
    root = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY") or os.getcwd())
    out = root / MANIFEST_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = build_manifest_lines(root)
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"wrote {len(lines)} docs to {MANIFEST_REL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
