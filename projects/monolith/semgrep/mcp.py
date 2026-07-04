"""MCP tool that runs Semgrep over changed files via the fc-invoke daemon.

Exposes a single ``semgrep_scan`` tool. It is a thin async wrapper that POSTs
the supplied file contents to the in-cluster ``fc-invoke`` HTTP service and
returns the structured findings. The daemon URL is injected from Helm values as
``FC_INVOKE_URL`` and is never hardcoded here.
"""

from __future__ import annotations

import logging
import os

import httpx

from app.mcp_app import mcp
from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

FC_INVOKE_URL = os.environ.get("FC_INVOKE_URL", "")

# Separate connect/read timeouts: a fast connect surfaces a down daemon quickly,
# while a generous read budget (a bit over the daemon ScanTimeout) lets a large
# multi-file scan finish.
SEMGREP_CONNECT_TIMEOUT = 5.0
SEMGREP_READ_TIMEOUT = 90.0


@mcp.tool
async def semgrep_scan(files: list[dict]) -> dict:
    """Scan changed source files for security and correctness issues with Semgrep.

    Send the whole content of each changed file to the in-cluster fc-invoke
    daemon and get back the Semgrep findings. Use this to lint a diff before
    committing or to triage a file you just edited.

    Args:
        files: A list of file objects. Each object needs a ``path`` (the repo
            relative file path, used to pick rules and report locations) and a
            ``content`` (the entire current text of that file). Pass the whole
            file, not just the changed lines, so Semgrep has full context.

    Returns:
        On success, the daemon response: a ``findings`` list (each finding has
        ``path``, ``line``, ``col``, ``rule_id``, ``severity``, ``message``) plus
        an ``errors`` list for any per-file scan problems. On failure, a dict
        with a single ``error`` key describing what went wrong.
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
