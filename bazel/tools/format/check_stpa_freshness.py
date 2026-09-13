#!/usr/bin/env python3
"""Report whether committed STPA reviews predate changes in their system."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


STAMP_RE = re.compile(
    r"^_(?P<body>[a-z][a-z0-9_-]* @ [0-9a-f]{7,40}"
    r"(?: · [a-z][a-z0-9_-]* @ [0-9a-f]{7,40})*)_$"
)
ENTRY_RE = re.compile(r"(?P<lens>[a-z][a-z0-9_-]*) @ (?P<revision>[0-9a-f]{7,40})")


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def metadata_entries(stpa_path: Path) -> list[tuple[str, str]]:
    try:
        lines = stpa_path.read_text(encoding="utf-8").splitlines()[:20]
    except (OSError, UnicodeError):
        return []

    for line in lines:
        match = STAMP_RE.fullmatch(line)
        if match:
            return [
                (entry.group("lens"), entry.group("revision"))
                for entry in ENTRY_RE.finditer(match.group("body"))
            ]
    return []


def covered_changes(
    repo: Path, revision: str, system_dir: Path, stpa_relative: Path
) -> tuple[list[str] | None, str | None]:
    resolved = git(repo, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    if resolved.returncode != 0:
        shallow = git(repo, "rev-parse", "--is-shallow-repository")
        if shallow.returncode == 0 and shallow.stdout.strip() == "true":
            return None, "review revision is unavailable in shallow/incomplete history"
        return None, "review revision is unavailable or ambiguous"

    full_revision = resolved.stdout.strip()
    ancestor = git(repo, "merge-base", "--is-ancestor", full_revision, "HEAD")
    if ancestor.returncode == 1:
        return None, "review revision is not an ancestor of HEAD"
    if ancestor.returncode != 0:
        detail = ancestor.stderr.strip() or "git could not compare revisions"
        return None, detail

    system = system_dir.as_posix()
    history = git(
        repo,
        "log",
        "--format=",
        "--name-only",
        f"{full_revision}..HEAD",
        "--",
        system,
        f":(exclude){stpa_relative.as_posix()}",
        f":(exclude,glob){system}/stpa/**",
    )
    if history.returncode != 0:
        detail = history.stderr.strip() or "git history query failed"
        return None, detail

    return sorted({line for line in history.stdout.splitlines() if line}), None


def check(repo: Path) -> int:
    root = git(repo, "rev-parse", "--show-toplevel")
    if root.returncode != 0:
        detail = root.stderr.strip() or "not a git repository"
        print(f"STPA FRESHNESS: UNKNOWN: {detail}")
        return 2
    repo = Path(root.stdout.strip())

    tracked = git(repo, "ls-files", "-z", "--", ":(glob)**/STPA.md")
    if tracked.returncode != 0:
        detail = tracked.stderr.strip() or "could not list tracked STPA files"
        print(f"STPA FRESHNESS: UNKNOWN: {detail}")
        return 2

    stpa_files = sorted(Path(path) for path in tracked.stdout.split("\0") if path)
    print(f"STPA FRESHNESS: checking {len(stpa_files)} covered system(s)")
    print(
        "STPA FRESHNESS: directories without a tracked <system>/STPA.md are "
        "intentionally not evaluated"
    )

    stale = 0
    current = 0
    unknown = 0
    for relative in stpa_files:
        system_dir = relative.parent
        entries = metadata_entries(repo / relative)
        if not entries:
            unknown += 1
            print(
                f"STPA FRESHNESS: UNKNOWN {relative}: metadata header is missing or "
                "malformed; expected '_logic @ <commit>_'"
            )
            continue

        seen_lenses: set[str] = set()
        for lens, revision in entries:
            if lens in seen_lenses:
                unknown += 1
                print(f"STPA FRESHNESS: UNKNOWN {relative}: duplicate {lens} metadata")
                continue
            seen_lenses.add(lens)

            paths, error = covered_changes(repo, revision, system_dir, relative)
            label = f"{relative} [{lens} @ {revision}]"
            if error is not None:
                unknown += 1
                print(f"STPA FRESHNESS: UNKNOWN {label}: {error}")
            elif paths:
                stale += 1
                print(
                    f"STPA FRESHNESS: STALE {label}: {len(paths)} covered file(s) "
                    "changed after this review"
                )
                for path in paths[:5]:
                    print(f"  changed: {path}")
                if len(paths) > 5:
                    print(f"  changed: ... and {len(paths) - 5} more")
                print(f"  REVIEW NUDGE: refresh the STPA for {system_dir}")
            else:
                current += 1
                print(f"STPA FRESHNESS: UP TO DATE {label}")

    print(
        f"STPA FRESHNESS: summary: {stale} stale, {current} up to date, "
        f"{unknown} unknown"
    )
    if unknown:
        return 2
    if stale:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="git checkout to inspect (default: current directory)",
    )
    args = parser.parse_args()
    try:
        return check(args.repo)
    except Exception as error:
        print(f"STPA FRESHNESS: UNKNOWN: checker error: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
