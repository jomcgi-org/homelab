#!/usr/bin/env python3
"""Reject PRs that move a published chart's version forward on the branch.

Usage:
    python3 bazel/tools/ci/chart_version_guard.py BASE_REF [HEAD_REF]

Since ADR platform/009 decision 1, the chart version is an output of merging:
main's publish computes it and `chart-version-bot` writes `version:` and the
matching `targetRevision:` back (bazel/helm/write-back-versions.sh). A branch
that edits either line races the bot. This guard enforces that in CI so it
holds for every author, human or agent, rather than only for whichever tool
reads a hook.

Scope mirrors what the bot writes, so it never blocks a file the bot ignores:
  - A published chart is a directory holding `Chart.yaml` and a `BUILD` with
    `publish = True`. Any change to its top-level `version:` is rejected.
  - Its Application is found the way the bot finds it (`_app_yaml_for`):
    `<parent of chart dir>/deploy/application.yaml`, else
    `<chart dir>/application.yaml`. Raising a semver `targetRevision:` there is
    rejected. Lowering one is allowed, because a lowered pin is the revert
    lever and the bot never lowers a version. Adding or removing a pin (moving
    a service onto or off the registry) is allowed.
Added and deleted files are allowed, so a new service can seed its first
version.
"""

from __future__ import annotations

import argparse
from pathlib import PurePosixPath
import re
import subprocess
import sys
from typing import Callable, Iterable, Sequence

_VERSION = re.compile(r"^version:\s*[\"']?([^\"'\s]+)[\"']?\s*$", re.MULTILINE)
_SEMVER_TR = re.compile(
    r"^\s*targetRevision:\s*[\"']?(\d+\.\d+\.\d+)[\"']?\s*$", re.MULTILINE
)
_PUBLISH = re.compile(r"^\s*publish\s*=\s*True\b", re.MULTILINE)

Reader = Callable[[str, str], "str | None"]


def chart_version(text: str | None) -> str | None:
    if text is None:
        return None
    match = _VERSION.search(text)
    return match.group(1) if match else None


def pinned_revisions(text: str | None) -> list[tuple[int, ...]]:
    if text is None:
        return []
    return [tuple(int(p) for p in v.split(".")) for v in _SEMVER_TR.findall(text)]


def _fmt(versions: list[tuple[int, ...]]) -> list[str]:
    return [".".join(map(str, v)) for v in versions]


def published_chart_dirs(paths: Iterable[str], read: Reader, ref: str) -> set[str]:
    """Chart directories at `ref` whose BUILD publishes them."""
    out: set[str] = set()
    for path in paths:
        p = PurePosixPath(path)
        if p.name != "Chart.yaml":
            continue
        build = read(ref, str(p.parent / "BUILD")) or read(
            ref, str(p.parent / "BUILD.bazel")
        )
        if build and _PUBLISH.search(build):
            out.add(str(p.parent))
    return out


def app_yaml_for(chart_dir: str, exists: Callable[[str], bool]) -> str | None:
    """Mirror of `_app_yaml_for` in bazel/helm/write-back-versions.sh."""
    d = PurePosixPath(chart_dir)
    for candidate in (d.parent / "deploy" / "application.yaml", d / "application.yaml"):
        if exists(str(candidate)):
            return str(candidate)
    return None


def findings(
    changed: Sequence[str],
    published: set[str],
    read: Reader,
    base: str,
    head: str,
) -> list[str]:
    """Return one diagnostic per forbidden change among `changed` paths."""
    apps = {}
    for chart_dir in published:
        app = app_yaml_for(
            chart_dir,
            lambda p: read(head, p) is not None or read(base, p) is not None,
        )
        if app is not None:
            apps[app] = chart_dir

    out: list[str] = []
    for path in changed:
        before, after = read(base, path), read(head, path)
        if before is None or after is None:
            continue
        p = PurePosixPath(path)
        if p.name == "Chart.yaml" and str(p.parent) in published:
            if chart_version(before) != chart_version(after):
                out.append(
                    f"{path}: version changed "
                    f"{chart_version(before)} -> {chart_version(after)}"
                )
        if path in apps:
            old, new = pinned_revisions(before), pinned_revisions(after)
            if len(old) == len(new) and any(n > o for o, n in zip(old, new)):
                out.append(f"{path}: targetRevision raised {_fmt(old)} -> {_fmt(new)}")
    return out


def _git_read(ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else None


def _git_lines(*args: str) -> list[str]:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base")
    parser.add_argument("head", nargs="?", default="HEAD")
    args = parser.parse_args(argv)

    changed = _git_lines("diff", "--name-only", args.base, args.head)
    if not changed:
        return 0
    published = set()
    for ref in (args.base, args.head):
        charts = _git_lines("ls-tree", "-r", "--name-only", ref)
        published |= published_chart_dirs(charts, _git_read, ref)

    problems = findings(changed, published, _git_read, args.base, args.head)
    if not problems:
        return 0
    print(
        "Published chart versions are written by chart-version-bot after merge "
        "(ADR platform/009). Revert these lines; the publish on main moves them:",
        file=sys.stderr,
    )
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
