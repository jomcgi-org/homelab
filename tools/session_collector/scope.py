"""Repository discovery and upload allowlist handling."""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_ALLOWLIST = {
    "jomcgi-org/homelab": "repo:jomcgi-org/homelab",
    "jomcgi/homelab": "repo:jomcgi-org/homelab",
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


def allowed_scope(repo: str | None, allowlist: dict[str, str]) -> str | None:
    return allowlist.get(repo or "")


def _remembered_repo(cwd: str, state: dict[str, dict[str, object]]) -> str | None:
    for entry in state.values():
        if entry.get("cwd") == cwd and isinstance(entry.get("repo"), str):
            return str(entry["repo"])
    return None


def discover_repo(cwd: str, state: dict[str, dict[str, object]]) -> str | None:
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
    if "homelab" in Path(cwd).parts:
        return "jomcgi-org/homelab"
    return None
