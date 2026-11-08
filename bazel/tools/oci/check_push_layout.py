#!/usr/bin/env python3
"""Check an OCI push's runfiles under bazel run --run_under, without pushing."""

import json
from pathlib import Path
import re
import sys


def check_layout(layout):
    layout = Path(layout)
    print(f"OCI layout: {layout} -> {layout.resolve()}")
    pending = [
        (item, True)
        for item in json.loads((layout / "index.json").read_text())["manifests"]
    ]
    seen = set()
    failures = []
    while pending:
        descriptor, manifest = pending.pop()
        digest = descriptor["digest"]
        if digest in seen:
            continue
        seen.add(digest)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            failures.append(f"Unsupported blob digest: {digest}")
            continue
        blob = layout / "blobs" / digest.replace(":", "/")
        try:
            with blob.open("rb") as stream:
                content = stream.read() if manifest else stream.read(1)
            if blob.stat().st_size != descriptor["size"]:
                raise ValueError(f"size {blob.stat().st_size} != {descriptor['size']}")
            if manifest:
                document = json.loads(content)
                if "manifests" in document:
                    pending.extend((item, True) for item in document["manifests"])
                else:
                    pending.append((document["config"], False))
                    pending.extend((item, False) for item in document["layers"])
        except (OSError, ValueError, KeyError) as exc:
            link = str(blob.readlink()) if blob.is_symlink() else "not a symlink"
            failures.append(
                f"Unreadable OCI blob {digest}: {exc}; link={link}; resolved={blob.resolve()}"
            )
    if failures:
        raise ValueError("\n".join(failures))
    print(f"Verified {len(seen)} referenced OCI blobs; no push executed")


def main(args):
    # Bazel appends the push executable and its arguments. Never execute them.
    try:
        check_layout(args[0])
    except (OSError, ValueError, KeyError, IndexError) as exc:
        print(f"OCI push layout check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
