"""Guard the root README's structural claims against the real tree.

Two failure modes rot the top-level README as the repo changes:

  1. **Deleted-project links**: a `[text](path)` in the README points at a
     directory or file that no longer exists (a project was renamed/removed).
  2. **Missing entries**: a new top-level `projects/*` directory lands with no
     mention anywhere in the README.

Both are mechanically decidable, so they are enforced here rather than left to
a human noticing. Prose staleness (a wrong model name, an out-of-date count)
needs judgment and is handled out of band by the weekly refresh routine.

Invoked two ways, mirroring `projects/firecracker/tools/env_readme/`:

  * As a CLI from the CI "Format check" step (`check()` against the real
    checkout, `exit 1` on any violation). That step runs on the full working
    tree, so it can list `projects/*` and resolve links, something a sandboxed
    `bazel test` cannot do on RBE.
  * As pure functions imported by `check_readme_structure_test.py`, which pins
    the contract with synthetic inputs (sandbox-safe, no real files).
"""

from __future__ import annotations

import pathlib
import re
import sys

# Top-level projects/* directories that are intentionally NOT called out in the
# README. Keep this list honest and commented: adding a name here is a decision
# that "this project is not worth a top-level README mention", not a way to
# silence the check. A brand-new project should fail the coverage check until
# someone either lists it in the README or adds it here on purpose.
COVERAGE_ALLOWLIST = frozenset(
    {
        "__pycache__",  # stray bytecode, never a project
        "shared",  # cross-project helpers, not a standalone system
        # The README's layout tree explicitly shows "major dirs" only; these are
        # real projects deliberately left out of that top-level tree. Promote one
        # into the README (and drop it from here) if it grows into a headline system.
        "model-bench",
    }
)

# A repo-relative markdown link worth resolving: skip external schemes, in-page
# anchors, and mailto. Captures the path, minus any #anchor or ?query tail.
_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_SKIP_PREFIXES = ("http://", "https://", "mailto:", "#", "tel:")

# A layout-tree entry: a line whose only leading content is tree-drawing glyphs
# / whitespace, then a directory token ending in "/". The README documents most
# top-level dirs this way (`├── platform/   # ...`) inside a `projects/` fence,
# rather than as inline projects/<name> links, so both forms count as coverage.
_TREE_ENTRY_RE = re.compile(r"(?m)^[\s│├└─|`+*-]*([A-Za-z0-9._-]+)/")


def extract_repo_links(readme_text: str) -> list[str]:
    """Repo-relative link targets from markdown, de-anchored and de-duped."""
    seen: dict[str, None] = {}
    for raw in _LINK_RE.findall(readme_text):
        target = raw.strip()
        if not target or target.startswith(_SKIP_PREFIXES):
            continue
        # Drop #fragment / ?query tails; a link to foo.md#heading resolves to foo.md.
        target = target.split("#", 1)[0].split("?", 1)[0]
        if target:
            seen.setdefault(target, None)
    return list(seen)


def find_broken_links(
    repo_root: pathlib.Path, readme_rel: str, readme_text: str
) -> list[str]:
    """Repo-relative links whose path does not exist on disk.

    Links are resolved relative to the README's own directory, exactly as a
    reader following them would.
    """
    base = (repo_root / readme_rel).parent
    broken = []
    for link in extract_repo_links(readme_text):
        if not (base / link).exists():
            broken.append(link)
    return broken


def mentioned_names(readme_text: str) -> set[str]:
    """Top-level names the README documents, in either supported form.

    A name counts as documented if it appears as an inline `projects/<name>`
    link OR as a `<name>/` entry in a layout tree. A bare prose mention of the
    name is intentionally NOT enough: the point is that the structural
    references stay real, not that the word appears somewhere.
    """
    names = set(_TREE_ENTRY_RE.findall(readme_text))
    for link in extract_repo_links(readme_text):
        parts = link.split("/")
        if len(parts) >= 2 and parts[0] == "projects" and parts[1]:
            names.add(parts[1])
    return names


def find_uncovered_projects(
    project_dirs: list[str],
    readme_text: str,
    allowlist: frozenset[str] = COVERAGE_ALLOWLIST,
) -> list[str]:
    """Top-level project names neither documented in the README nor allowlisted."""
    documented = mentioned_names(readme_text)
    return [
        name
        for name in project_dirs
        if name not in allowlist and name not in documented
    ]


def list_project_dirs(repo_root: pathlib.Path) -> list[str]:
    """Names of the immediate subdirectories of projects/."""
    projects = repo_root / "projects"
    return sorted(p.name for p in projects.iterdir() if p.is_dir())


def check(repo_root: pathlib.Path, readme_rel: str = "README.md") -> list[str]:
    """Return human-readable violation lines; empty means the README is intact."""
    readme_text = (repo_root / readme_rel).read_text()
    violations = []

    for link in find_broken_links(repo_root, readme_rel, readme_text):
        violations.append(
            f"{readme_rel}: link target does not exist: {link} "
            f"(a project may have been renamed or removed)"
        )

    project_dirs = list_project_dirs(repo_root)
    for name in find_uncovered_projects(project_dirs, readme_text):
        violations.append(
            f"projects/{name}: new top-level project not referenced in {readme_rel}. "
            f"Either add it to the README or, if it is intentionally minor, add "
            f"'{name}' to COVERAGE_ALLOWLIST in "
            f"bazel/tools/format/readme_structure/check_readme_structure.py"
        )

    return violations


def main() -> int:
    repo_root = pathlib.Path.cwd()
    violations = check(repo_root)
    if not violations:
        print("README structure intact")
        return 0
    print("README structure check failed:", file=sys.stderr)
    for v in violations:
        print(f"  - {v}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
