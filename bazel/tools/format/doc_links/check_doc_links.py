"""Reject references to the retired ADR tree.

ADR files were removed on 2026-09-06 under #4667. Decision rationale now lives
in each domain's ``ARCHITECTURE.md``. This checker guards that references to a
retired decision-tree path do not reappear, while preserving explicit
exemptions for frozen fixtures and applied migrations.

``git ls-files`` keeps the scanned population deterministic and excludes local
build artifacts.

Invoked two ways, mirroring ``bazel/tools/format/readme_structure/``:

  * As a CLI from the CI "Format check" step, against the real checkout.
  * As pure functions imported by ``check_doc_links_test.py``, which pins the
    contract with synthetic inputs and needs no real files.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

_DECISIONS_PREFIX = "docs/" + "decisions/"

# A reference to a numbered ADR by full repo path.
_ADR_REF_RE = re.compile(
    re.escape(_DECISIONS_PREFIX) + r"[a-z0-9_]+/\d{3}-[a-z0-9-]+\.md"
)

# A markdown link to a relative .md path, e.g. [003](003-context-forge.md) or
# [020](../agents/020-deprecate.md). ADRs cite each other this way rather than
# by full repo path, and that is the population that actually breaks on
# deletion, so full-path matching alone would miss most of it.
_REL_MD_LINK_RE = re.compile(r"\]\(([^)\s#]+\.md)(?:#[^)]*)?\)")

# A fenced code block. Links inside one are illustrations, not references:
# the (since harvested) static-docs-site ADR demonstrated link rewriting with
# deliberately unresolvable example paths, and flagging those would be wrong.
_FENCE_RE = re.compile(r"^\s*(```|~~~)", re.MULTILINE)


def strip_code_fences(text: str) -> str:
    """Text with fenced code blocks removed, line count preserved for clarity."""
    out, in_fence = [], False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


# Files whose ADR-shaped strings are illustrative rather than real references.
#
# Tests build synthetic decision trees (stand-in names like "001-a.md") to pin
# generator behaviour without depending on real ADRs, which is correct: a test
# that breaks when an unrelated ADR is deleted is a bad test. Matched on
# filename so a new test needs no edit here.
#
# Note the deliberate absence of a full example path anywhere in this file: a
# complete one would match the pattern below and make the guard flag itself.
_TEST_FILE_SUFFIXES: tuple[str, ...] = (
    "_test.py",
    ".test.js",
    ".test.ts",
    "_test.go",
    "_test.sh",
)

# Individual non-test files carrying an ADR-shaped path as documentation rather
# than as a link. Keep this list short and commented: an entry here is a claim
# that the path is an example, not a reference.
#
# The model-bench research task pins a historical decision snapshot at a fixed
# commit and asks the agent to name the ADR file in that snapshot, so its
# expected answer is a path in history, not a reference to the working tree.
#
_EXAMPLE_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Frozen knowledge-gate calibration fixtures: an atom's evidence text
        # quotes the ADR path it was extracted from, and rewriting the evidence
        # would change what the calibration test grades.
        "projects/monolith/knowledge/testdata/atoms-all.tsv",
        "projects/monolith/knowledge/testdata/atoms-graded.tsv",
        "projects/model-bench/tasks/research-adr-writeback-01/task.yaml",
    }
)

# Generated artifacts that embed whole doc bodies, so they inherit every path
# mentioned inside the prose they bake in. Regenerating is what fixes them.
_GENERATED: frozenset[str] = frozenset(
    {
        "projects/monolith/frontend/src/lib/public/docs/docs-manifest.json",
        "projects/monolith/knowledge/repo_docs_manifest.ndjson",
        "projects/monolith/chat/orchestrator_bundle.md",
    }
)

# Applied Atlas migrations are immutable: Atlas records each applied file's
# hash in its revisions table and refuses to apply a directory whose history
# changed, and atlas.sum chains every later file's checksum on top. Editing
# even a comment to repoint an ADR path would therefore break the in-cluster
# AtlasMigration, so these keep their historical citation; the rollup that
# deleted the ADR records where it went in the domain's ARCHITECTURE.md.
_APPLIED_MIGRATIONS: frozenset[str] = frozenset(
    {
        "projects/monolith/chart/migrations/20260703070000_grimoire_schema.sql",
        "projects/monolith/chart/migrations/20260714000000_faas_function.sql",
    }
)

# Binary-ish extensions never worth reading as text.
_SKIP_SUFFIXES: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".pdf",
    ".tgz",
    ".gz",
    ".zip",
    ".lock",
)


def should_scan(rel_path: str) -> bool:
    """True when a tracked path's ADR references should be required to resolve."""
    if rel_path in _GENERATED or rel_path in _EXAMPLE_ALLOWLIST:
        return False
    if rel_path in _APPLIED_MIGRATIONS:
        return False
    if rel_path.endswith(_SKIP_SUFFIXES):
        return False
    if rel_path.endswith(_TEST_FILE_SUFFIXES):
        return False
    return True


def find_refs(text: str) -> list[str]:
    """Every distinct ADR path referenced in one file's text, in order."""
    seen: dict[str, None] = {}
    for match in _ADR_REF_RE.findall(text):
        seen.setdefault(match, None)
    return list(seen)


def find_relative_links(rel_path: str, text: str) -> list[str]:
    """Repo-relative targets of markdown links inside a doc, resolved from its dir.

    Only applied to files under the retired decision tree: elsewhere a relative .md
    link is not an ADR reference and is out of scope for this guard.
    """
    if not rel_path.startswith(_DECISIONS_PREFIX):
        return []
    base = pathlib.PurePosixPath(rel_path).parent
    seen: dict[str, None] = {}
    for target in _REL_MD_LINK_RE.findall(strip_code_fences(text)):
        if target.startswith(("http://", "https://", "/")):
            continue
        resolved = os.path.normpath(str(base / target)).replace(os.sep, "/")
        # Only ADR-to-ADR links are in scope. An ADR also links out to code and
        # to the retired docs/plans/ tree, and 11 of those already dangle; that
        # is pre-existing rot with a different cause, and folding it in here
        # would mean this guard could never go green.
        if not resolved.startswith(_DECISIONS_PREFIX):
            continue
        seen.setdefault(resolved, None)
    return list(seen)


def check_file(rel_path: str, text: str) -> list[str]:
    """Violation lines for one file."""
    if not should_scan(rel_path):
        return []
    violations = [
        f"{rel_path}: references the retired ADR path: {ref} "
        f"(point at the domain's ARCHITECTURE.md instead)"
        for ref in find_refs(text)
    ]
    violations.extend(
        f"{rel_path}: links within the retired ADR tree: {ref} "
        f"(drop or repoint the link)"
        for ref in find_relative_links(rel_path, text)
    )
    return violations


def tracked_files(repo_root: pathlib.Path) -> list[str]:
    """Repo-relative paths git tracks. Never a filesystem walk (see module docstring)."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def check(repo_root: pathlib.Path) -> list[str]:
    """Return human-readable violation lines; empty means every reference resolves."""

    violations: list[str] = []
    for rel_path in tracked_files(repo_root):
        if not should_scan(rel_path):
            continue
        try:
            text = (repo_root / rel_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        violations.extend(check_file(rel_path, text))
    return violations


def main() -> int:
    violations = check(pathlib.Path.cwd())
    if not violations:
        print("ADR references intact")
        return 0
    print("ADR link check failed:", file=sys.stderr)
    for v in violations:
        print(f"  - {v}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
