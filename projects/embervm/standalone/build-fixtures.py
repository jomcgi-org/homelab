#!/usr/bin/env python3
"""Build deterministic zip fixtures consumed by the standalone Workloads."""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

EXPECTED_CONTINUITY_SHA256 = (
    "bd12054be9fc385984157d27bba1847ab2ceb4a8167db26b3d0c0b99784f31d9"
)


def build(source: Path, output: Path) -> str:
    info = zipfile.ZipInfo(filename="app.py", date_time=(2000, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o644 << 16
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(info, source.read_bytes())
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent / "functions" / "continuity" / "app.py"
    digest = build(source, args.output)
    print(digest)
    if args.check and digest != EXPECTED_CONTINUITY_SHA256:
        print(f"expected {EXPECTED_CONTINUITY_SHA256}, got {digest}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
