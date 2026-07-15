"""Idempotent registration for the og-image function (Task 12, ADR agents/045).

Builds a deterministic zip of ``app.py`` and POSTs it to the FaaS ingestion API
(``POST /api/functions``). Idempotency is a server property: the endpoint
short-circuits when an identical ``(zip sha256, handler, runtime, visibility)``
is already registered and smoked (see ``faas/router.py``), so re-running this on
an unchanged ``app.py`` is a true no-op (no S3 upload, no CR churn, no VM smoke).
This script therefore just always POSTs and reports what the server did.

The zip is built with fixed member metadata (name, ``2000-01-01`` timestamp,
mode ``0644``, deflate), exactly like the echo fixture, so its sha256 is stable
across rebuilds and machines: the same ``app.py`` bytes always yield the same
sha, which is what makes "idempotent by zip sha" hold. ``build_archive`` is the
load-bearing, unit-tested core (``og_image_test.py`` asserts determinism); the
network POST below is a thin CLI wrapper.

Invocation (the private ``/api/functions`` endpoint sits behind Cloudflare
Access, so reach it via a port-forward that bypasses the edge, matching the
echo live-verify recipe):

    kubectl -n <ns> port-forward deploy/monolith 8000:8000 &
    FAAS_API_BASE=http://localhost:8000 \
        python3 projects/monolith/faas/functions/og_image/register.py

Exit status: 0 on a fresh registration OR an idempotent no-op, non-zero on any
validation/build/smoke failure (the server's error detail is printed).
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path

# Registration manifest. ``handler`` matches app.handle; ``requirements`` is the
# IMPORTABLE name PIL (not the pip name "pillow"): the ingestion API checks
# declared requirements against the runtime base's baked IMPORT names
# (faas/runtime.py BAKED_PACKAGES), where Pillow appears as "PIL".
FUNCTION_NAME = "og-image"
RUNTIME = "python312"
HANDLER = "app.handle"
REQUIREMENTS = "PIL"
# PUBLIC as of Task 13: og-image is served at jomcgi.dev/functions/og-image. A
# visibility change is intentionally NOT an idempotent no-op (the server's
# short-circuit compares visibility), so re-registering a currently-private
# og-image with this manifest re-smokes and re-gates it as public.
VISIBILITY = "public"

_APP_PY = Path(__file__).with_name("app.py")

# Deterministic zip member metadata (mirrors the echo fixture's reproducible
# build). A fixed epoch + mode + deflate means the archive bytes depend only on
# app.py's content, so the sha256 is stable.
_ZIP_EPOCH = (2000, 1, 1, 0, 0, 0)
_ZIP_MODE = 0o644


def build_archive(app_source: bytes | None = None) -> bytes:
    """Build the deterministic function zip (``app.py`` at the archive root).

    ``app_source`` defaults to the checked-in ``app.py`` bytes; passing explicit
    bytes lets the unit test build without touching the filesystem. The output
    is a pure function of the source bytes: fixed name/timestamp/mode/compression
    make the sha256 reproducible.
    """
    if app_source is None:
        app_source = _APP_PY.read_bytes()

    info = zipfile.ZipInfo(filename="app.py", date_time=_ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = _ZIP_MODE << 16

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, app_source)
    return buf.getvalue()


def archive_sha256(app_source: bytes | None = None) -> str:
    """Return the sha256 hex digest of the deterministic archive."""
    return hashlib.sha256(build_archive(app_source)).hexdigest()


def register(base_url: str) -> tuple[bool, str]:
    """POST the function to ``{base_url}/api/functions``. Returns (ok, message).

    A 2xx is success (either a fresh registration or an idempotent no-op, which
    the server marks with ``unchanged: true``). Any other status is a failure
    whose ``detail`` is surfaced to the caller.
    """
    import httpx

    archive = build_archive()
    sha = hashlib.sha256(archive).hexdigest()
    files = {"zip": ("og-image.zip", archive, "application/zip")}
    data = {
        "name": FUNCTION_NAME,
        "visibility": VISIBILITY,
        "runtime": RUNTIME,
        "handler": HANDLER,
        "requirements": REQUIREMENTS,
    }
    # Generous read timeout: a first-time registration blocks on the Ready poll
    # (up to 3m) plus a smoke invoke, so allow past the server's 180s budget.
    with httpx.Client(timeout=httpx.Timeout(240.0)) as client:
        resp = client.post(f"{base_url}/api/functions", data=data, files=files)

    if 200 <= resp.status_code < 300:
        body = resp.json()
        state = "unchanged (no-op)" if body.get("unchanged") else "registered"
        return (
            True,
            f"{FUNCTION_NAME} {state} (sha256={sha[:12]}…, status={resp.status_code})",
        )
    return False, f"registration failed (status={resp.status_code}): {resp.text[:800]}"


def main() -> int:
    base_url = os.environ.get("FAAS_API_BASE", "http://localhost:8000").rstrip("/")
    ok, message = register(base_url)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
