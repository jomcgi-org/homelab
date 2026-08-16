"""Broker for the EmberVM shotter task workload (website snapshotter).

Mirrors sandbox/client.py's POST-with-Idempotency-Key shape: a fresh task per
call, dispatched synchronously (``?wait=true``), returning the guest's
response verbatim. shotter.mcp reaches ``capture`` through this module
(``client.capture(...)``), not via a name bound at import time, so a unit
test's monkeypatch of ``shotter.client.capture`` actually takes effect (see
shotter/tests/bdd_mcp_tool_test.py's TestMCPReturnShape docstring).
"""

from __future__ import annotations

import hashlib
import logging
import os

import httpx

from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

SHOTTER_WORKLOAD = os.environ.get("SHOTTER_WORKLOAD", "shotter")

# Timeout nesting (ADR embervm/035 section 5): Context Forge TOOL_TIMEOUT is
# 60s, the workload's timeoutSeconds is 50s, the guest handler caps a PNG at
# 6 MiB inside the workload's 8 MiB resultMaxBytes. This client's read
# timeout has to sit strictly between Context Forge and the workload: long
# enough to wait out a full 50s workload budget, short enough that a hung
# request reports a real ToolTimeout before Context Forge's own 60s cutoff
# turns it into a generic severed connection.
_CONNECT_TIMEOUT_SECONDS = 5.0
_READ_TIMEOUT_SECONDS = 55.0


class ToolTimeout(Exception):
    """The EmberVM shotter dispatch exceeded its client-side timeout budget."""


async def capture(
    url: str,
    width: int,
    height: int,
    timeout_ms: int,
    *,
    full_page: bool = False,
    wait_until: str = "load",
) -> dict:
    """POST a screenshot request to the EmberVM shotter task workload.

    A fresh task is created per screenshot request, so each capture is
    isolated from every other caller's (ADR embervm/035 section 1).
    ``timeout_ms`` is forwarded to the guest, which uses it to bound its own
    CDP navigate wait: a layer deeper than this client's own read timeout,
    not a substitute for it.

    Returns the guest's response verbatim on success: ``png_b64``, ``width``,
    ``height``, ``final_url``, ``status``, ``duration_ms`` (issue #4994 T3).
    A transport-level timeout is converted into ``ToolTimeout`` so it
    surfaces as a real tool error rather than a severed connection.
    """
    embervm_url = os.environ.get("EMBERVM_URL", "")
    if not embervm_url:
        raise RuntimeError("EMBERVM_URL is not configured")

    payload = {
        "url": url,
        "width": width,
        "height": height,
        "full_page": full_page,
        "wait_until": wait_until,
        "timeout_ms": timeout_ms,
    }
    key_material = f"{url}\0{width}\0{height}\0{full_page}\0{wait_until}".encode()
    headers = {
        **auth_headers(),
        "Idempotency-Key": hashlib.sha256(key_material).hexdigest(),
    }

    async with httpx.AsyncClient() as client:
        # Set after construction rather than via the AsyncClient(timeout=...)
        # constructor kwarg: a hermetic unit test's fake transport stands in
        # for the whole httpx.AsyncClient class with a bare, zero-argument
        # constructor, and this reaches the same effective per-request
        # timeout without requiring that fake to also accept a kwarg it
        # never reads.
        client.timeout = httpx.Timeout(
            _READ_TIMEOUT_SECONDS, connect=_CONNECT_TIMEOUT_SECONDS
        )
        try:
            resp = await client.post(
                f"{embervm_url}/v1/workloads/{SHOTTER_WORKLOAD}/tasks?wait=true",
                json=payload,
                headers=headers,
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ToolTimeout(
                f"shotter capture of {url!r} exceeded the client timeout budget "
                f"of {_READ_TIMEOUT_SECONDS}s"
            ) from exc
        resp.raise_for_status()
        return resp.json()
