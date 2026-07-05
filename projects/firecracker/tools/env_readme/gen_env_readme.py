"""Generate /etc/environment.md for a Firecracker guest image.

The package table is derived from the apko lock file, so the readme cannot
drift from the image it ships in (ADR agents/044). Hand-written context goes
in the per-guest notes file, never here.
"""

from __future__ import annotations

import argparse
import json
import pathlib


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--notes", required=True)
    args = ap.parse_args()

    lock = json.loads(pathlib.Path(args.lock).read_text())
    # apko locks list packages once per arch under contents.packages, each a
    # dict with at least "name" and "version" (plus per-arch fields we do not
    # need here). Dedupe across archs so a dual-arch image gets one row per
    # package.
    raw = lock.get("contents", {}).get("packages", [])
    rows = []
    for p in raw:
        if isinstance(p, dict):
            rows.append((p.get("name", "?"), p.get("version", "?")))
        else:
            name, _, version = str(p).rpartition("-")
            rows.append((name or str(p), version))

    print(f"# {args.title}")
    print()
    print(pathlib.Path(args.notes).read_text().rstrip())
    print()
    print("## Installed packages (from the image lock; exact and exhaustive)")
    print()
    print("| Package | Version |")
    print("| ------- | ------- |")
    for name, version in sorted(set(rows)):
        print(f"| {name} | {version} |")


if __name__ == "__main__":
    main()
