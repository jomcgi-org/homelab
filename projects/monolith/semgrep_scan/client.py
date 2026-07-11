"""Broker for the fc-invoke semgrep workload (ADR agents/044).

The plain-function half of the semgrep scan path, mirroring
``sandbox/client.py``. It POSTs file contents to the in-cluster ``fc-invoke``
daemon's ``/invoke/semgrep`` endpoint and returns the structured findings. The
MCP tool (``semgrep_scan/mcp.py``) and the demos router (``demos/firecracker_api.py``)
both call ``scan_files``; keeping the HTTP logic here means neither has to reach
through the FastMCP tool wrapper to run a scan.
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


async def scan_files(files: list[dict]) -> dict:
    """POST file contents to the fc-invoke semgrep workload and return findings.

    Each entry in ``files`` needs a ``path`` (repo-relative, used to pick rules
    and report locations) and a ``content`` (the whole current file text). On
    success returns the daemon response: a ``findings`` list plus an ``errors``
    list. On failure returns a dict with a single ``error`` key.
    """
    if not FC_INVOKE_URL:
        return {"error": "FC_INVOKE_URL is not configured"}
    if not files:
        return {"error": "no files provided to scan"}

    timeout = httpx.Timeout(SEMGREP_READ_TIMEOUT, connect=SEMGREP_CONNECT_TIMEOUT)
    payload = {"files": files}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Carry this pod's ServiceAccount token so fc-invoke's TokenReview
            # gate admits the call; off-cluster this is an empty header set.
            resp = await client.post(
                f"{FC_INVOKE_URL}/invoke/semgrep",
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
