"""Deterministic "norms" signals for a passing agentic cell (#6699).

Passing the verifier is the floor; these free, judge-less signals rank how a model got
there: did it stay in scope, leave debug output behind, add lint findings, bloat the
diff relative to the real fix, or change code without touching a test. They are
computed from the fixture (before) and the authored workdir before verification, so they are
identical for every harness (bench tool loop or Claude Code anchor).

norms_score is 1 minus a weighted mean of per-signal penalties in [0, 1]. A signal
that does not apply to the task (no target_files, no gold size, ruff unavailable, no
code changed) is left out and the remaining weights are renormalised, so a task is
never penalised for metadata it does not carry.
"""

from __future__ import annotations

import difflib
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import tokenize
from collections import Counter
from pathlib import Path

# Weight of each penalty in norms_score. Scope and debug leftovers are the clearest
# norm violations; diff size and lint are noisier; a missing test is the softest
# signal because not every code change in a fixture has a natural test home.
WEIGHTS = {
    "scope": 0.225,
    "debug": 0.18,
    "lint": 0.18,
    "size": 0.18,
    "test": 0.135,
    "comments": 0.10,
}
NORMS_VERSION = 2
COMMENT_FREE_DELTA = 0.10
COMMENT_SATURATION_DELTA = 0.40

# Penalty saturation points: this many violations (or this diff-size excess) is a
# full penalty for that signal.
SCOPE_SATURATION = 2  # files changed outside target_files
DEBUG_SATURATION = 3  # added print/breakpoint/console.log/TODO/FIXME/XXX lines
LINT_SATURATION = 5  # new lint findings
SIZE_FREE_RATIO = 1.5  # up to 1.5x the gold diff is free
SIZE_SATURATION = 3.0  # ... and a further 3x on top of that is a full penalty

_MARKER_RE = re.compile(r"\bbreakpoint\(|console\.log\(|\b(TODO|FIXME|XXX)\b")
_DEBUG_RE = re.compile(r"\bprint\(|" + _MARKER_RE.pattern)
_CODE_SUFFIXES = (".py", ".go", ".js", ".ts")
_IGNORED_DIRS = {"__pycache__", "node_modules"}


def _is_test(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name.endswith(
        ("_test.py", "_test.go", ".test.js", ".test.ts", ".spec.js", ".spec.ts")
    ) or (name.startswith("test_") and name.endswith(".py"))


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


# Never download tools during scoring. Installed versions must match these pins.
RUFF_VERSION = "0.16.10"
GOLANGCI_LINT_VERSION = "2.1.6"
LINT_TIMEOUT = 60


def _ruff_cmd() -> list[str] | None:
    if shutil.which("ruff"):
        return ["ruff"]
    return None


def _check_version(cmd: list[str], expected: str) -> None:
    res = subprocess.run(
        [*cmd, "--version"],
        capture_output=True,
        text=True,
        timeout=LINT_TIMEOUT,
        check=False,
    )
    if res.returncode != 0 or not re.search(
        rf"(?<![\d.]){re.escape(expected)}(?![\d.])", res.stdout
    ):
        raise RuntimeError(f"{cmd[0]} version mismatch (expected {expected})")


def _ruff_findings(cmd: list[str], rel: str, text: str) -> Counter:
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
        timeout=LINT_TIMEOUT,
        check=False,
    )
    if res.returncode not in (0, 1):
        raise RuntimeError(res.stderr.strip()[:200])
    records = json.loads(res.stdout)
    if not isinstance(records, list):
        raise TypeError("ruff returned an invalid diagnostic list")
    if res.returncode == 1 and not records:
        raise RuntimeError("ruff failed without findings")
    return Counter((rel, item["code"], item["message"]) for item in records)


