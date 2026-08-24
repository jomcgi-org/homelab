"""Fatal inference health for the public monolith tier.

This component is FATAL, not advisory: public product features depend on the
in-cluster inference lane, so an unavailable model makes ``/api/health`` fail.
It lives in ``chat_public`` because that domain already has reachability to
in-cluster inference for those features, which adds no new network privilege.
That differs from the advisory, externally focused ``cluster.cd_health``
pattern.

The component probes the inference server's ``/health`` path, matching the
kubelet readiness probe in ``projects/inference/deploy/values.yaml``. Because
inference uses a Recreate deployment strategy, loading the 27B model during a
deploy will make public health report down. That is an intentional tradeoff for
an honest dependency signal.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

INFERENCE_URL_ENV = "CHAT_PUBLIC_INFERENCE_URL"
INFERENCE_HEALTH_TIMEOUT_ENV = "CHAT_PUBLIC_INFERENCE_HEALTH_TIMEOUT_SECONDS"
INFERENCE_HEALTH_CONNECT_TIMEOUT_ENV = (
    "CHAT_PUBLIC_INFERENCE_HEALTH_CONNECT_TIMEOUT_SECONDS"
)


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw else default
    except ValueError:
        logger.warning(
            "inference health: %s=%r is not a number, using %s", name, raw, default
        )
        return default


def _client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


async def inference_health() -> dict:
    """Probe the in-cluster inference readiness endpoint without caching."""
    base_url = os.environ.get(INFERENCE_URL_ENV, "")
    if not base_url:
        logger.warning(
            "inference health: %s not configured, failing open", INFERENCE_URL_ENV
        )
        return {"ok": True, "detail": f"{INFERENCE_URL_ENV} not configured"}

    timeout = httpx.Timeout(
        _env_seconds(INFERENCE_HEALTH_TIMEOUT_ENV, 3.0),
        connect=_env_seconds(INFERENCE_HEALTH_CONNECT_TIMEOUT_ENV, 2.0),
    )
    started = time.monotonic()
    try:
        async with _client(timeout) as http:
            response = await http.get(f"{base_url.rstrip('/')}/health")
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "detail": f"inference unreachable: {type(exc).__name__}",
        }

    elapsed_ms = (time.monotonic() - started) * 1000
    if 200 <= response.status_code < 300:
        return {"ok": True, "detail": f"inference ok, {elapsed_ms:.0f}ms"}
    return {
        "ok": False,
        "detail": f"inference returned HTTP {response.status_code}",
    }
