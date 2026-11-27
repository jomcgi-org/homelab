"""Install the pinned hass-oidc-auth integration into /config/custom_components.

The release zip must match SHA256, or the pod does not start. A matching
install is kept, so a restart needs no download. The manifest pins joserfc,
which Home Assistant installs from PyPI at startup.
"""

import hashlib
import io
import json
import pathlib
import shutil
import urllib.request
import zipfile

URL = "https://github.com/christiaangoossens/hass-oidc-auth/releases/download/v1.2.1/hass-oidc-auth.zip"
SHA256 = "e5badaaacaa63cfd6fe733924a05e76d75058836190398598fb24de57cd47ccd"
JOSERFC = "joserfc==1.7.7"

dest = pathlib.Path("/config/custom_components/auth_oidc")
marker = dest / ".release-sha256"
if marker.exists() and marker.read_text() == SHA256:
    raise SystemExit(0)

data = urllib.request.urlopen(URL, timeout=60).read()
digest = hashlib.sha256(data).hexdigest()
if digest != SHA256:
    raise SystemExit(f"hass-oidc-auth zip sha256 {digest}, want {SHA256}")

staging = dest.with_name("auth_oidc.new")
shutil.rmtree(staging, ignore_errors=True)
zipfile.ZipFile(io.BytesIO(data)).extractall(staging)
manifest = staging / "manifest.json"
spec = json.loads(manifest.read_text())
spec["requirements"] = [
    JOSERFC if r == "joserfc" else r for r in spec["requirements"]
]
manifest.write_text(json.dumps(spec, indent=2))
(staging / ".release-sha256").write_text(SHA256)
shutil.rmtree(dest, ignore_errors=True)
staging.rename(dest)
