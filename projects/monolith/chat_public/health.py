"""Fatal OpenAI-compatible inference health for the public monolith tier.

Public product features depend on the configured chat provider, so an
unavailable model makes ``/api/health`` fail. The component probes the standard
models endpoint with the same optional Meta Spark bearer header as chat calls.
Local OpenAI-compatible servers remain usable without a key.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

from shared.inference import auth_headers

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
    """Probe the configured inference provider without caching."""
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
            response = await http.get(
                f"{base_url.rstrip('/')}/v1/models",
                headers=auth_headers(base_url),
            )
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
