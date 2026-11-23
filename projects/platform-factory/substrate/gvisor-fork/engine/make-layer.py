"""Make the one image layer of the engine image: layer.tar.

    python3 -I make-layer.py WHEEL BIN_DIR OUT_TAR

WHEEL is the Semgrep OSS wheel of pins.env (its sha256 is checked here), BIN_DIR
holds forksrv-net and shim.so from compile.sh. The tar is byte-reproducible:
sorted names, owner 0:0, mtime 0, fixed modes. It holds:

  /semgrep/semgrep-core, /semgrep/libs/*   from the wheel, untouched
  /semgrep/shim.so, /usr/local/bin/forksrv-net
  /usr/bin/uname                           semgrep-core runs `uname -s` at start;
                                           the base image has no coreutils
  /semgrep/rules.json, /semgrep/targets.json   the 40 regex rules, one target
  /scan/target.json                        a 1 MiB placeholder: the child's
                                           request body replaces it at open time
  /semgrep/NOTICE
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREFIX = "semgrep-{version}.data/purelib/semgrep/bin/"
PLACEHOLDER_BYTES = 1 << 20

NOTICE = """\
Semgrep OSS engine {version} (semgrep-core and its libraries), unmodified, from
the PyPI wheel {wheel} (sha256 {sha}).
License: LGPL-2.1-or-later. Source: https://github.com/semgrep/semgrep
(tag v{version}).

This image holds no Semgrep Pro engine, no token and no proprietary rule. The
40 rules in /semgrep/rules.json are secret-shaped regular expressions written
for the gVisor sandbox fork tests (jomcgi-org/homelab,
projects/platform-factory/substrate/gvisor-fork/engine/rules.json).

/usr/local/bin/forksrv-net and /semgrep/shim.so are the listen-after-fork
server of jomcgi-org/homelab (same directory), and /usr/bin/uname is its small
stand-in for uname(1). They are not part of Semgrep.
"""


def read_pins() -> dict[str, str]:
    pins = {}
    for line in (HERE / "pins.env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            pins[key] = value
    return pins


def entry(name: str, mode: int, *, directory: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = mode
    if directory:
        info.type = tarfile.DIRTYPE
    return info


def main(wheel: str, bin_dir: str, out: str) -> None:
    pins = read_pins()
    wheel_bytes = Path(wheel).read_bytes()
    sha = hashlib.sha256(wheel_bytes).hexdigest()
    if sha != pins["WHEEL_SHA256"]:
        sys.exit(f"wheel sha256 {sha} is not the pinned {pins['WHEEL_SHA256']}")
    prefix = PREFIX.format(version=pins["SEMGREP_VERSION"])
    files: dict[str, tuple[int, bytes]] = {}
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as zf:
        core = prefix + "semgrep-core"
        files["semgrep/semgrep-core"] = (0o755, zf.read(core))
        libs = [n for n in zf.namelist() if n.startswith(prefix + "libs/") and not n.endswith("/")]
        if not libs:
            sys.exit("the wheel has no libs/ directory")
        for name in libs:
            files["semgrep/libs/" + name[len(prefix + "libs/") :]] = (0o755, zf.read(name))
    bins = Path(bin_dir)
    files["semgrep/shim.so"] = (0o755, (bins / "shim.so").read_bytes())
    files["usr/local/bin/forksrv-net"] = (0o755, (bins / "forksrv-net").read_bytes())
    files["usr/bin/uname"] = (0o755, (bins / "uname").read_bytes())
    files["semgrep/rules.json"] = (0o644, (HERE / "rules.json").read_bytes())
    files["semgrep/targets.json"] = (0o644, (HERE / "targets.json").read_bytes())
    files["scan/target.json"] = (0o644, b"x" * PLACEHOLDER_BYTES)
    notice = NOTICE.format(version=pins["SEMGREP_VERSION"], wheel=pins["WHEEL_FILE"], sha=sha)
    files["semgrep/NOTICE"] = (0o644, notice.encode())

    dirs = {""}
    for name in files:
        parts = name.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    dirs.discard("")
    # usr and usr/local may exist in the base; a layer directory entry only
    # sets mode and owner, and 0755 root is what the base has.
    with tarfile.open(out, "w", format=tarfile.GNU_FORMAT) as tar:
        for name in sorted(dirs | set(files)):
            if name in files:
                mode, data = files[name]
                info = entry(name, mode)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(entry(name, 0o755, directory=True))
    digest = hashlib.sha256(Path(out).read_bytes()).hexdigest()
    print(f"layer {out} sha256:{digest}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
