"""Fetch the microVM sandbox assets and tools that the asset bucket still lacks.

Runs in the microvm-assets-init Job between the key listing and the upload
(snapshot-store.yaml's sibling, microvm-assets.yaml). It reads assets.json,
skips every asset and tool whose key is already in /work/existing.txt,
downloads the rest from the official releases, checks each against its pinned
sha256 and leaves the verified bytes at /work/assets/<sha256>. The tools are
the CLIs that one-off Jobs run, so those Jobs need no internet access. It also writes
/work/expected.txt, the key of every pinned asset, for the upload step's final
check. A hash mismatch fails the Job before anything reaches the bucket.

Needs Python 3.14 (compression.zstd through tarfile).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

CONFIG = Path(os.environ.get("ASSETS_CONFIG", "/config/assets.json"))
WORK = Path(os.environ.get("WORK_DIR", "/work"))
CHUNK = 1 << 20
MAX_BYTES = 2 << 30
ATTEMPTS = 3


def key_for(cfg: dict, sha: str) -> str:
    return f"{cfg['prefix']}/{cfg['arch']}/{sha}"


def copy_hashed(src, dst, expected: str, name: str) -> None:
    """Write src to dst, then fail unless its sha256 is expected."""
    digest = hashlib.sha256()
    size = 0
    with open(dst, "wb") as out:
        while chunk := src.read(CHUNK):
            size += len(chunk)
            if size > MAX_BYTES:
                raise SystemExit(f"{name}: larger than {MAX_BYTES} bytes")
            digest.update(chunk)
            out.write(chunk)
    got = digest.hexdigest()
    if got != expected:
        os.unlink(dst)
        raise SystemExit(f"{name}: sha256 {got}, pinned {expected}")
    print(f"{name}: {size} bytes, sha256 {got}", flush=True)


def open_url(url: str):
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return urllib.request.urlopen(url, timeout=60)
        except OSError as err:
            print(f"GET {url}: {err} ({attempt}/{ATTEMPTS})", flush=True)
            if attempt == ATTEMPTS:
                raise
            time.sleep(5 * attempt)


def publish(tmp: Path, sha: str) -> None:
    tmp.rename(WORK / "assets" / sha)


def fetch_direct(name: str, spec: dict) -> None:
    sha = spec["sha256"]
    tmp = WORK / "assets" / f".{name}.part"
    with open_url(spec["url"]) as resp:
        copy_hashed(resp, tmp, sha, name)
    publish(tmp, sha)


def fetch_from_kata(url: str, wanted: dict[str, tuple[str, str]]) -> None:
    """Stream the kata-static tarball and keep only the members in wanted.

    wanted maps a tar member path to (asset name, sha256). The kata symlinks
    the asset set names (vmlinux.container, kata-containers.img) resolve to
    the member paths in assets.json; the sha256 check covers any drift.
    """
    left = dict(wanted)
    with open_url(url) as resp, tarfile.open(fileobj=resp, mode="r|zst") as tar:
        for member in tar:
            name = member.name.removeprefix("./")
            if name not in left or not member.isreg():
                continue
            asset, sha = left.pop(name)
            tmp = WORK / "assets" / f".{asset}.part"
            src = tar.extractfile(member)
            assert src is not None
            copy_hashed(src, tmp, sha, asset)
            publish(tmp, sha)
            if not left:
                return
    raise SystemExit(f"not in the kata archive: {sorted(left)}")


def main() -> None:
    cfg = json.loads(CONFIG.read_text())
    existing = set((WORK / "existing.txt").read_text().split())
    assets = {**cfg["assets"], **cfg.get("tools", {})}
    (WORK / "expected.txt").write_text(
        "".join(key_for(cfg, a["sha256"]) + "\n" for a in assets.values())
    )
    shutil.rmtree(WORK / "assets", ignore_errors=True)
    (WORK / "assets").mkdir()

    missing = {
        name: spec
        for name, spec in assets.items()
        if key_for(cfg, spec["sha256"]) not in existing
    }
    for name in assets.keys() - missing.keys():
        print(f"{name}: already in the bucket", flush=True)
    if not missing:
        return

    for name, spec in missing.items():
        if "url" in spec:
            fetch_direct(name, spec)
    from_kata = {
        spec["kata"]: (name, spec["sha256"])
        for name, spec in missing.items()
        if "kata" in spec
    }
    if from_kata:
        fetch_from_kata(cfg["kata"]["url"], from_kata)


if __name__ == "__main__":
    sys.exit(main())
