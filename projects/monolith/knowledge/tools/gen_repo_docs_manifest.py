"""Generate the repo-docs manifest baked into the monolith image.

Writes one NDJSON line per indexed markdown file, sorted by repo-relative path.
The manifest is a build output, not a committed file (#6446): the
``//projects/monolith:repo_docs_manifest`` genrule runs this script with
``--out`` and the doc files from ``//projects/monolith:repo_docs_srcs`` as
arguments, and the image ships the result as its own layer.

Without path arguments the script lists the repo's tracked markdown via
``git ls-files`` (deterministic, never picks up untracked files or build
artifacts under symlinked bazel-out/ dirs) and writes the gitignored local copy
at projects/monolith/knowledge/repo_docs_manifest.ndjson, which a locally run
backend reads beside ``knowledge/repo_docs.py``. The same selection, applied to
``git ls-files``, is what bazel/images/validate-generate-scripts.sh compares
against the genrule's inputs so a doc outside every ``repo_docs`` filegroup
fails CI instead of silently leaving the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MANIFEST_REL = "projects/monolith/knowledge/repo_docs_manifest.ndjson"

# We index *.md under these top-level prefixes, plus any CLAUDE.md anywhere.
# bazel/ carries the build and CI architecture document plus the per-ruleset
# READMEs (helm, oci, image), which are the only place that
# knowledge lives; without it the KG could not answer a CI question.
_INCLUDE_DIRS = ("bazel/", "docs/", "projects/")
_INCLUDE_NAMES = ("AGENTS.md", "CLAUDE.md")  # indexed anywhere (root + nested)

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
    # Vendored upstream trees (their LICENSE.md is not our documentation) and
    # test fixtures (a semgrep fixture deliberately contains the stale paths
    # its rule flags). Both appear under bazel/; neither exists under docs/
    # or projects/ today, so this drops nothing already indexed.
    "/third_party/",
    "/fixtures/",
)


_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def derive_title(content: str, rel_path: str) -> str:
    m = _H1.search(content)
    return m.group(1).strip() if m else rel_path


# Trees in .bazelignore: no Bazel filegroup can carry their docs into the
# manifest genrules, so the coverage check could never pass for them. Remove an
# entry when its project moves to Bazel and gains a ``repo_docs`` filegroup.
_EXCLUDE_PREFIXES = ("projects/oom-inference/",)


def _excluded(rel_path: str) -> bool:
    p = f"/{rel_path}/"
    return (
        any(seg in p for seg in _EXCLUDE_SEGMENTS)
        or rel_path.startswith(_EXCLUDE_PREFIXES)
        or rel_path == MANIFEST_REL
    )


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


def select_doc_paths(paths: list[str]) -> list[str]:
    """Indexed paths from an explicit candidate list, sorted and de-duplicated."""
    return sorted({p for p in paths if _should_index(p)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        help="output path (default: the local copy under the repo root)",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="repo-relative candidate docs (default: git ls-files)",
    )
    args = parser.parse_args(argv)
    root = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY") or os.getcwd())
    paths = select_doc_paths(args.paths) if args.paths else iter_doc_paths(root)
    out = Path(args.out) if args.out else root / MANIFEST_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = build_manifest_lines(root, paths)
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"wrote {len(lines)} docs to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
