"""Generate the public docs manifest baked into the monolith-public frontend.

The public docs surface contains a fixed set of projects and, for each project,
up to four current-state documents: README, architecture, STPA, and threat
model. Only the exact project/document pairs listed below are published. The
generator uses ``git ls-files`` so untracked files, nested READMEs, ADRs, and
other repository documentation never enter the manifest.

The committed JSON manifest stores full bodies inline at
``projects/monolith/frontend/src/lib/public/docs/docs-manifest.json``. The
SvelteKit ``/docs`` route imports it server-side and sends only rendered HTML
and small navigation structures to the browser.

Regeneration is automatic: CI's Format stage runs this generator on every push
and auto-commits manifest changes on PR branches. To regenerate locally, run
this script with any python3 (or
``bazel run //projects/monolith:gen_docs_manifest``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PUBLIC_PROJECTS = (
    ("embervm", "projects/embervm"),
    ("monolith", "projects/monolith"),
    ("mcp", "projects/mcp"),
    ("sextant", "projects/sextant"),
    ("model-bench", "projects/model-bench"),
    ("oci-model-cache", "projects/operators/oci-model-cache"),
    ("platform", "projects/platform"),
)

DOC_KINDS = (
    ("readme", "README.md", "Readme"),
    ("architecture", "ARCHITECTURE.md", "Architecture"),
    ("stpa", "STPA.md", "STPA"),
    ("threat-model", "THREAT-MODEL.md", "Threat model"),
)

MANIFEST_REL = "projects/monolith/frontend/src/lib/public/docs/docs-manifest.json"

_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_DOC_BY_PATH = {
    f"{directory}/{filename}": (project, kind)
    for project, directory in PUBLIC_PROJECTS
    for kind, filename, _label in DOC_KINDS
}
_PROJECT_ORDER = {
    project: order for order, (project, _directory) in enumerate(PUBLIC_PROJECTS)
}
_KIND_ORDER = {kind: order for order, (kind, _filename, _label) in enumerate(DOC_KINDS)}


def _basename(rel_path: str) -> str:
    return rel_path.rsplit("/", 1)[-1]


def derive_title(content: str, rel_path: str) -> str:
    """First H1, falling back to the filename (without the .md suffix).

    A README with no H1 falls back to its parent directory name instead of
    the literal "README", since "README" alone carries no information about
    which project it is.
    """
    match = _H1.search(content)
    if match:
        return match.group(1).strip()
    name = _basename(rel_path)
    if name == "README.md":
        return rel_path.rsplit("/", 2)[-2]
    return name[:-3] if name.endswith(".md") else name


def _should_index(rel_path: str) -> bool:
    """Return whether ``rel_path`` is an exact public project/doc pair."""
    return rel_path in _DOC_BY_PATH


def make_slug(project: str, kind: str) -> str:
    """Return the URL path below ``/docs`` for a project document kind."""
    return project if kind == "readme" else f"{project}/{kind}"


def iter_doc_paths(root: Path) -> list[str]:
    """Return tracked public doc paths in project and document-kind order."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    tracked = {path for path in result.stdout.split("\0") if path}
    return [path for path in _DOC_BY_PATH if path in tracked]


def build_manifest(root: Path, paths: list[str]) -> list[dict]:
    """Build manifest entries in stable project then document-kind order."""
    public_paths = [path for path in paths if _should_index(path)]
    ordered = sorted(
        public_paths,
        key=lambda path: (
            _PROJECT_ORDER[_DOC_BY_PATH[path][0]],
            _KIND_ORDER[_DOC_BY_PATH[path][1]],
        ),
    )
    entries: list[dict] = []
    for rel_path in ordered:
        source = root / rel_path
        if not source.is_file():
            continue
        # Strip NUL bytes so the body is JSON-safe and storable.
        content = source.read_text(encoding="utf-8").replace("\x00", "")
        project, kind = _DOC_BY_PATH[rel_path]
        entries.append(
            {
                "path": rel_path,
                "slug": make_slug(project, kind),
                "project": project,
                "kind": kind,
                "title": derive_title(content, rel_path),
                "order": len(entries),
                "content": content,
            }
        )
    return entries


def main() -> int:
    root = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY") or os.getcwd())
    out = root / MANIFEST_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    entries = build_manifest(root, iter_doc_paths(root))
    out.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(entries)} docs to {MANIFEST_REL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
