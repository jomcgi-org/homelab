"""Snapshot materialization and guest diff application."""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class MissingCommitError(RuntimeError):
    """Raised when the local source checkout lacks a task snapshot commit."""


@dataclass
class ApplyResult:
    applied: bool
    out_of_scope_files: list[str]
    error: str = ""


def ensure_snapshot_commit(repo: Path, snapshot: dict) -> None:
    """Fail clearly before a probe run when its pinned commit is unavailable."""
    commit = snapshot.get("commit")
    if not commit:
        raise ValueError("snapshot needs a commit")
    present = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if present.returncode != 0:
        raise MissingCommitError(
            f"snapshot commit {commit} is missing from {repo}; run git fetch first"
        )


def materialize_fixture(repo: Path, snapshot: dict, destination: Path) -> None:
    """Extract a task snapshot exactly like ``bench.cli._snapshot``."""
    commit = snapshot.get("commit")
    paths = snapshot.get("paths", [])
    if not commit or not paths:
        raise ValueError("snapshot needs commit and paths")
    ensure_snapshot_commit(repo, snapshot)

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", str(commit), "--", *paths],
        capture_output=True,
        check=True,
        timeout=120,
    )
    tar_cmd = ["tar", "-x", "-C", str(destination)]
    strip = int(snapshot.get("strip_components", 0) or 0)
    if strip:
        tar_cmd.append(f"--strip-components={strip}")
    subprocess.run(tar_cmd, input=archive.stdout, check=True, timeout=120)

    excludes = snapshot.get("exclude", ["*_test.py"])
    dir_excludes = {entry.rstrip("/") for entry in excludes if entry.endswith("/")}
    name_globs = [entry for entry in excludes if not entry.endswith("/")]
    for path in sorted(destination.rglob("*"), reverse=True):
        if path.is_file():
            parents = set(path.relative_to(destination).parts[:-1])
            if (dir_excludes & parents) or any(
                fnmatch.fnmatch(path.name, pattern) for pattern in name_globs
            ):
                path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def _diff_blocks(diff: str) -> list[tuple[str, str]]:
    starts = [match.start() for match in re.finditer(r"(?m)^diff --git ", diff)]
    blocks: list[tuple[str, str]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(diff)
        block = diff[start:end]
        match = re.search(r"(?m)^\+\+\+ (?:b/)?([^\t\n]+)", block)
        if match and match.group(1) != "/dev/null":
            path = match.group(1)
        else:
            match = re.search(r"(?m)^--- (?:a/)?([^\t\n]+)", block)
            path = match.group(1) if match else ""
        blocks.append((path, block))
    return blocks


def _inside(path: str, root: str) -> bool:
    normalized_path = path.strip("/")
    normalized_root = root.strip("/")
    return normalized_path == normalized_root or normalized_path.startswith(
        normalized_root + "/"
    )


def _rewrite_path(path: str, strip_components: int) -> str:
    parts = Path(path).parts
    if len(parts) <= strip_components:
        return ""
    return Path(*parts[strip_components:]).as_posix()


def is_in_scope(
    path: str, snapshot: dict, extra_files: list[str] | None = None
) -> bool:
    """Return whether a repo-root path is eligible for fixture application."""
    roots = [str(root) for root in snapshot.get("paths", [])]
    extras = [str(extra) for extra in (extra_files or [])]
    return any(_inside(path, root) for root in roots) or path in extras


def apply_guest_diff(
    fixture_dir: Path,
    diff: str,
    snapshot: dict,
    *,
    extra_files: list[str] | None = None,
    changed_files: list[str] | None = None,
) -> ApplyResult:
    """Apply in-scope diff blocks to a materialized fixture."""
    snapshot_paths = [str(path) for path in snapshot.get("paths", [])]
    extras = [str(path) for path in (extra_files or [])]
    blocks = _diff_blocks(diff)
    observed_files = (
        changed_files
        if changed_files is not None
        else [path for path, _ in blocks if path]
    )
    out_of_scope = sorted(
        path for path in observed_files if not is_in_scope(path, snapshot, extras)
    )
    selected = [block for path, block in blocks if is_in_scope(path, snapshot, extras)]
    if not selected:
        return ApplyResult(True, out_of_scope)

    strip = int(snapshot.get("strip_components", 0) or 0)
    includes = []
    for root in snapshot_paths:
        rewritten = _rewrite_path(root, strip)
        if rewritten:
            includes.append(f"{rewritten}/*")
    for path in extras:
        rewritten = _rewrite_path(path, strip)
        if rewritten:
            includes.append(rewritten)

    command = ["git", "apply", f"-p{1 + strip}"]
    command.extend(f"--include={pattern}" for pattern in includes)
    applied = subprocess.run(
        command,
        cwd=fixture_dir,
        input="".join(selected),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if applied.returncode != 0:
        message = (applied.stderr or applied.stdout).strip()
        return ApplyResult(False, out_of_scope, message)
    return ApplyResult(True, out_of_scope)


def remove_fixture(path: Path) -> None:
    """Remove a temporary fixture tree."""
    shutil.rmtree(path, ignore_errors=True)
