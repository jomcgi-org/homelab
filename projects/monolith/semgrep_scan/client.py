"""Broker for the EmberVM semgrep workload.

The plain-function half of the semgrep scan path, mirroring
``sandbox/client.py``. It POSTs file contents to EmberVM and returns structured
findings. The MCP tool (``semgrep_scan/mcp.py``) and the demos router
(``demos/firecracker_api.py``) call ``scan_files``.
"""

from __future__ import annotations

import hashlib
import logging
import os

import httpx

from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

EMBERVM_URL = os.environ.get("EMBERVM_URL", "")

# Separate connect/read timeouts: a fast connect surfaces a down daemon quickly,
# while a generous read budget (a bit over the daemon ScanTimeout) lets a large
# multi-file scan finish.
SEMGREP_CONNECT_TIMEOUT = 5.0
SEMGREP_READ_TIMEOUT = 90.0

# The whole-repo interfile FULL scan (semgrep-full workload) walks the entire
# repo with cross-file analysis, which is far slower than a per-PR diff scan.
# Same fast connect (a down daemon still fails fast); a much longer read budget
# so the daemon has room to finish before httpx gives up on us. Set a margin
# ABOVE the daemon's 600s requestTimeout so a scan that runs to its budget
# surfaces the daemon's structured error, not an opaque client-side timeout.


async def _post_embervm(
    files: list[dict], read_timeout: float, dedupe: bool = True
) -> dict:
    """POST a diff scan to EmberVM's ``semgrep`` Workload; the EmberVM counterpart
    Submits synchronously (``?wait=true``) so the guest's
    ScanResult comes back inline (EmberVM forwards the guest response verbatim, so
    the shape is stable), with an Idempotency-Key from the content hash so
    a webhook redelivery dedupes to the same task, unless ``dedupe`` is False (the
    demo single-scan path), in which case the header is omitted so a fresh scan
    always runs. Same error shape as ``_post_invoke`` (a dict with a single
    ``error`` key on failure).
    """
    if not EMBERVM_URL:
        return {"error": "EMBERVM_URL is not configured"}
    if not files:
        return {"error": "no files provided to scan"}

    timeout = httpx.Timeout(read_timeout, connect=SEMGREP_CONNECT_TIMEOUT)
    payload = {"files": files}
    headers = auth_headers()
    if dedupe:
        headers["Idempotency-Key"] = _content_key(files)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{EMBERVM_URL}/v1/workloads/semgrep/tasks?wait=true",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as exc:
        logger.exception("embervm connection failed")
        return {"error": f"could not reach embervm: {exc}"}
    except httpx.HTTPStatusError as exc:
        logger.exception("embervm returned an error status")
        return {
            "error": (
                f"embervm returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            )
        }
    except Exception as exc:  # noqa: BLE001: surface any failure as structured error
        logger.exception("embervm semgrep scan failed")
        return {"error": f"embervm semgrep scan failed: {exc}"}


def _content_key(files: list[dict]) -> str:
    """A stable idempotency key from the scan's file contents (path + content),
    order-independent, so a webhook redelivery of the same diff dedupes to the
    same EmberVM task."""
    digest = hashlib.sha256()
    for f in sorted(files, key=lambda e: e.get("path", "")):
        digest.update(f.get("path", "").encode())
        digest.update(b"\0")
        digest.update(f.get("content", "").encode())
        digest.update(b"\0")
    return digest.hexdigest()


async def scan_files(files: list[dict], dedupe: bool = True) -> dict:
    """POST file contents to the semgrep diff workload and return findings.

    Each entry in ``files`` needs a ``path`` (repo-relative, used to pick rules
    and report locations) and a ``content`` (the whole current file text). On
    success returns the daemon response: a ``findings`` list plus an ``errors``
    list. On failure returns a dict with a single ``error`` key.

    ``dedupe`` (default True) controls whether the EmberVM path attaches the
    Idempotency-Key header, so a webhook redelivery of the same diff collapses
    to the same task. The demo single-scan handler passes ``dedupe=False`` so
    every demo run is a genuinely fresh scan rather than a cached prior result.
    """
    return await _post_embervm(files, SEMGREP_READ_TIMEOUT, dedupe=dedupe)