def _go_findings(root: Path, changes: list[str]) -> Counter:
    """Lint isolated module copies, never the authored or fixture tree."""
    modules: set[Path] = set()
    for rel in changes:
        if not (root / rel).exists():
            continue
        parent = (root / rel).parent
        while not (parent / "go.mod").is_file():
            if parent == root:
                raise RuntimeError("golangci-lint: missing go.mod")
            parent = parent.parent
        modules.add(parent)
    findings: Counter = Counter()
    for module in sorted(modules):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "module"
            shutil.copytree(module, copy, ignore=shutil.ignore_patterns(".git"))
            env = {
                **os.environ,
                "GOPROXY": "off",
                "GOSUMDB": "off",
                "GONOPROXY": "none",
                "GOTOOLCHAIN": "local",
                "GOENV": "off",
                "GOWORK": "off",
                "GOFLAGS": "-mod=readonly",
                "GOLANGCI_LINT_CACHE": str(Path(tmp) / "cache"),
            }
            res = subprocess.run(
                [
                    "golangci-lint",
                    "run",
                    "--no-config",
                    "--output.text.path=",
                    "--output.json.path=stdout",
                    "--show-stats=false",
                    "--issues-exit-code=1",
                    "--modules-download-mode=readonly",
                    f"--timeout={LINT_TIMEOUT}s",
                    "--max-issues-per-linter=0",
                    "--max-same-issues=0",
                    "--uniq-by-line=false",
                    "./...",
                ],
                cwd=copy,
                env=env,
                capture_output=True,
                text=True,
                timeout=LINT_TIMEOUT,
                check=False,
            )
            if res.returncode not in (0, 1):
                raise RuntimeError(
                    f"golangci-lint exit {res.returncode}: {res.stderr[:200]}"
                )
            payload = json.loads(res.stdout)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("Issues"), list
            ):
                raise TypeError("golangci-lint returned an invalid diagnostic list")
            issues = payload["Issues"]
            report = payload.get("Report") or {}
            if not isinstance(report, dict) or report.get("Error"):
                raise RuntimeError("golangci-lint reported an analysis error")
            if res.returncode == 1 and not issues:
                raise RuntimeError("golangci-lint failed without findings")
            for item in issues:
                if item["FromLinter"] == "typecheck":
                    raise RuntimeError("golangci-lint: code could not be typechecked")
                path = Path(item["Pos"]["Filename"])
                if path.is_absolute():
                    path = path.relative_to(copy)
                rel = (module.relative_to(root) / path).as_posix()
                if rel in changes:
                    findings[(rel, item["FromLinter"], item["Text"])] += 1
    return findings


def _js_comment_lines(text: str) -> set[int]:
    """Comment lines for JS/TS, with template-interpolation awareness.

    Inside a template literal, comment markers are literal text; only ${...}
    expressions hold real code and may nest further templates. Brace depth is
    tracked per expression so nested object literals do not end it early.
    """
    lines: set[int] = set()
    stack: list[tuple] = []  # ("str", quote) | ("tpl",) | ("expr", depth)
    block = False
    i = line = 0
    while i < len(text):
        char = text[i]
        top = stack[-1] if stack else None
        if block:
            if char.strip():
                lines.add(line)
            if text.startswith("*/", i):
                block = False
                i += 2
                continue
        elif top is not None and top[0] == "str":
            if char == "\\":
                if i + 1 < len(text) and text[i + 1] == "\n":
                    line += 1
                i += 2
                continue
            if char == top[1]:
                stack.pop()
        elif top is not None and top[0] == "tpl":
            if char == "\\":
                if i + 1 < len(text) and text[i + 1] == "\n":
                    line += 1
                i += 2
                continue
            if text.startswith("${", i):
                stack.append(("expr", 1))
                i += 2
                continue
            if char == "`":
                stack.pop()
        elif char in ('"', "'"):
            stack.append(("str", char))
        elif char == "`":
            stack.append(("tpl",))
        elif text.startswith("//", i):
            lines.add(line)
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
            continue
        elif text.startswith("/*", i):
            block = True
            lines.add(line)
            i += 2
            continue
        elif char == "{" and top is not None and top[0] == "expr":
            stack[-1] = ("expr", top[1] + 1)
        elif char == "}" and top is not None and top[0] == "expr":
            if top[1] <= 1:
                stack.pop()
            else:
                stack[-1] = ("expr", top[1] - 1)
        if char == "\n":
            line += 1
        i += 1
    if stack or block:
        raise ValueError("unterminated literal or comment")
    return lines


