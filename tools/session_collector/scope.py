"""Repository discovery and upload allowlist handling."""

from __future__ import annotations

import subprocess
from functools import cache
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_ALLOWLIST = {
    "jomcgi-org/homelab": "repo:jomcgi-org/homelab",
    "jomcgi/homelab": "repo:jomcgi-org/homelab",
}

DEFAULT_PATH_ALLOWLIST = {
    Path("/tmp/claude-worktrees"): "jomcgi-org/homelab",
    Path("/private/tmp/claude-worktrees"): "jomcgi-org/homelab",
    Path("/tmp/codex-worktrees"): "jomcgi-org/homelab",
    Path.home() / "repos/ft-worktrees": "jomcgi-org/homelab",
    Path.home() / "repos/homelab": "jomcgi-org/homelab",
}


def normalize_origin(origin: str) -> str | None:
    value = origin.strip()
    if not value:
        return None
    if "://" in value:
        path = urlparse(value).path
    elif ":" in value and "@" in value.split(":", 1)[0]:
        path = value.split(":", 1)[1]
    else:
        path = value
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    repo = "/".join(parts[-2:])
    return repo.removesuffix(".git")


def parse_allowlist(values: list[str] | None) -> dict[str, str]:
    if not values:
        return dict(DEFAULT_ALLOWLIST)
    result: dict[str, str] = {}
    for value in values:
        repo, separator, scope = value.partition("=")
        if not separator or not repo or not scope:
            raise ValueError(f"invalid --allow value: {value}")
        result[repo] = scope
    return result


def parse_path_allowlist(values: list[str] | None) -> dict[Path, str]:
    if not values:
        return dict(DEFAULT_PATH_ALLOWLIST)
    result: dict[Path, str] = {}
    for value in values:
        prefix, separator, repo = value.partition("=")
        if not separator or not prefix or not repo:
            raise ValueError(f"invalid --allow-path value: {value}")
        result[Path(prefix).expanduser()] = repo
    return result


def allowed_scope(repo: str | None, allowlist: dict[str, str]) -> str | None:
    return allowlist.get(repo or "")


def _remembered_repo(cwd: str, state: dict[str, dict[str, object]]) -> str | None:
    for entry in state.values():
        if entry.get("cwd") == cwd and isinstance(entry.get("repo"), str):
            return str(entry["repo"])
    return None


@cache
def _load_homelab_worktrees() -> frozenset[Path]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(Path.home() / "repos/homelab"),
            "worktree",
            "list",
            "--porcelain",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return frozenset()
    return frozenset(
        Path(line.removeprefix("worktree ")).expanduser().resolve(strict=False)
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    )


def homelab_worktrees() -> frozenset[Path]:
    return _load_homelab_worktrees()


def reset_worktree_cache() -> None:
    _load_homelab_worktrees.cache_clear()


def discover_repo(
    cwd: str,
    state: dict[str, dict[str, object]],
    path_allowlist: dict[Path, str] | None = None,
    transcript_origin: str | None = None,
) -> str | None:
    repo = normalize_origin(transcript_origin or "")
    if repo:
        return repo
    directory = Path(cwd).expanduser()
    if cwd and directory.is_dir():
        result = subprocess.run(
            ["git", "-C", str(directory), "remote", "get-url", "origin"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            repo = normalize_origin(result.stdout)
            if repo:
                return repo
    remembered = _remembered_repo(cwd, state)
    if remembered:
        return remembered
    allowed_paths = DEFAULT_PATH_ALLOWLIST if path_allowlist is None else path_allowlist
    registered = homelab_worktrees()
    fallback_directory = directory.resolve(strict=False)
    for prefix, repo in sorted(
        allowed_paths.items(), key=lambda item: len(item[0].parts), reverse=True
    ):
        if fallback_directory.is_relative_to(
            prefix.expanduser().resolve(strict=False)
        ) and any(
            fallback_directory.is_relative_to(worktree.resolve(strict=False))
            for worktree in registered
        ):
            return repo
    return None
