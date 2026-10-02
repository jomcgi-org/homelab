"""Deterministic "norms" signals for a passing agentic cell (#6699).

Passing the verifier is the floor; these free, judge-less signals rank how a model got
there: did it stay in scope, leave debug output behind, add lint findings, bloat the
diff relative to the real fix, or change code without touching a test. They are
computed from the fixture (before) and the final workdir (after), so they are
identical for every harness (bench tool loop or Claude Code anchor).

norms_score is 1 minus a weighted mean of per-signal penalties in [0, 1]. A signal
that does not apply to the task (no target_files, no gold size, ruff unavailable, no
code changed) is left out and the remaining weights are renormalised, so a task is
never penalised for metadata it does not carry.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
from pathlib import Path

# Weight of each penalty in norms_score. Scope and debug leftovers are the clearest
# norm violations; diff size and lint are noisier; a missing test is the softest
# signal because not every code change in a fixture has a natural test home.
WEIGHTS = {
    "scope": 0.25,
    "debug": 0.20,
    "lint": 0.20,
    "size": 0.20,
    "test": 0.15,
}

# Penalty saturation points: this many violations (or this diff-size excess) is a
# full penalty for that signal.
SCOPE_SATURATION = 2  # files changed outside target_files
DEBUG_SATURATION = 3  # added print/breakpoint/console.log/TODO/FIXME/XXX lines
LINT_SATURATION = 5  # new ruff findings
SIZE_FREE_RATIO = 1.5  # up to 1.5x the gold diff is free
SIZE_SATURATION = 3.0  # ... and a further 3x on top of that is a full penalty

_MARKER_RE = re.compile(r"\bbreakpoint\(|console\.log\(|\b(TODO|FIXME|XXX)\b")
_DEBUG_RE = re.compile(r"\bprint\(|" + _MARKER_RE.pattern)
_CODE_SUFFIXES = (".py", ".go", ".js", ".ts")
_IGNORED_DIRS = {"__pycache__", "node_modules"}


def _is_test(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name.endswith(("_test.py", "_test.go")) or (
        name.startswith("test_") and name.endswith(".py")
    )


def _files(root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        # Hidden dirs cover tool caches (.pytest_cache, .ruff_cache, .claude, .git).
        if any(
            part in _IGNORED_DIRS or part.startswith(".") for part in rel.parts[:-1]
        ):
            continue
        out[rel.as_posix()] = p
    return out


def _read(p: Path | None) -> str | None:
    if p is None:
        return ""
    try:
        return p.read_text()
    except (UnicodeDecodeError, OSError):
        return None


# ruff's default rule set changes between releases, so the uvx path is pinned to keep
# lint_delta comparable across runs. A ruff already on PATH is used as-is.
RUFF_VERSION = "0.16.10"


def _ruff_cmd() -> list[str] | None:
    if shutil.which("ruff"):
        return ["ruff"]
    if shutil.which("uvx"):
        return ["uvx", f"ruff@{RUFF_VERSION}"]
    return None


def _ruff_count(cmd: list[str], rel: str, text: str) -> int:
    # --isolated: default rule set, independent of whichever pyproject sits above the
    # temp workdir, so the count is reproducible across machines.
    res = subprocess.run(
        [
            *cmd,
            "check",
            "--isolated",
            "--no-cache",
            "--output-format",
            "json",
            "--stdin-filename",
            rel,
            "-",
        ],
        input=text,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if res.returncode not in (0, 1):
        raise RuntimeError(res.stderr.strip()[:200])
    return len(json.loads(res.stdout or "[]"))


def compute_norms(
    fixture_dir: Path,
    workdir: Path,
    *,
    target_files: list[str] | None = None,
    gold_diff_lines: int | None = None,
    lint: bool = True,
) -> dict:
    """Diff fixture_dir against workdir and score the change against repo norms."""
    before, after = _files(fixture_dir), _files(workdir)
    changed: list[str] = []
    added = removed = debug = 0
    py_changes: list[tuple[str, str, str]] = []
    for rel in sorted(set(before) | set(after)):
        old, new = _read(before.get(rel)), _read(after.get(rel))
        if old is None or new is None:
            # Binary on either side: count the file, skip line-level signals.
            if (
                before.get(rel) is None
                or after.get(rel) is None
                or (before[rel].read_bytes() != after[rel].read_bytes())
            ):
                changed.append(rel)
            continue
        if old == new:
            continue
        changed.append(rel)
        for line in difflib.unified_diff(
            old.splitlines(), new.splitlines(), lineterm=""
        ):
            if line.startswith(("+++", "---")):
                continue
            if line.startswith("+"):
                added += 1
                body = line[1:]
                # print() is normal in a test's diagnostics; elsewhere it is debris.
                pattern = _MARKER_RE if _is_test(rel) else _DEBUG_RE
                if pattern.search(body):
                    debug += 1
            elif line.startswith("-"):
                removed += 1
        if rel.endswith(".py") and rel in after:
            py_changes.append((rel, old, new))

    outside = (
        len([rel for rel in changed if rel not in set(target_files)])
        if target_files
        else None
    )
    test_added = any(_is_test(rel) for rel in changed)
    code_changed = any(
        rel.endswith(_CODE_SUFFIXES) and not _is_test(rel) for rel in changed
    )

    lint_delta: int | None = None
    cmd = _ruff_cmd() if lint and py_changes else None
    if cmd is not None:
        try:
            lint_delta = sum(
                max(
                    0,
                    _ruff_count(cmd, rel, new)
                    - (_ruff_count(cmd, rel, old) if old else 0),
                )
                for rel, old, new in py_changes
            )
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
            lint_delta = None
    elif lint and not py_changes:
        lint_delta = 0

    diff_lines = added + removed
    diff_ratio = (diff_lines / gold_diff_lines) if gold_diff_lines else None

    penalties: dict[str, float] = {
        "debug": min(1.0, debug / DEBUG_SATURATION),
    }
    if outside is not None:
        penalties["scope"] = min(1.0, outside / SCOPE_SATURATION)
    if lint_delta is not None:
        penalties["lint"] = min(1.0, lint_delta / LINT_SATURATION)
    if diff_ratio is not None:
        penalties["size"] = min(
            1.0, max(0.0, diff_ratio - SIZE_FREE_RATIO) / SIZE_SATURATION
        )
    if code_changed:
        penalties["test"] = 0.0 if test_added else 1.0
    total_w = sum(WEIGHTS[k] for k in penalties)
    score = 1.0 - sum(WEIGHTS[k] * p for k, p in penalties.items()) / total_w

    return {
        "files_changed": len(changed),
        "lines_added": added,
        "lines_removed": removed,
        "files_outside_targets": outside,
        "debug_leftovers": debug,
        "test_added": test_added,
        "lint_delta": lint_delta,
        "diff_ratio": round(diff_ratio, 3) if diff_ratio is not None else None,
        "norms_score": round(score, 4),
    }


DIFF_CAP = 60_000


def unified_diff(fixture_dir: Path, workdir: Path, cap: int = DIFF_CAP) -> str:
    """The cell's change as one unified diff (text files only), capped at cap chars.

    Stored on a passing cell so the pairwise judge (bench/pairwise.py) can compare
    changes after the workdir is gone.
    """
    before, after = _files(fixture_dir), _files(workdir)
    chunks: list[str] = []
    for rel in sorted(set(before) | set(after)):
        old, new = _read(before.get(rel)), _read(after.get(rel))
        if old is None or new is None or old == new:
            continue
        chunks.extend(
            difflib.unified_diff(
                old.splitlines(),
                new.splitlines(),
                fromfile=f"a/{rel}" if rel in before else "/dev/null",
                tofile=f"b/{rel}" if rel in after else "/dev/null",
                lineterm="",
            )
        )
    text = "\n".join(chunks)
    return text if len(text) <= cap else text[:cap] + "\n[diff truncated]"


def safe_diff(fixture_dir: Path, workdir: Path) -> str | None:
    """unified_diff for the cell runners; advisory like norms, so never raises."""
    try:
        return unified_diff(fixture_dir, workdir)
    except Exception:  # noqa: BLE001 - the diff is advisory; the pass stands
        return None


def safe_norms(fixture_dir: Path, workdir: Path, opts: dict | None) -> dict | None:
    """compute_norms for the cell runners: a norms failure must never fail a cell
    the verifier passed, so any error records no norms rather than raising."""
    try:
        return compute_norms(fixture_dir, workdir, **(opts or {}))
    except Exception:  # noqa: BLE001 - norms are advisory; the pass stands
        return None
