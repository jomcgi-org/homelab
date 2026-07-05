"""Broker for the fc-invoke sandbox workload (ADR agents/044).

Shared by the MCP tool (sandbox/mcp.py) and the Discord concierge tool
(chat/agent.py). POSTs code to the in-cluster fc-invoke daemon; the guest is
zero-egress and one-shot, so this client is the only stateful party.
"""

from __future__ import annotations

import logging
import os

import httpx

from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

FC_INVOKE_URL = os.environ.get("FC_INVOKE_URL", "")

SANDBOX_CONNECT_TIMEOUT = 5.0
# Guest wall-clock cap is 25s inside a 30s workload requestTimeout; read a
# little past that so the daemon's timeout error reaches us intact.
SANDBOX_READ_TIMEOUT = 35.0


async def run_python_in_sandbox(code: str, files: list[dict] | None = None) -> dict:
    """POST code (and optional input files) to the fc-invoke sandbox workload.

    Returns the daemon's structured response on success: ``stdout``,
    ``stderr``, ``exit_code``, ``files`` (base64-encoded), ``duration_ms``,
    and ``truncated``. On failure, a dict with a single ``error`` key.
    """
    if not FC_INVOKE_URL:
        return {"error": "FC_INVOKE_URL is not configured"}
    if not code or not code.strip():
        return {"error": "no code provided"}

    payload: dict = {"code": code}
    if files:
        payload["files"] = files

    timeout = httpx.Timeout(SANDBOX_READ_TIMEOUT, connect=SANDBOX_CONNECT_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Carry this pod's ServiceAccount token so fc-invoke's TokenReview
            # gate admits the call; off-cluster this is an empty header set.
            resp = await client.post(
                f"{FC_INVOKE_URL}/invoke/sandbox",
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
        logger.exception("sandbox execution failed")
        return {"error": f"sandbox execution failed: {exc}"}
