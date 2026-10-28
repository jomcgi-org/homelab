"""Generate the baked orchestrator context bundle (ADR 036 Phase 2).

Concatenates, in strict stable order, two deterministic sections into a
single committed markdown file the ADR 036 orchestrator client sends as the
stable prefix of its `system` message (so provider prompt caching applies):

1. The hand-written base prompt (``projects/monolith/chat/orchestrator_prompt.md``).
2. A repo structure digest: the sorted top-level entries under ``projects/``.

Byte-determinism is the whole point (spec section 4: "no timestamps, ids, or
unsorted collections anywhere before the volatile tail"): two runs over the
same tree must produce identical bytes, and consecutive escalations against an
unchanged repo must produce an identical `system` message so the provider's
prefix cache actually hits. That means:

- No wall-clock timestamps, run ids, or hostnames anywhere in the output.
- Directory listings come from ``git ls-files`` (never a raw filesystem walk,
  whose order is platform- and inode-dependent) and are always sorted before
  formatting, exactly like ``gen_repo_docs_manifest.py``.

Regeneration is automatic: the "Format check" CI action (buildbuddy.yaml) runs
this generator on every push (wired into ``bazel/tools/format/run-generators.sh``
alongside the doc manifests) and auto-commits any change to the bundle on PR
branches (as ci-format-bot), so a prompt edit or a new top-level
project directory never needs a manual regen. To regenerate locally: run this
script with any python3 (or ``bazel run //projects/monolith:gen_orchestrator_bundle``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BUNDLE_REL = "projects/monolith/chat/orchestrator_bundle.md"
PROMPT_REL = "projects/monolith/chat/orchestrator_prompt.md"
PROJECTS_DIR_REL = "projects"


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


def repo_structure_digest(all_paths: list[str]) -> list[str]:
    """Sorted top-level ``projects/`` entries."""
    project_prefix = f"{PROJECTS_DIR_REL}/"
    project_dirs = sorted(
        {
            p[len(project_prefix) :].split("/", 1)[0]
            for p in all_paths
            if p.startswith(project_prefix) and "/" in p[len(project_prefix) :]
        }
    )

    lines = ["Top-level projects/ directories:"]
    lines.extend(f"- {d}" for d in project_dirs)
    return lines


def build_bundle(root: Path) -> str:
    all_paths = _git_ls_files(root)
    base_prompt = (root / PROMPT_REL).read_text(encoding="utf-8").rstrip("\n")

    structure_lines = repo_structure_digest(all_paths)

    return assemble_bundle(base_prompt, structure_lines)


def assemble_bundle(
    base_prompt: str, structure_lines: list[str]
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
