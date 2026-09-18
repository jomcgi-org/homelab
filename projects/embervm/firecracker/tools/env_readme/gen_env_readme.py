"""Generate the committed environment.md for a Firecracker guest image.

The package table is derived from the apko lock file, so the readme cannot
drift from the image it ships in (ADR agents/044). Hand-written context goes
in the per-guest notes file, never here. Invoked from run-generators.sh (the
same pipeline as the docs manifests), so a lock change regenerates the file
and CI's format job auto-commits any drift; the guest BUILD just tars the
committed file into the image at /etc/environment.md.
"""

from __future__ import annotations

import argparse
import json
import pathlib


def render(lock_path: str, title: str, notes_path: str) -> str:
    lock = json.loads(pathlib.Path(lock_path).read_text())
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

    lines = [
        f"# {title}",
        "",
        pathlib.Path(notes_path).read_text().rstrip(),
        "",
        "## Installed packages (from the image lock; exact and exhaustive)",
        "",
        "| Package | Version |",
        "| ------- | ------- |",
    ]
    for name, version in sorted(set(rows)):
        lines.append(f"| {name} | {version} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--notes", required=True)
    ap.add_argument("--out", help="write here instead of stdout")
    args = ap.parse_args()

    content = render(args.lock, args.title, args.notes)
    if args.out:
        pathlib.Path(args.out).write_text(content)
    else:
        print(content, end="")


if __name__ == "__main__":
    main()
