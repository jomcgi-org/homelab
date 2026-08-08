"""Generate the baked orchestrator context bundle (ADR 036 Phase 2).

Concatenates, in strict stable order, three deterministic sections into a
single committed markdown file the ADR 036 orchestrator client sends as the
stable prefix of its `system` message (so provider prompt caching applies):

1. The hand-written base prompt (``projects/monolith/chat/orchestrator_prompt.md``).
2. A recipe catalog: one ``- <name>: <description>`` line per recipe YAML
   under ``projects/monolith/goosecracker/recipes/*.yaml``,
   sorted by recipe name (the filename stem).
3. A repo structure digest: the sorted top-level entries under ``projects/``
   and the sorted category list under ``docs/decisions/``.

Byte-determinism is the whole point (spec section 4: "no timestamps, ids, or
unsorted collections anywhere before the volatile tail"): two runs over the
same tree must produce identical bytes, and consecutive escalations against an
unchanged repo must produce an identical `system` message so the provider's
prefix cache actually hits. That means:

- No wall-clock timestamps, run ids, or hostnames anywhere in the output.
- Directory listings come from ``git ls-files`` (never a raw filesystem walk,
  whose order is platform- and inode-dependent) and are always sorted before
  formatting, exactly like ``gen_repo_docs_manifest.py``.
- Recipe descriptions are read from committed YAML via a tiny line-based
  scalar parser (see ``_read_recipe_field``), not a full YAML load, to keep
  this generator dependency-free like its sibling generators.

Regeneration is automatic: the "Format check" CI action (buildbuddy.yaml) runs
this generator on every push (wired into ``bazel/tools/format/run-generators.sh``
alongside the doc manifests) and auto-commits any change to the bundle on PR
branches (as ci-format-bot), so a recipe description edit or a new top-level
project directory never needs a manual regen. To regenerate locally: run this
script with any python3 (or ``bazel run //projects/monolith:gen_orchestrator_bundle``).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

BUNDLE_REL = "projects/monolith/chat/orchestrator_bundle.md"
PROMPT_REL = "projects/monolith/chat/orchestrator_prompt.md"
RECIPES_DIR_REL = "projects/monolith/goosecracker/recipes"
PROJECTS_DIR_REL = "projects"
DECISIONS_DIR_REL = "docs/decisions"

_DESCRIPTION_RE = re.compile(r'^description:\s*"(.*)"\s*$')
_TITLE_RE = re.compile(r'^title:\s*"(.*)"\s*$')


def _read_recipe_field(content: str, pattern: re.Pattern[str]) -> str | None:
    """Line-based scalar extraction for a top-level ``key: "value"`` field.

    Deliberately not a full YAML parse (this generator stays dependency-free,
    like its sibling generators): every guest recipe's ``description``/``title``
    is a single double-quoted scalar on its own line near the top of the file,
    so a line match is exact and avoids pulling in a YAML library for one field.
    """
    for line in content.splitlines():
        m = pattern.match(line)
        if m:
            return m.group(1)
    return None


def recipe_catalog_entries(root: Path, recipe_paths: list[str]) -> list[str]:
    """One ``name: description`` entry per recipe, sorted by name.

    Description falls back to title if a recipe ever omits ``description``
    (none currently do); a recipe missing both fields is skipped rather than
    emitting a blank catalog line.
    """
    entries: dict[str, str] = {}
    for rel in recipe_paths:
        name = Path(rel).stem
        content = (root / rel).read_text(encoding="utf-8")
        description = _read_recipe_field(content, _DESCRIPTION_RE)
        if description is None:
            description = _read_recipe_field(content, _TITLE_RE)
        if description is None:
            continue
        entries[name] = description
    return [f"- {name}: {entries[name]}" for name in sorted(entries)]


def _git_ls_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return [p for p in result.stdout.split("\0") if p and (root / p).is_file()]


def iter_recipe_paths(all_paths: list[str]) -> list[str]:
    prefix = f"{RECIPES_DIR_REL}/"
    return sorted(p for p in all_paths if p.startswith(prefix) and p.endswith(".yaml"))


def repo_structure_digest(all_paths: list[str]) -> list[str]:
    """Sorted top-level ``projects/`` entries + sorted ``docs/decisions`` categories."""
    project_prefix = f"{PROJECTS_DIR_REL}/"
    project_dirs = sorted(
        {
            p[len(project_prefix) :].split("/", 1)[0]
            for p in all_paths
            if p.startswith(project_prefix) and "/" in p[len(project_prefix) :]
        }
    )

    decisions_prefix = f"{DECISIONS_DIR_REL}/"
    decision_categories = sorted(
        {
            p[len(decisions_prefix) :].split("/", 1)[0]
            for p in all_paths
            if p.startswith(decisions_prefix) and "/" in p[len(decisions_prefix) :]
        }
    )

    lines = ["Top-level projects/ directories:"]
    lines.extend(f"- {d}" for d in project_dirs)
    lines.append("")
    lines.append("docs/decisions/ categories:")
    lines.extend(f"- {c}" for c in decision_categories)
    return lines


def build_bundle(root: Path) -> str:
    all_paths = _git_ls_files(root)
    base_prompt = (root / PROMPT_REL).read_text(encoding="utf-8").rstrip("\n")

    recipe_paths = iter_recipe_paths(all_paths)
    catalog_lines = recipe_catalog_entries(root, recipe_paths)
    structure_lines = repo_structure_digest(all_paths)

    return assemble_bundle(base_prompt, catalog_lines, structure_lines)


def assemble_bundle(
    base_prompt: str, catalog_lines: list[str], structure_lines: list[str]
) -> str:
    """Pure concatenation in the exact stable section order (no I/O, no git).

    Split out from ``build_bundle`` so determinism and formatting can be unit
    tested without a git checkout: this is the part of the pipeline the test
    suite exercises directly, matching how ``build_manifest_lines`` in
    ``gen_repo_docs_manifest.py`` is tested apart from its git-driven path
    discovery.
    """
    sections = [
        base_prompt,
        "\n## Recipe catalog\n\n" + "\n".join(catalog_lines),
        "\n## Repo structure\n\n" + "\n".join(structure_lines),
    ]
    return "\n".join(sections) + "\n"


def main() -> int:
    root = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY") or os.getcwd())
    out = root / BUNDLE_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    bundle = build_bundle(root)
    out.write_text(bundle, encoding="utf-8")
    print(f"wrote {len(bundle)} bytes to {BUNDLE_REL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