def _comment_lines(text: str, suffix: str) -> set[int]:
    """Zero-based physical lines containing comments, excluding string literals."""
    if suffix == ".py":
        return {
            token.start[0] - 1
            for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type == tokenize.COMMENT
        }
    if suffix in (".js", ".ts"):
        return _js_comment_lines(text)
    lines: set[int] = set()
    i = line = 0
    quote: str | None = None
    block = False
    while i < len(text):
        char = text[i]
        if block:
            if char.strip():
                lines.add(line)
            if text.startswith("*/", i):
                block = False
                i += 2
                continue
        elif quote:
            if char == "\\" and quote != "`":
                if i + 1 < len(text) and text[i + 1] == "\n":
                    line += 1
                i += 2
                continue
            if char == "\\" and quote == "`" and suffix != ".go":
                if i + 1 < len(text) and text[i + 1] == "\n":
                    line += 1
                i += 2
                continue
            if char == quote:
                quote = None
        elif char in ('"', "'", "`"):
            quote = char
        elif text.startswith("//", i):
            lines.add(line)
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
            continue
        elif text.startswith("/*", i):
            block = True
            lines.add(line)
            i += 2
            continue
        if char == "\n":
            line += 1
        i += 1
    if quote or block:
        raise ValueError("unterminated literal or comment")
    return lines


def _comment_density(
    changes: list[tuple[str, str, str]],
) -> tuple[float | None, float | None, float | None]:
    added_n = added_comments = baseline_n = baseline_comments = 0
    try:
        for rel, old, new in changes:
            if _is_test(rel) or not rel.endswith(_CODE_SUFFIXES):
                continue
            old_lines, new_lines = old.splitlines(), new.splitlines()
            added_indexes = {
                i
                for tag, _, _, start, end in difflib.SequenceMatcher(
                    None, old_lines, new_lines, autojunk=False
                ).get_opcodes()
                if tag in ("insert", "replace")
                for i in range(start, end)
                if new_lines[i].strip()
            }
            old_comments = _comment_lines(old, Path(rel).suffix)
            new_comments = _comment_lines(new, Path(rel).suffix)
            baseline_n += sum(bool(line.strip()) for line in old_lines)
            baseline_comments += len(old_comments)
            added_n += len(added_indexes)
            added_comments += len(added_indexes & new_comments)
    except (tokenize.TokenError, SyntaxError, ValueError):
        return None, None, None
    if not added_n:
        return None, None, None
    added_density = added_comments / added_n
    baseline_density = baseline_comments / baseline_n if baseline_n else 0.0
    return added_density, baseline_density, added_density - baseline_density


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
    text_changes: list[tuple[str, str, str]] = []
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
        text_changes.append((rel, old, new))
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
    lint_unavailable: str | None = None
    py_changes = [
        (rel, old, new) for rel, old, new in text_changes if rel.endswith(".py")
    ]
    go_changes = [rel for rel in changed if rel.endswith(".go")]
    if lint:
        try:
            lint_delta = 0
            if py_changes:
                cmd = _ruff_cmd()
                if cmd is None:
                    raise RuntimeError("ruff not on PATH")
                _check_version(cmd, RUFF_VERSION)
                for rel, old, new in py_changes:
                    baseline = _ruff_findings(cmd, rel, old) if old else Counter()
                    current = _ruff_findings(cmd, rel, new) if new else Counter()
                    lint_delta += sum((current - baseline).values())
            if go_changes:
                if not shutil.which("golangci-lint"):
                    raise RuntimeError("golangci-lint not on PATH")
                _check_version(["golangci-lint"], GOLANGCI_LINT_VERSION)
                baseline = _go_findings(fixture_dir, go_changes)
                current = _go_findings(workdir, go_changes)
                lint_delta += sum((current - baseline).values())
            if any(
                rel.endswith(".py") and rel not in {p[0] for p in py_changes}
                for rel in changed
            ):
                raise RuntimeError("ruff: changed Python file is not readable text")
        except (
            RuntimeError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.TimeoutExpired,
        ) as exc:
            lint_delta = None
            lint_unavailable = str(exc)[:300] or type(exc).__name__

    comment_added, comment_baseline, comment_delta = _comment_density(text_changes)

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
    if comment_delta is not None:
        penalties["comments"] = min(
            1.0,
            max(0.0, abs(comment_delta) - COMMENT_FREE_DELTA)
            / (COMMENT_SATURATION_DELTA - COMMENT_FREE_DELTA),
        )
    total_w = sum(WEIGHTS[k] for k in penalties)
    score = 1.0 - sum(WEIGHTS[k] * p for k, p in penalties.items()) / total_w

    return {
        "norms_version": NORMS_VERSION,
        "files_changed": len(changed),
        "lines_added": added,
        "lines_removed": removed,
        "files_outside_targets": outside,
        "debug_leftovers": debug,
        "test_added": test_added,
        "lint_delta": lint_delta,
        "lint_unavailable": lint_unavailable,
        "comment_density_added": comment_added,
        "comment_density_baseline": comment_baseline,
        "comment_density_delta": comment_delta,
        "diff_ratio": round(diff_ratio, 3) if diff_ratio is not None else None,
        "norms_score": round(score, 4),
    }


