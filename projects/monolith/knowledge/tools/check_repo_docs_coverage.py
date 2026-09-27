"""Fail when a tracked doc the manifests would index is outside the build graph.

Usage (CI format stage, from bazel/images/validate-generate-scripts.sh):
    python3 projects/monolith/knowledge/tools/check_repo_docs_coverage.py LABELS

LABELS is the output of
``bazel query 'kind("source file", deps(//projects/monolith:repo_docs_srcs))'``.

The doc manifests are build outputs (#6446): their genrules read the markdown
gathered by ``//projects/monolith:repo_docs_srcs``, one ``repo_docs`` filegroup
per package that owns a doc. A Bazel glob stops at package boundaries, so a doc
in a new package would silently drop out of the manifests. This check applies
both generators' selection to ``git ls-files`` (what they index when run
locally) and fails on any selected path the aggregate does not carry. Extra
covered files are fine: the generators filter their inputs.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

try:  # imported as knowledge.tools.* by tests, run as a bare script by CI
    from knowledge.tools import gen_docs_manifest, gen_repo_docs_manifest
except ImportError:  # pragma: no cover - script invocation
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gen_docs_manifest
    import gen_repo_docs_manifest

AGGREGATE = "//projects/monolith:repo_docs_srcs"


def label_to_path(label: str) -> str | None:
    """Repo-relative path for a main-repo source file label, else None."""
    label = label.strip()
    for prefix in ("@@//", "@//"):
        if label.startswith(prefix):
            label = label[len(prefix) - 2 :]
    if not label.startswith("//") or ":" not in label:
        return None
    package, name = label[2:].split(":", 1)
    return f"{package}/{name}" if package else name


def missing_docs(tracked: Iterable[str], covered_labels: Iterable[str]) -> list[str]:
    """Tracked paths either generator would index that no covered label names."""
    tracked = list(tracked)
    wanted = set(gen_repo_docs_manifest.select_doc_paths(tracked))
    wanted.update(p for p in tracked if gen_docs_manifest._should_index(p))
    covered = {path for label in covered_labels if (path := label_to_path(label))}
    return sorted(wanted - covered)


def _git_ls_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return [p for p in result.stdout.split("\0") if p]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    labels = Path(args[0]).read_text(encoding="utf-8").splitlines()
    if not any(label_to_path(label) for label in labels):
        print(f"no source files in the {AGGREGATE} query output", file=sys.stderr)
        return 1
    missing = missing_docs(_git_ls_files(), labels)
    if not missing:
        return 0
    print(f"Tracked docs the doc manifests index but {AGGREGATE} does not carry:")
    for path in missing:
        print(f"  {path}")
    print(
        "Add a `repo_docs` filegroup to the package that owns each file (copy the"
        " one in projects/platform/kargo/BUILD) and list it in :repo_docs_srcs"
        " in projects/monolith/BUILD, or widen the root BUILD's repo_docs globs."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
