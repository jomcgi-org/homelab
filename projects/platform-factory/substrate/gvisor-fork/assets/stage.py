"""Fetch the gVisor fork build and repack it as the SandboxConfig tarball.

Runs in the gvisor-fork-assets-init Job between the key listing and the upload
(gvisor-assets-job.yaml). It reads assets.json and does nothing when the
repacked tarball is already in the bucket. Otherwise it:

1. downloads the CI artifact `runsc-release.tar.gz` of the pinned fork build
   and checks its sha256;
2. checks every member against the pinned member list (no extra, no missing,
   regular files and directories only);
3. writes the members, without the `release/` prefix, into a plain tar with
   fixed order, owner, mode and time, so the bytes are the same on every run;
4. checks the sha256 of that tar and leaves it at /work/assets/<sha256>.tar.

atelet wants `runsc` and `gvisor-bin/` at the top of the archive and picks the
archive format from the key's suffix, so the key ends in `.tar`. A mismatch at
any step fails the Job before anything reaches the bucket.

Python 3.12 or newer (standard library only).
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
from pathlib import Path, PurePosixPath

CONFIG = Path(os.environ.get("ASSETS_CONFIG", "/config/assets.json"))
WORK = Path(os.environ.get("WORK_DIR", "/work"))
CHUNK = 1 << 20
MAX_BYTES = 1 << 30
ATTEMPTS = 3


def key_for(cfg: dict, asset: dict) -> str:
    return f"{cfg['prefix']}/{cfg['arch']}/{asset['sha256']}{asset['suffix']}"


def open_url(url: str):
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return urllib.request.urlopen(url, timeout=60)
        except OSError as err:
            print(f"GET {url}: {err} ({attempt}/{ATTEMPTS})", flush=True)
            if attempt == ATTEMPTS:
                raise
            time.sleep(5 * attempt)


def hash_stream(src) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := src.read(CHUNK):
        size += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), size


def download(url: str, dst: Path, expected: str) -> None:
    """Write url to dst, then fail unless its sha256 is expected."""
    digest = hashlib.sha256()
    size = 0
    with open_url(url) as resp, open(dst, "wb") as out:
        while chunk := resp.read(CHUNK):
            size += len(chunk)
            if size > MAX_BYTES:
                raise SystemExit(f"{url}: larger than {MAX_BYTES} bytes")
            digest.update(chunk)
            out.write(chunk)
    got = digest.hexdigest()
    if got != expected:
        dst.unlink()
        raise SystemExit(f"build archive: sha256 {got}, pinned {expected}")
    print(f"build archive: {size} bytes, sha256 {got}", flush=True)


def checked_members(archive: tarfile.TarFile, prefix: str, pins: dict) -> dict:
    """Map each stripped member path to its TarInfo, after checking all of them."""
    found: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        if member.isdir() and member.name.rstrip("/") + "/" == prefix:
            continue  # the prefix directory itself
        if not member.name.startswith(prefix):
            raise SystemExit(f"member outside {prefix}: {member.name!r}")
        name = member.name.removeprefix(prefix).rstrip("/")
        if not name:
            raise SystemExit(f"unsafe member path: {member.name!r}")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe member path: {member.name!r}")
        if member.isdir():
            found[name] = member
            continue
        if not member.isreg():
            raise SystemExit(f"member is not a regular file or directory: {name!r}")
        if name not in pins:
            raise SystemExit(f"member not in the pinned list: {name!r}")
        src = archive.extractfile(member)
        assert src is not None
        got, _ = hash_stream(src)
        if got != pins[name]:
            raise SystemExit(f"{name}: sha256 {got}, pinned {pins[name]}")
        found[name] = member
    missing = sorted(set(pins) - set(found))
    if missing:
        raise SystemExit(f"pinned members missing from the archive: {missing}")
    return found


def repack(archive: tarfile.TarFile, members: dict, dst: Path) -> None:
    """Write the members to a plain tar whose bytes depend only on their content."""
    with tarfile.open(dst, "w", format=tarfile.USTAR_FORMAT) as out:
        for name in sorted(members):
            member = members[name]
            info = tarfile.TarInfo(name)
            info.mode = member.mode & 0o777
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if member.isdir():
                info.type = tarfile.DIRTYPE
                out.addfile(info)
                continue
            info.size = member.size
            src = archive.extractfile(member)
            assert src is not None
            out.addfile(info, src)


def main() -> None:
    cfg = json.loads(CONFIG.read_text())
    asset = cfg["assets"]["gvisor"]
    existing = set((WORK / "existing.txt").read_text().split())
    key = key_for(cfg, asset)
    (WORK / "expected.txt").write_text(key + "\n")
    shutil.rmtree(WORK / "assets", ignore_errors=True)
    (WORK / "assets").mkdir()
    if key in existing:
        print(f"gvisor: {key} is already in the bucket", flush=True)
        return

    source = cfg["source"]
    src_path = WORK / "source.tar.gz"
    download(source["url"], src_path, source["sha256"])
    tmp = WORK / "assets" / ".gvisor.part"
    with tarfile.open(src_path, "r:gz") as archive:
        members = checked_members(archive, source["strip"], cfg["members"])
        repack(archive, members, tmp)
    src_path.unlink()
    with open(tmp, "rb") as repacked:
        got, size = hash_stream(repacked)
    if got != asset["sha256"]:
        tmp.unlink()
        raise SystemExit(f"repacked tar: sha256 {got}, pinned {asset['sha256']}")
    tmp.rename(WORK / "assets" / f"{got}{asset['suffix']}")
    print(f"gvisor: {size} bytes, sha256 {got}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