def project_snapshot_path(path: str, snap: dict) -> str | None:
    """Match git archive paths, tar stripping and the snapshot's pruning rules."""
    if not any(
        path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/")
        for p in snap.get("paths", [])
    ):
        return None
    parts = Path(path).parts[snap.get("strip_components") or 0 :]
    if not parts:
        return None
    for exclude in snap.get("exclude", ["*_test.py"]):
        if exclude.endswith("/"):
            if exclude.rstrip("/") in parts[:-1]:
                return None
        elif fnmatch.fnmatch(parts[-1], exclude):
            return None
    return Path(*parts).as_posix()


def gold_diff_size(
    repo: Path, source_commit: str, snap: dict
) -> tuple[int | None, str | None]:
    """Project a real fix onto an unchanged pre-fix snapshot. Return size or reason."""
    if not snap.get("commit") or not snap.get("paths"):
        return None, "snapshot needs commit and paths"
    if snap.get("patches") or snap.get("review_diff"):
        return None, "snapshot has planted patches or review content"

    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=True,
            timeout=120,
        ).stdout

    try:
        parent = (
            git("rev-parse", "--verify", f"{source_commit}^{{commit}}").decode().strip()
            + "^"
        )
        paths = snap["paths"]
        mismatches = git(
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            snap["commit"],
            parent,
            "--",
            *paths,
        )
        if any(
            project_snapshot_path(p.decode(), snap) is not None
            for p in mismatches.split(b"\0")
            if p
        ):
            return (
                None,
                "projected snapshot differs from the source commit's pre-fix tree",
            )
        # Two source paths must not collapse to one fixture path after stripping.
        projected: dict[str, str] = {}
        for revision in (parent, source_commit):
            for raw in git(
                "ls-tree", "-r", "--name-only", "-z", revision, "--", *paths
            ).split(b"\0"):
                if not raw:
                    continue
                path = raw.decode()
                rel = project_snapshot_path(path, snap)
                if rel is not None:
                    if rel in projected and projected[rel] != path:
                        return (
                            None,
                            "snapshot strip_components creates a path collision",
                        )
                    projected[rel] = path
        for overlay in snap.get("overlays", []):
            overlay_snap = {**snap, "paths": overlay["paths"]}
            for raw in git(
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                overlay["commit"],
                "--",
                *overlay["paths"],
            ).split(b"\0"):
                if not raw:
                    continue
                rel = project_snapshot_path(raw.decode(), overlay_snap)
                if rel in projected:
                    return None, "overlay changes the projected pre-fix snapshot"
        total = 0
        for raw in git(
            "diff",
            "--no-renames",
            "--numstat",
            "-z",
            parent,
            source_commit,
            "--",
            *paths,
        ).split(b"\0"):
            if not raw:
                continue
            added, removed, path = raw.decode().split("\t", 2)
            if (
                added == "-"
                or removed == "-"
                or project_snapshot_path(path, snap) is None
            ):
                continue
            total += int(added) + int(removed)
        if not total:
            return None, "source commit has no text fix in the projected snapshot"
        return total, None
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
        ValueError,
    ) as exc:
        return None, f"gold history unavailable: {type(exc).__name__}"


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
