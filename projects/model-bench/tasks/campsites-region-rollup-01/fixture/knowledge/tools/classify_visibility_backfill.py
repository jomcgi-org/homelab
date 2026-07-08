"""Phase-2 backfill: classify every unlabelled note via the `claude` CLI.

The script writes ``visibility: public`` or ``visibility: private`` into
each note's frontmatter via in-place line replacement (no full YAML
round-trip). Per ``memory/feedback_claude_cli_subprocess_for_tos.md``,
we shell out to the ``claude`` CLI rather than the Anthropic SDK.

Resumable: re-running picks up only still-unlabelled notes. Crash
recovery is implicit — partial work is on disk.

Report: a JSON file written OUTSIDE the repo (the script refuses
report paths inside the working tree). Rationales quote note content
and must not be committed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from knowledge import frontmatter
from knowledge.visibility import VISIBILITY_CRITERIA

logger = logging.getLogger("monolith.knowledge.tools.classify_visibility_backfill")

_FRONTMATTER_RE = re.compile(r"^(---\n)(.*?)(\n---\n)", re.DOTALL)
_VISIBILITY_LINE_RE = re.compile(r"^visibility:.*$", re.MULTILINE)


class BackfillError(Exception):
    """Raised when the classifier output cannot be safely applied."""


@dataclass
class _ReportEntry:
    note_id: str
    decision: str | None
    rationale: str | None
    error: str | None = None


@dataclass
class _Report:
    entries: list[_ReportEntry] = field(default_factory=list)


def _find_repo_root() -> Path | None:
    """Walk up from this file looking for a `.git` directory.

    Returns the repo root, or ``None`` if no `.git` is found (e.g. running
    from inside a Bazel runfiles tree). Callers should treat ``None`` as
    "no repo to protect against" and skip the report-path check.
    """
    here = Path(__file__).resolve()
    for ancestor in (here, *here.parents):
        if (ancestor / ".git").exists():
            return ancestor
    return None


def _check_report_path_outside_repo(report: Path, repo_root: Path | None) -> None:
    """Raise ``BackfillError`` if ``report`` would land inside ``repo_root``.

    Anchored to the actual repo root (detected via ``.git/``) rather than
    ``Path.cwd()``, which is unreliable: pytest tmpdirs sit outside the
    repo and would defeat a cwd-based check.
    """
    if repo_root is None:
        return
    try:
        report.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return  # outside the repo — safe
    raise BackfillError(
        f"refusing to write report at {report} — must be outside the repo "
        "working tree (rationales quote note content; never commit)"
    )


def classify_one(*, body: str, title: str) -> tuple[str, str]:
    """Single LLM call via the ``claude`` CLI subprocess.

    Returns ``(decision, rationale)``. Raises ``BackfillError`` on any
    failure mode (non-zero exit, non-JSON output, invalid decision).
    """
    prompt = (
        f"{VISIBILITY_CRITERIA}\n\n"
        f"Title: {title}\n\nBody:\n{body}\n\n"
        "Respond with strict JSON:\n"
        '{"visibility": "public" | "private", "rationale": "<one sentence>"}\n'
    )
    proc = subprocess.run(
        ["claude", "--print", "--output-format", "json"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise BackfillError(
            f"claude exit={proc.returncode} stderr={proc.stderr[:200]!r}"
        )
    try:
        # `claude --output-format json` wraps the response; the actual
        # answer is in the .result field. Adjust if the CLI shape differs.
        outer = json.loads(proc.stdout)
        text = outer.get("result", proc.stdout)
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BackfillError(f"non-JSON output: {exc}") from exc

    decision = parsed.get("visibility")
    rationale = parsed.get("rationale", "")
    if decision not in {"public", "private"}:
        raise BackfillError(f"invalid decision {decision!r}")
    return decision, str(rationale)


def _replace_visibility_line(content: str, value: str) -> str:
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise BackfillError("note has no frontmatter")
    head, block, tail = match.group(1), match.group(2), match.group(3)
    if _VISIBILITY_LINE_RE.search(block):
        new_block = _VISIBILITY_LINE_RE.sub(f"visibility: {value}", block)
    else:
        new_block = block + f"\nvisibility: {value}"
    return head + new_block + tail + content[match.end() :]


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def _is_unlabelled(meta: frontmatter.ParsedFrontmatter) -> bool:
    return meta.visibility in (None, "")


def run(
    *,
    vault_root: Path,
    report: Path,
    run_one_for_test: Callable[[str], tuple[str, str]] | None = None,
    max_files: int | None = None,
    _repo_root_for_test: Path | None = None,
) -> _Report:
    """Walk vault and classify every unlabelled note.

    ``run_one_for_test`` is the seam tests use to avoid hitting the real
    ``claude`` CLI; production passes ``None`` and we use ``classify_one``.
    ``_repo_root_for_test`` overrides the ``.git``-based repo-root detection
    so the report-path safety check is exercised under pytest tmpdirs (which
    sit outside the worktree).
    """
    repo_root = (
        _repo_root_for_test if _repo_root_for_test is not None else _find_repo_root()
    )
    _check_report_path_outside_repo(report, repo_root)

    rep = _Report()

    files = sorted((vault_root / "_processed").rglob("*.md"))
    processed = 0
    for path in files:
        if max_files is not None and processed >= max_files:
            break
        raw = path.read_text()
        try:
            meta, body = frontmatter.parse(raw)
        except frontmatter.FrontmatterError:
            rep.entries.append(
                _ReportEntry(
                    note_id=path.stem,
                    decision=None,
                    rationale=None,
                    error="frontmatter parse failure",
                )
            )
            continue
        if not _is_unlabelled(meta):
            continue

        try:
            if run_one_for_test is not None:
                decision, rationale = run_one_for_test(body)
            else:
                decision, rationale = classify_one(
                    body=body, title=meta.title or path.stem
                )
        except BackfillError as exc:
            rep.entries.append(
                _ReportEntry(
                    note_id=path.stem,
                    decision=None,
                    rationale=None,
                    error=str(exc),
                )
            )
            continue

        new = _replace_visibility_line(raw, decision)
        _atomic_write(path, new)
        rep.entries.append(
            _ReportEntry(
                note_id=path.stem,
                decision=decision,
                rationale=rationale,
            )
        )
        processed += 1

    report.write_text(json.dumps([e.__dict__ for e in rep.entries], indent=2))
    return rep


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument(
        "--report",
        type=Path,
        required=True,
        help="must be outside the repo working tree",
    )
    args = parser.parse_args()
    rep = run(vault_root=args.vault_root, report=args.report)
    classified = sum(1 for e in rep.entries if e.decision)
    errored = sum(1 for e in rep.entries if e.error)
    print(f"classified={classified} errored={errored} report={args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
