"""Broker for the fc-invoke semgrep workloads (ADR agents/044).

The plain-function half of the semgrep scan path, mirroring
``sandbox/client.py``. It POSTs file contents to the in-cluster ``fc-invoke``
daemon and returns the structured findings, for two workloads sharing the same
wire shape: ``scan_files`` calls ``/invoke/semgrep`` (a per-PR diff scan), and
``scan_files_full`` calls ``/invoke/semgrep-full`` (a whole-repo interfile FULL
scan). The MCP tool (``semgrep_scan/mcp.py``) and the demos router
(``demos/firecracker_api.py``) call ``scan_files``; ``full_scan.py`` calls
``scan_files_full``. Keeping the HTTP logic here means none of them has to
reach through the FastMCP tool wrapper to run a scan.
"""

from __future__ import annotations

import logging
import os

import httpx

from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

FC_INVOKE_URL = os.environ.get("FC_INVOKE_URL", "")

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
SEMGREP_FULL_READ_TIMEOUT = 660.0


async def _post_invoke(workload: str, files: list[dict], read_timeout: float) -> dict:
    """Shared POST to an fc-invoke semgrep workload; used by both scan entrypoints.

    ``workload`` is the ``/invoke/<workload>`` path segment (``semgrep`` for the
    per-PR diff scan, ``semgrep-full`` for the whole-repo interfile scan). Same
    body shape and error handling for both: on success returns the daemon's
    parsed JSON response, on failure a dict with a single ``error`` key.
    """
    if not FC_INVOKE_URL:
        return {"error": "FC_INVOKE_URL is not configured"}
    if not files:
        return {"error": "no files provided to scan"}

    timeout = httpx.Timeout(read_timeout, connect=SEMGREP_CONNECT_TIMEOUT)
    payload = {"files": files}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Carry this pod's ServiceAccount token so fc-invoke's TokenReview
            # gate admits the call; off-cluster this is an empty header set.
            resp = await client.post(
                f"{FC_INVOKE_URL}/invoke/{workload}",
                json=payload,
                headers=auth_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as exc:
        logger.exception("fc-invoke connection failed")
        return {"error": f"could not reach fc-invoke: {exc}"}
    except httpx.HTTPStatusError as exc:
        logger.exception("fc-invoke returned an error status")
        return {
            "error": (
                f"fc-invoke returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            )
        }
    except Exception as exc:  # noqa: BLE001: surface any failure as structured error
        logger.exception("fc-invoke semgrep scan failed")
        return {"error": f"semgrep scan failed: {exc}"}


async def scan_files(files: list[dict]) -> dict:
    """POST file contents to the fc-invoke semgrep workload and return findings.

    Each entry in ``files`` needs a ``path`` (repo-relative, used to pick rules
    and report locations) and a ``content`` (the whole current file text). On
    success returns the daemon response: a ``findings`` list plus an ``errors``
    list. On failure returns a dict with a single ``error`` key.
    """
    return await _post_invoke("semgrep", files, SEMGREP_READ_TIMEOUT)


async def scan_files_hi(files: list[dict]) -> dict:
    """POST a large PR diff to the semgrep-hi fc-invoke workload and return findings.

    Same wire shape and response as ``scan_files``, but targets ``/invoke/semgrep-hi``:
    the warm mcp scan-server sized to 6 vCPU so a many-file diff is matched in
    parallel over cores instead of serially on one. The monolith routes only large
    diffs here (``router._HEAVY_ROUTE_MIN_FILES``); small diffs use ``scan_files``,
    which is faster for them (semgrep-hi carries a fixed thread-pool overhead). Uses
    the same read timeout as ``scan_files`` (a heavy diff scan is still seconds, and
    parallelism keeps it under the daemon's 90s workload timeout).
    """
    return await _post_invoke("semgrep-hi", files, SEMGREP_READ_TIMEOUT)


async def scan_files_full(files: list[dict]) -> dict:
    """POST the whole repo's file contents to the semgrep-full fc-invoke workload.

    Same wire shape as ``scan_files`` (``{"path", "content"}`` entries, same
    daemon response and error shape), but targets ``/invoke/semgrep-full``: a
    separate fc-invoke workload that runs Semgrep's interfile engine over the
    whole repo instead of a per-PR diff. Uses a much longer read timeout
    (``SEMGREP_FULL_READ_TIMEOUT``) since a whole-repo interfile scan is far
    slower than a diff scan.
    """
    return await _post_invoke("semgrep-full", files, SEMGREP_FULL_READ_TIMEOUT)
