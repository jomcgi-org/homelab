"""Make the one image layer of the Pro engine image: layer.tar.

    python3 -I make-layer.py WHEEL BIN_DIR FETCH_DIR OUT_TAR

WHEEL is the Semgrep OSS wheel of ../engine/pins.env (its sha256 is checked here; the
Pro engine needs its libs/), BIN_DIR holds forksrv-net, uname and shim.so from
compile.sh, FETCH_DIR holds semgrep-core-proprietary, rules.json and fetch.json from
fetch-pro.py. The layer maker never sees the token and refuses to run when
SEMGREP_APP_TOKEN is in its environment. Given the same inputs the tar is
byte-identical: sorted names, owner 0:0, mtime 0, fixed modes. It holds:

  /semgrep/semgrep-core-proprietary          the Pro engine, as downloaded
  /semgrep/libs/*                            from the wheel, untouched
  /semgrep/shim.so, /usr/local/bin/forksrv-net
  /usr/bin/uname                             the engine runs `uname -s` at start
  /semgrep/rules.json                        the Secrets rules, as downloaded
  /semgrep/targets.json, /scan/target.json   one target; a 1 MiB placeholder
  /semgrep/.tok                              the PLACEHOLDER token, mode 0444
  /semgrep/BUILD-INFO.json, /semgrep/NOTICE  input digests; what the image is
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WHEEL_PREFIX = "semgrep-{version}.data/purelib/semgrep/bin/"
PLACEHOLDER_BYTES = 1 << 20

NOTICE = """\
PRIVATE IMAGE. Do not publish it, copy it out of the private package or share it.

This image holds the Semgrep Pro engine {version} (semgrep-core-proprietary,
platform {platform}) and the Secrets rules of one Semgrep deployment. Both are
Semgrep's proprietary software and data, downloaded at build time with a token
of that deployment. The terms that cover them are an open item of the owner of
this repository (jomcgi-org/homelab,
projects/platform-factory/substrate/gvisor-fork/engine-pro/README.md).

The real token is not in this image. /semgrep/.tok holds a placeholder: the
engine checks only that a token is present when it starts.

/semgrep/libs is from the Semgrep OSS wheel {wheel} (sha256 {sha}),
LGPL-2.1-or-later, https://github.com/semgrep/semgrep (tag v{version}).
/usr/local/bin/forksrv-net, /semgrep/shim.so and /usr/bin/uname are the
listen-after-fork server of jomcgi-org/homelab (directories engine and
engine-pro). They are not part of Semgrep.
"""


def read_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for path in (HERE.parent / "engine" / "pins.env", HERE / "pins.env"):
        for line in path.read_text().splitlines():
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


def main(wheel: str, bin_dir: str, fetch_dir: str, out: str) -> None:
    if os.environ.get("SEMGREP_APP_TOKEN"):
        sys.exit(
            "make-layer: SEMGREP_APP_TOKEN is set; the layer build runs without it"
        )
    pins = read_pins()
    wheel_bytes = Path(wheel).read_bytes()
    sha = hashlib.sha256(wheel_bytes).hexdigest()
    if sha != pins["WHEEL_SHA256"]:
        sys.exit(f"wheel sha256 {sha} is not the pinned {pins['WHEEL_SHA256']}")
    prefix = WHEEL_PREFIX.format(version=pins["SEMGREP_VERSION"])
    files: dict[str, tuple[int, bytes]] = {}
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as zf:
        libs = [
            n
            for n in zf.namelist()
            if n.startswith(prefix + "libs/") and not n.endswith("/")
        ]
        if not libs:
            sys.exit("the wheel has no libs/ directory")
        for name in libs:
            files["semgrep/libs/" + name[len(prefix + "libs/") :]] = (
                0o755,
                zf.read(name),
            )

    fetched = Path(fetch_dir)
    engine = (fetched / "semgrep-core-proprietary").read_bytes()
    if len(engine) < int(pins["PRO_BINARY_MIN_BYTES"]) or engine[:4] != b"\x7fELF":
        sys.exit("the Pro engine file is too small or not an ELF binary")
    rules_bytes = (fetched / "rules.json").read_bytes()
    rules = json.loads(rules_bytes)["rules"]
    if len(rules) < int(pins["RULES_MIN_COUNT"]):
        sys.exit(f"{len(rules)} rules is below {pins['RULES_MIN_COUNT']}")
    placeholder = pins["PLACEHOLDER_TOKEN"]
    if len(placeholder) < 16 or any(c.isspace() for c in placeholder):
        sys.exit("the placeholder token is too short or has white space")
    if placeholder.encode() in engine or placeholder.encode() in rules_bytes:
        sys.exit("the placeholder token is inside the engine or the rules")

    files["semgrep/semgrep-core-proprietary"] = (0o755, engine)
    files["semgrep/rules.json"] = (0o644, rules_bytes)
    files["semgrep/targets.json"] = (0o644, (HERE / "targets.json").read_bytes())
    bins = Path(bin_dir)
    files["semgrep/shim.so"] = (0o755, (bins / "shim.so").read_bytes())
    files["usr/local/bin/forksrv-net"] = (0o755, (bins / "forksrv-net").read_bytes())
    files["usr/bin/uname"] = (0o755, (bins / "uname").read_bytes())
    files["scan/target.json"] = (0o644, b"x" * PLACEHOLDER_BYTES)
    files["semgrep/.tok"] = (0o444, placeholder.encode() + b"\n")
    info = {
        "semgrep_version": pins["SEMGREP_VERSION"],
        "platform": pins["PRO_PLATFORM"],
        "engine_sha256": hashlib.sha256(engine).hexdigest(),
        "rules_sha256": hashlib.sha256(rules_bytes).hexdigest(),
        "rules_count": len(rules),
        "wheel_sha256": sha,
        "token": "placeholder; the real token is not in this image",
    }
    files["semgrep/BUILD-INFO.json"] = (
        0o644,
        (json.dumps(info, indent=2, sort_keys=True) + "\n").encode(),
    )
    notice = NOTICE.format(
        version=pins["SEMGREP_VERSION"],
        platform=pins["PRO_PLATFORM"],
        wheel=pins["WHEEL_FILE"],
        sha=sha,
    )
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
                member = entry(name, mode)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
            else:
                tar.addfile(entry(name, 0o755, directory=True))
    h = hashlib.sha256()
    with open(out, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    print(f"layer {out} sha256:{h.hexdigest()}")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:])
