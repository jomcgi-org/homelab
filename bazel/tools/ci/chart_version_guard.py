#!/usr/bin/env python3
"""Reject PRs that move a published chart's version or pinned targetRevision.

Usage:
    python3 bazel/tools/ci/chart_version_guard.py BASE_REF [HEAD_REF]

Since ADR platform/009 decision 1, the chart version is an output of merging:
main's publish computes it and `chart-version-bot` writes `version:` and
`targetRevision:` back (bazel/helm/write-back-versions.sh). A branch that edits
either line races the bot and, on the Kargo-owned Applications, fights the
revert lever. This guard enforces that in CI so it holds for every author,
human or agent, rather than only for whichever tool reads a hook.

Scope, so third-party pins are never caught:
  - `deploy/application.yaml` files whose sources include this repo's OCI
    chart registry: a changed semver `targetRevision:` is rejected.
  - `chart/Chart.yaml` files whose sibling `deploy/application.yaml` includes
    that registry: a changed top-level `version:` is rejected.
Added and deleted files are allowed, so a new service can seed its first
version. There is no command-line bypass.
"""

from __future__ import annotations

import argparse
from pathlib import PurePosixPath
import re
import subprocess
import sys
from typing import Callable, Sequence

REGISTRY = "ghcr.io/jomcgi/homelab/charts"
_VERSION = re.compile(r"^version:\s*[\"']?([^\"'\s]+)[\"']?\s*$", re.MULTILINE)
_SEMVER_TR = re.compile(
    r"^\s*targetRevision:\s*[\"']?(\d+\.\d+\.\d+)[\"']?\s*$", re.MULTILINE
)

Reader = Callable[[str, str], "str | None"]


def chart_version(text: str | None) -> str | None:
    if text is None:
        return None
    match = _VERSION.search(text)
    return match.group(1) if match else None


def pinned_revisions(text: str | None) -> list[str]:
    if text is None:
        return []
    return _SEMVER_TR.findall(text)


def uses_registry(text: str | None) -> bool:
    return text is not None and REGISTRY in text


def app_for_chart(chart_path: str) -> str | None:
    path = PurePosixPath(chart_path)
    if path.name != "Chart.yaml" or path.parent.name != "chart":
        return None
    return str(path.parent.parent / "deploy" / "application.yaml")


def findings(changed: Sequence[str], read: Reader, base: str, head: str) -> list[str]:
    """Return one diagnostic per forbidden change among `changed` paths."""
    out: list[str] = []
    for path in changed:
        before, after = read(base, path), read(head, path)
        if before is None or after is None:
            continue
        if path.endswith("/deploy/application.yaml"):
            if not (uses_registry(before) or uses_registry(after)):
                continue
            if pinned_revisions(before) != pinned_revisions(after):
                out.append(
                    f"{path}: targetRevision changed "
                    f"{pinned_revisions(before)} -> {pinned_revisions(after)}"
                )
        app = app_for_chart(path)
        if app is not None:
            if not (uses_registry(read(head, app)) or uses_registry(read(base, app))):
                continue
            if chart_version(before) != chart_version(after):
                out.append(
                    f"{path}: version changed "
                    f"{chart_version(before)} -> {chart_version(after)}"
                )
    return out


def _git_read(ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else None


def _changed(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", base, head],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base")
    parser.add_argument("head", nargs="?", default="HEAD")
    args = parser.parse_args(argv)

    problems = findings(_changed(args.base, args.head), _git_read, args.base, args.head)
    if not problems:
        return 0
    print(
        "Chart versions are written by chart-version-bot after merge "
        "(ADR platform/009). Revert these lines; the publish on main moves them:",
        file=sys.stderr,
    )
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
