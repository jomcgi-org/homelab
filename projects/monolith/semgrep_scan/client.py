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
import re

import httpx

from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

EMBERVM_URL = os.environ.get("EMBERVM_URL", "")

# Separate connect/read timeouts: a fast connect surfaces a down daemon quickly,
# while a generous read budget (a bit over the daemon ScanTimeout) lets a large
# multi-file scan finish.
SEMGREP_CONNECT_TIMEOUT = 5.0
SEMGREP_READ_TIMEOUT = 90.0
MAX_CORRELATION_ID_LENGTH = 128
_CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


async def _post_embervm(
    files: list[dict],
    read_timeout: float,
    dedupe: bool = True,
    correlation_id: str | None = None,
) -> dict:
    """POST a diff scan to EmberVM's ``semgrep`` Workload; the EmberVM counterpart
    Submits synchronously (``?wait=true``) so the guest's
    ScanResult comes back inline (EmberVM forwards the guest response verbatim, so
    the shape is stable), with an Idempotency-Key from the content hash so
    a webhook redelivery dedupes to the same task, unless ``dedupe`` is False (the
    demo single-scan path), in which case the header is omitted so a fresh scan
    always runs. Same error shape as ``_post_invoke`` (a dict with a single
    ``error`` key on failure). A supplied correlation ID is part of both the
    body and idempotency identity, preventing a cached task from echoing a
    different scan's ID.
    """
    if not EMBERVM_URL:
        return {"error": "EMBERVM_URL is not configured"}
    if not files:
        return {"error": "no files provided to scan"}
    if not _valid_correlation_id(correlation_id):
        return {"error": "invalid correlation_id"}

    timeout = httpx.Timeout(read_timeout, connect=SEMGREP_CONNECT_TIMEOUT)
    payload = {"files": files}
    if correlation_id:
        payload["correlation_id"] = correlation_id
    headers = auth_headers()
    if dedupe:
        headers["Idempotency-Key"] = _content_key(files, correlation_id)

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


def _valid_correlation_id(correlation_id: str | None) -> bool:
    """Return whether optional caller metadata is bounded and log-safe."""
    if correlation_id in (None, ""):
        return True
    return (
        isinstance(correlation_id, str)
        and len(correlation_id) <= MAX_CORRELATION_ID_LENGTH
        and _CORRELATION_ID_PATTERN.fullmatch(correlation_id) is not None
    )


def _content_key(files: list[dict], correlation_id: str | None = None) -> str:
    """A stable idempotency key from the scan's file contents (path + content),
    order-independent, so a webhook redelivery of the same diff dedupes to the
    same EmberVM task."""
    digest = hashlib.sha256()
    for f in sorted(files, key=lambda e: e.get("path", "")):
        digest.update(f.get("path", "").encode())
        digest.update(b"\0")
        digest.update(f.get("content", "").encode())
        digest.update(b"\0")
    # Tagged scans must not dedupe to a cached response carrying a different
    # correlation ID. Keep the legacy files-only hash exactly unchanged when
    # metadata is omitted.
    if correlation_id:
        digest.update(b"correlation_id\0")
        digest.update(correlation_id.encode())
    return digest.hexdigest()


async def scan_files(
    files: list[dict],
    dedupe: bool = True,
    correlation_id: str | None = None,
) -> dict:
    """POST file contents to the semgrep diff workload and return findings.

    Each entry in ``files`` needs a ``path`` (repo-relative, used to pick rules
    and report locations) and a ``content`` (the whole current file text). On
    success returns the daemon response: a ``findings`` list plus an ``errors``
    list. On failure returns a dict with a single ``error`` key.

    ``dedupe`` (default True) controls whether the EmberVM path attaches the
    Idempotency-Key header, so a webhook redelivery of the same diff collapses
    to the same task. The demo single-scan handler passes ``dedupe=False`` so
    every demo run is a genuinely fresh scan rather than a cached prior result.

    ``correlation_id`` is optional bounded caller metadata. When present it is
    sent in the invoke body, included in the idempotency key, and echoed by the
    guest in its response. Invalid values fail locally without being logged.
    """
    return await _post_embervm(
        files,
        SEMGREP_READ_TIMEOUT,
        dedupe=dedupe,
        correlation_id=correlation_id,
    )
