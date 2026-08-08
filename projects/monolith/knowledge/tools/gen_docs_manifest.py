"""Generate the public docs manifest baked into the monolith-public frontend.

Globs the public-allowlisted repository docs (project READMEs, ``projects/**/
README.md``, plus the ADR tree ``docs/decisions/**/*.md``) via ``git ls-files``
and writes a committed JSON manifest with full bodies inline to
``projects/monolith/frontend/src/lib/public/docs/docs-manifest.json``. The
SvelteKit ``/docs`` route imports that manifest SERVER-SIDE (never in a client
bundle) and renders each doc with ``marked``.

Using git (not a filesystem walk) makes the output deterministic across
platforms and Python versions and never picks up untracked files or build
artifacts under symlinked ``bazel-out/`` dirs.

Security (ADR docs/001): the public docs surface is built from an EXPLICIT
allowlist, never the RAG ingest (which indexes internal docs). Both tiers are
self-maintaining: ADRs are append-only decisions, and READMEs are colocated
with the code they describe so they get updated by proximity pressure. Hand-
written top-level ``docs/*.md`` reference docs are no longer published (they
rot far from the code they describe); they remain internal-only. Excluded:
``docs/plans/**``, vendored README subtrees (a prefix blocklist for
third-party charts we vendor but did not author), and a per-file blocklist for any README that
should stay off the public surface. Be conservative: if unsure whether a doc is
public, it stays off the allowlist.

Regeneration is automatic: the "Format check" CI action (buildbuddy.yaml) runs
this generator on every push and auto-commits any change to the manifest on PR
branches (as ci-format-bot), like any other formatting fix, so a doc edit never
needs a manual regen. To regenerate locally: run this script with any python3
(or ``bazel run //projects/monolith:gen_docs_manifest``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

MANIFEST_REL = "projects/monolith/frontend/src/lib/public/docs/docs-manifest.json"

DOCS_PREFIX = "docs/"
DECISIONS_PREFIX = "docs/decisions/"
PROJECTS_PREFIX = "projects/"
README_SUFFIX = "/README.md"

# Vendored subtree prefix blocklist: third-party charts/code we vendor in but
# did not author, so their READMEs should not appear on the public docs site.
# Add future vendored trees here.
_VENDORED_PREFIXES: tuple[str, ...] = ()

# Per-file blocklist: individual README paths that must NOT appear on the
# public docs site even though they match the allowlist glob. These are
# build glue or internal tooling notes, not project documentation.
_BLOCKLIST: frozenset[str] = frozenset(
    {
        "projects/shared/README.md",
        # March-era standalone GCP app, slated for decommission; its runbook
        # would read as the live ingest path to an outside reader.
        "projects/monolith/frontend/visual/README.md",
        "projects/platform/signoz-addons/operator/crds/README.md",
    }
)

_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_NUM = re.compile(r"^(\d+)")


def _basename(rel_path: str) -> str:
    return rel_path.rsplit("/", 1)[-1]


def derive_title(content: str, rel_path: str) -> str:
    """First H1, falling back to the filename (without the .md suffix).

    A README with no H1 falls back to its parent directory name instead of
    the literal "README", since "README" alone carries no information about
    which project it is.
    """
    m = _H1.search(content)
    if m:
        return m.group(1).strip()
    name = _basename(rel_path)
    if name == "README.md":
        return rel_path.rsplit("/", 2)[-2]
    return name[:-3] if name.endswith(".md") else name


def _should_index(rel_path: str) -> bool:
    """True if a repo-relative path belongs on the public docs site.

    Allowlist (conservative):
      - docs/decisions/**/*.md   (the ADR tree, incl. index.md, any depth)
      - projects/**/README.md    (project READMEs, any depth), excluding
                                  vendored subtrees in _VENDORED_PREFIXES
    Everything else (docs/plans/**, hand-written docs/*.md reference docs,
    non-README project files, vendored README subtrees, the per-file
    blocklist, the manifest itself) is excluded.
    """
    if not rel_path.endswith(".md"):
        return False
    if rel_path in _BLOCKLIST or rel_path == MANIFEST_REL:
        return False
    if rel_path.startswith(DECISIONS_PREFIX):
        return True
    if rel_path.startswith(PROJECTS_PREFIX) and rel_path.endswith(README_SUFFIX):
        return not any(rel_path.startswith(p) for p in _VENDORED_PREFIXES)
    return False


def make_slug(rel_path: str) -> str:
    """URL path under /docs/ for a repo doc path.

    docs/decisions/agents/001-x.md -> decisions/agents/001-x
    docs/decisions/index.md        -> decisions            (index collapses)
    projects/firecracker/README.md -> projects/firecracker  (README collapses)
    """
    s = rel_path[len(DOCS_PREFIX) :] if rel_path.startswith(DOCS_PREFIX) else rel_path
    if s.endswith(".md"):
        s = s[:-3]
    if s.endswith("/index"):
        s = s[: -len("/index")]
    elif s.endswith("/README"):
        s = s[: -len("/README")]
    return s


def section_for(rel_path: str) -> str:
    return "Decisions" if rel_path.startswith(DECISIONS_PREFIX) else "Projects"


def category_for(rel_path: str) -> str:
    """ADR category (the dir segment under docs/decisions/); "" otherwise."""
    if not rel_path.startswith(DECISIONS_PREFIX):
        return ""
    rest = rel_path[len(DECISIONS_PREFIX) :]
    if "/" not in rest:
        return ""  # docs/decisions/index.md
    return rest.split("/", 1)[0]


def _numeric_prefix(rel_path: str) -> int:
    m = _NUM.match(_basename(rel_path))
    return int(m.group(1)) if m else 0


def _sort_key(rel_path: str):
    """Deterministic sidebar ordering.

    Projects (READMEs) first, alphabetical by path, then the ADR tree: the
    decisions index, then each category alphabetically with its ADRs by
    numeric prefix.
    """
    if section_for(rel_path) == "Projects":
        return (0, "", 0, rel_path)
    cat = category_for(rel_path)
    if cat == "":
        return (1, "", -1, rel_path)  # decisions/index.md sorts before categories
    return (1, cat, _numeric_prefix(rel_path), rel_path)


def iter_doc_paths(root: Path) -> list[str]:
    """Tracked allowlisted doc paths. Uses ``git ls-files`` so the set is exactly
    the committed files (deterministic, no symlinked build artifacts)."""
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


def build_manifest(root: Path, paths: list[str]) -> list[dict]:
    """Manifest entries in stable sidebar order. ``order`` is the entry index."""
    ordered = sorted(paths, key=_sort_key)
    entries: list[dict] = []
    for rel in ordered:
        if not (root / rel).is_file():
            continue
        # Strip NUL bytes (0x00) so the body is JSON-safe and storable; the docs
        # are first-party markdown, but mirror the repo_docs generator's guard.
        content = (root / rel).read_text(encoding="utf-8").replace("\x00", "")
        entries.append(
            {
                "path": rel,
                "slug": make_slug(rel),
                "title": derive_title(content, rel),
                "section": section_for(rel),
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
    # indent=2 keeps the committed manifest diff-friendly; insertion order of
    # each entry's keys is fixed above so the serialization is stable.
    out.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(entries)} docs to {MANIFEST_REL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
