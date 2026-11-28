"""Install the pinned hass-oidc-auth integration and its locked packages.

The release zip must match SHA256 and every package must match its hash in
auth-oidc-requirements.lock, or the pod does not start. A matching install
is kept, so a restart needs no download.

The manifest's requirements are removed: the lockfile packages are on
PYTHONPATH (/config/deps/auth_oidc), so Home Assistant installs nothing
from PyPI at startup.
"""

import hashlib
import io
import json
import pathlib
import shutil
import subprocess
import urllib.request
import zipfile

URL = "https://github.com/christiaangoossens/hass-oidc-auth/releases/download/v1.2.1/hass-oidc-auth.zip"
SHA256 = "e5badaaacaa63cfd6fe733924a05e76d75058836190398598fb24de57cd47ccd"
LOCK = pathlib.Path("/seed/auth-oidc-requirements.lock")

dest = pathlib.Path("/config/custom_components/auth_oidc")
deps = pathlib.Path("/config/deps/auth_oidc")


def installed(path: pathlib.Path, want: str) -> bool:
    marker = path / ".lock-sha256"
    return marker.exists() and marker.read_text() == want


def replace(path: pathlib.Path, build, want: str) -> None:
    staging = path.with_name(path.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    build(staging)
    (staging / ".lock-sha256").write_text(want)
    shutil.rmtree(path, ignore_errors=True)
    staging.rename(path)


def integration(staging: pathlib.Path) -> None:
    data = urllib.request.urlopen(URL, timeout=60).read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"hass-oidc-auth zip sha256 {digest}, want {SHA256}")
    zipfile.ZipFile(io.BytesIO(data)).extractall(staging)
    manifest = staging / "manifest.json"
    spec = json.loads(manifest.read_text())
    spec["requirements"] = []
    manifest.write_text(json.dumps(spec, indent=2))


def packages(staging: pathlib.Path) -> None:
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--require-hashes",
            "--no-deps",
            "--target",
            str(staging),
            "-r",
            str(LOCK),
        ],
        check=True,
    )


if not installed(dest, SHA256):
    dest.parent.mkdir(parents=True, exist_ok=True)
    replace(dest, integration, SHA256)
lock_sha = hashlib.sha256(LOCK.read_bytes()).hexdigest()
if not installed(deps, lock_sha):
    deps.parent.mkdir(parents=True, exist_ok=True)
    replace(deps, packages, lock_sha)
