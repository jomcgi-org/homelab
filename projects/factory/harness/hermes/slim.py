"""Write a slim Hermes Agent root filesystem from the pinned upstream image.

The upstream image carries Chromium, FFmpeg, Node.js, a C toolchain and the
desktop and terminal UIs. With every Hermes toolset off, the gateway loads only
its Python package, its virtualenv and its Python interpreter. This tool reads
the image's OCI layout and writes two layer tars:

- base.tar: the image's first layer (the Debian root filesystem), unchanged.
- hermes.tar: the merged later layers, with whiteouts applied, cut to KEEP
  minus DROP.

Both tars list members in input order, so the same layout gives the same
bytes.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
import tarfile
from pathlib import Path

# Paths from the later layers that the gateway needs: Hermes, its interpreter
# and virtualenv, its image provenance, the CA bundle, and the SQLite build
# the image pins for its WAL fix.
KEEP = (
    "opt/hermes",
    "etc/hermes",
    "etc/ssl",
    "etc/ca-certificates",
    "etc/ca-certificates.conf",
    "usr/share/ca-certificates",
    "usr/local/bin/python3",
    "usr/local/lib",
    "etc/ld.so.conf.d/000-sqlite-fixed.conf",
)

# Parts of opt/hermes that only the browser, media, terminal, desktop and web
# UIs, Node plugins and lazy installs use.
DROP = (
    "opt/hermes/tools/chromium-",
    "opt/hermes/tools/ffmpeg-",
    "opt/hermes/tools/node-",
    "opt/hermes/tools/npm-",
    "opt/hermes/tools/uv-",
    "opt/hermes/tools/ripgrep-",
    "opt/hermes/apps/",
    "opt/hermes/node_modules/",
    "opt/hermes/ui-tui/",
    "opt/hermes/uv-cache/",
    "opt/hermes/web/",
    "opt/hermes/hermes_cli/web_dist/",
    "opt/hermes/hermes_cli/tui_dist/",
    "opt/hermes/plugins/platforms/photon/sidecar/node_modules/",
)


def _blob(layout: Path, digest: str) -> Path:
    algo, hex_digest = digest.split(":", 1)
    return layout / "blobs" / algo / hex_digest


def layers(layout: Path) -> list[Path]:
    """Return the image's layer blobs, lowest first."""
    index = json.loads((layout / "index.json").read_text())
    (entry,) = index["manifests"]
    manifest = json.loads(_blob(layout, entry["digest"]).read_text())
    if "manifests" in manifest:  # An image index: take its one platform.
        (entry,) = manifest["manifests"]
        manifest = json.loads(_blob(layout, entry["digest"]).read_text())
    return [_blob(layout, layer["digest"]) for layer in manifest["layers"]]


def _name(member: tarfile.TarInfo) -> str:
    return member.name.removeprefix("./").rstrip("/")


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + "/")


def wanted(name: str) -> bool:
    """Report whether a path of the later layers goes into hermes.tar."""
    if not any(_under(name, keep) for keep in KEEP):
        return False
    return not any(name.startswith(drop) or name + "/" == drop for drop in DROP)


def _winners(blobs: list[Path]) -> dict[str, int]:
    """Map each path in the merged later layers to the layer that wins it."""
    final: dict[str, int] = {}
    for index, blob in enumerate(blobs):
        with tarfile.open(blob, "r|*") as layer:
            for member in layer:
                name = _name(member)
                parent, _, base = name.rpartition("/")
                if base == ".wh..wh..opq":
                    gone = [k for k in final if k.startswith(parent + "/")]
                elif base.startswith(".wh."):
                    target = f"{parent}/{base[4:]}" if parent else base[4:]
                    gone = [k for k in final if _under(k, target)]
                else:
                    final[name] = index
                    continue
                for key in gone:
                    del final[key]
    return final


def write_hermes(blobs: list[Path], out: Path) -> int:
    """Write the kept paths of the later layers; return the member count."""
    winners = {k: v for k, v in _winners(blobs).items() if wanted(k)}
    written: set[str] = set()
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as tar:
        for index, blob in enumerate(blobs):
            with tarfile.open(blob, "r|*") as layer:
                for member in layer:
                    name = _name(member)
                    if winners.get(name) != index or name in written:
                        continue
                    if member.islnk():
                        target = member.linkname.removeprefix("./")
                        if target not in written:
                            sys.exit(f"{name}: hard link to unkept {target}")
                    if member.isdev():
                        sys.exit(f"{name}: device file in a later layer")
                    data = layer.extractfile(member) if member.isfile() else None
                    tar.addfile(member, data)
                    written.add(name)
    missing = sorted(set(winners) - written)
    if missing:
        sys.exit(f"paths not written: {missing[:10]}")
    return len(written)


def write_base(blob: Path, out: Path) -> None:
    """Write the first layer as an uncompressed tar."""
    with blob.open("rb") as raw:
        gzipped = raw.read(2) == b"\x1f\x8b"
    opener = gzip.open if gzipped else open
    with opener(blob, "rb") as src, out.open("wb") as dest:
        shutil.copyfileobj(src, dest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--hermes", type=Path, required=True)
    args = parser.parse_args()
    blobs = layers(args.layout)
    write_base(blobs[0], args.base)
    write_hermes(blobs[1:], args.hermes)


if __name__ == "__main__":
    main()
