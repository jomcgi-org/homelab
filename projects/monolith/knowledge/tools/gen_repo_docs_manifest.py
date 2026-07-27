"""Generate the repo-docs manifest baked into the monolith image.

Lists the repo's tracked markdown via ``git ls-files`` and writes one NDJSON line
per indexed file, sorted by repo-relative path, to
projects/monolith/knowledge/repo_docs_manifest.ndjson. Using git (not a filesystem
walk) makes the output deterministic across platforms and Python versions and
never picks up untracked files or build artifacts under symlinked bazel-out/ dirs.

Regeneration is automatic: the "Format check" CI action (buildbuddy.yaml) runs
this generator on every push and auto-commits any change to the manifest on PR
branches (as ci-format-bot), like any other formatting fix, so a doc edit never
needs a manual regen. To regenerate locally: run this script with any python3
(or `bazel run //projects/monolith:gen_repo_docs_manifest`). The private
monolith's reconcile job reads the committed manifest from the image.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MANIFEST_REL = "projects/monolith/knowledge/repo_docs_manifest.ndjson"

# We index *.md under these top-level prefixes, plus any CLAUDE.md anywhere.
_INCLUDE_DIRS = ("docs/", "projects/")
_INCLUDE_NAMES = ("CLAUDE.md",)  # indexed anywhere (root + nested)

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


def _should_index(rel_path: str) -> bool:
    """True if a repo-relative path belongs in the manifest (pure predicate)."""
    if _excluded(rel_path):
        return False
    if rel_path.rsplit("/", 1)[-1] in _INCLUDE_NAMES:
        return True
    return rel_path.endswith(".md") and rel_path.startswith(_INCLUDE_DIRS)


def iter_doc_paths(root: Path) -> list[str]:
    """Tracked markdown paths to index, sorted. Uses ``git ls-files`` so the set
    is exactly the committed files (deterministic, no symlinked build artifacts).
    """
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    paths = (p for p in result.stdout.split("\0") if p)
    return sorted({p for p in paths if _should_index(p)})


def build_manifest_lines(root: Path, paths: list[str]) -> list[str]:
    lines: list[str] = []
    for rel in sorted(paths):
        if not (root / rel).is_file():
            continue
        # Strip NUL bytes: a doc may contain 0x00, which Postgres TEXT columns
        # reject on insert (the reconcile would fail). Drop them so chunk_text is
        # always storable; the hash is taken over the cleaned content.
        content = (root / rel).read_text(encoding="utf-8").replace("\x00", "")
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
    lines = build_manifest_lines(root, iter_doc_paths(root))
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"wrote {len(lines)} docs to {MANIFEST_REL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
