"""MCP tool that runs Semgrep over changed files via the semgrep-scand daemon.

Exposes a single ``semgrep_scan`` tool. It is a thin async wrapper that POSTs
the supplied file contents to the in-cluster ``semgrep-scand`` HTTP service and
returns the structured findings. The daemon URL is injected from Helm values as
``SEMGREP_SCAND_URL`` and is never hardcoded here.
"""

from __future__ import annotations

import logging
import os

import httpx

from app.mcp_app import mcp

logger = logging.getLogger(__name__)

SEMGREP_SCAND_URL = os.environ.get("SEMGREP_SCAND_URL", "")

# Separate connect/read timeouts: a fast connect surfaces a down daemon quickly,
# while a generous read budget (a bit over the daemon ScanTimeout) lets a large
# multi-file scan finish.
SEMGREP_CONNECT_TIMEOUT = 5.0
SEMGREP_READ_TIMEOUT = 90.0


@mcp.tool
async def semgrep_scan(files: list[dict], format: str = "json") -> dict:
    """Scan changed source files for security and correctness issues with Semgrep.

    Send the whole content of each changed file to the in-cluster semgrep-scand
    daemon and get back the Semgrep findings. Use this to lint a diff before
    committing or to triage a file you just edited.

    Args:
        files: A list of file objects. Each object needs a ``path`` (the repo
            relative file path, used to pick rules and report locations) and a
            ``content`` (the entire current text of that file). Pass the whole
            file, not just the changed lines, so Semgrep has full context.
        format: Output format requested from the daemon. Defaults to ``json``.

    Returns:
        On success, the daemon response: a ``findings`` list (each finding has
        ``path``, ``line``, ``col``, ``ruleId``, ``severity``, ``message``) plus
        an ``errors`` list for any per-file scan problems. On failure, a dict
        with a single ``error`` key describing what went wrong.
    """
    if not SEMGREP_SCAND_URL:
        return {"error": "SEMGREP_SCAND_URL is not configured"}
    if not files:
        return {"error": "no files provided to scan"}

    timeout = httpx.Timeout(SEMGREP_READ_TIMEOUT, connect=SEMGREP_CONNECT_TIMEOUT)
    payload = {"files": files, "format": format}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{SEMGREP_SCAND_URL}/scan", json=payload)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as exc:
        logger.exception("semgrep-scand connection failed")
        return {"error": f"could not reach semgrep-scand: {exc}"}
    except httpx.HTTPStatusError as exc:
        logger.exception("semgrep-scand returned an error status")
        return {
            "error": (
                f"semgrep-scand returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            )
        }
    except Exception as exc:  # noqa: BLE001 — surface any failure as structured error
        logger.exception("semgrep-scand scan failed")
        return {"error": f"semgrep scan failed: {exc}"}
