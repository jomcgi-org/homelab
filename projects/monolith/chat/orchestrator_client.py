"""ADR 036 orchestrator brief-compiler OpenAI-compatible client.

Unary, short-timeout httpx client shaped like ``summarizer.build_llm_caller``
(summarizer.py:412), but for the hosted escalation tier rather than the default
chat provider. Per the fail-open philosophy (ADR 036 Architecture: "Fail open"),
each provider attempt makes exactly one call: any timeout, HTTP error, or
unusable response shape raises ``OrchestratorUnavailable``.

The primary provider is pinned via ``ORCHESTRATOR_MODEL`` / ``ORCHESTRATOR_BASE_URL``
/ ``ORCHESTRATOR_API_KEY``. ``ORCHESTRATOR_FALLBACKS`` is an ordered JSON array of
additional providers (``{"model", "base_url", "api_key_env"}``); a primary
``OrchestratorUnavailable`` walks that chain in order, stopping at the first
success, before the exception finally propagates and the caller fails open to
direct submit. This exists so a free rate-limited primary (Nemotron on NVIDIA's
40 RPM tier) degrades to DeepSeek/OpenRouter, then to Meta Spark, rather than
dropping the whole brief on one throttle or blip. With no chain
configured the behavior is byte-identical to the original single-attempt contract.
There is still no per-provider retry loop (contrast with build_llm_caller's 3
retries), because escalations must degrade quickly, not stall on any single call.

"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
# Routing call budget, overridable via ORCHESTRATOR_TIMEOUT_S.
_DEFAULT_TIMEOUT_S = 60.0


class OrchestratorUnavailable(Exception):
    """Raised when the orchestrator call cannot be completed (timeout, HTTP
    error, or any other transport failure). Callers should treat this as a
    fail-open signal and fall back to today's direct-submit path."""


@dataclass
class OrchestratorResponse:
    """Parsed result of one orchestrator call."""

    content: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    latency_ms: int


def _read_config() -> tuple[str, str, str, float]:
    """Read the primary model, base URL, API key, and timeout from the environment.

    The model is expected to be pinned (never ``:auto``) so briefs stay
    attributable to a specific provider model (ADR 036 Risks table). The API key
    prefers the provider-agnostic ``ORCHESTRATOR_API_KEY`` and falls back to
    ``OPENROUTER_API_KEY`` so a deployment that only wired the legacy OpenRouter
    key keeps working unchanged.
    """
    model = os.environ.get("ORCHESTRATOR_MODEL", "")
    base_url = os.environ.get("ORCHESTRATOR_BASE_URL", "") or _DEFAULT_BASE_URL
    api_key = os.environ.get("ORCHESTRATOR_API_KEY", "") or os.environ.get(
        "OPENROUTER_API_KEY", ""
    )
    timeout_raw = os.environ.get("ORCHESTRATOR_TIMEOUT_S", "")
    try:
        timeout_s = float(timeout_raw) if timeout_raw else _DEFAULT_TIMEOUT_S
    except ValueError:
        timeout_s = _DEFAULT_TIMEOUT_S
    return model, base_url, api_key, timeout_s


def _read_fallback_chain() -> list[tuple[str, str, str]]:
    """Read the ordered fallback chain as ``[(model, base_url, api_key), ...]``.

    Parses ``ORCHESTRATOR_FALLBACKS``, a JSON array of
    ``{"model", "base_url", "api_key_env"}`` objects in priority order. Each
    ``api_key_env`` names the env var holding that provider's key (injected from a
    Secret; empty or absent means no auth for a local provider). A primary
    ``OrchestratorUnavailable`` walks this list in order, so a rate-limited or
    erroring primary degrades to DeepSeek, then to Meta Spark, before failing
    open. Returns ``[]`` when unset or unparseable, which
    restores the single-attempt contract exactly.
    """
    raw = os.environ.get("ORCHESTRATOR_FALLBACKS", "").strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception("orchestrator: ORCHESTRATOR_FALLBACKS is not valid JSON")
        return []
    if not isinstance(entries, list):
        logger.error("orchestrator: ORCHESTRATOR_FALLBACKS must be a JSON array")
        return []
    chain: list[tuple[str, str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model = str(entry.get("model", "")).strip()
        if not model:
            continue
        base_url = str(entry.get("base_url", "")).strip() or _DEFAULT_BASE_URL
        api_key_env = str(entry.get("api_key_env", "")).strip()
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        chain.append((model, base_url, api_key))
    return chain


def _extract_usage(payload: dict) -> tuple[int | None, int | None, int | None]:
    """Pull prompt/completion/cached token counts out of an OpenRouter
    response body, tolerating any of them being absent."""
    usage = payload.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    cached_tokens = None
    prompt_details = usage.get("prompt_tokens_details") or {}
    if isinstance(prompt_details, dict):
        cached_tokens = prompt_details.get("cached_tokens")
    return prompt_tokens, completion_tokens, cached_tokens


async def _attempt_call(
    model: str,
    base_url: str,
    api_key: str,
    timeout_s: float,
    system: str,
    user: str,
) -> OrchestratorResponse:
    """One provider attempt for ``call``: POST + parse, or raise
    ``OrchestratorUnavailable``. No retry loop here (the caller decides whether
    to walk the fallback chain)."""
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            resp.raise_for_status()
            payload = resp.json()
    except httpx.TimeoutException as exc:
        raise OrchestratorUnavailable(f"orchestrator call timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise OrchestratorUnavailable(f"orchestrator call failed: {exc}") from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OrchestratorUnavailable(
            f"unexpected orchestrator response shape: {exc}"
        ) from exc

    prompt_tokens, completion_tokens, cached_tokens = _extract_usage(payload)

    return OrchestratorResponse(
        content=content,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        latency_ms=latency_ms,
    )


async def call(system: str, user: str) -> OrchestratorResponse:
    """Send one system/user turn to the configured model and parse the response.

    Tries the primary provider, then each provider in the fallback chain in
    order, stopping at the first success. Raises ``OrchestratorUnavailable`` only
    when every provider fails (or when no chain is configured and the primary
    fails), so the caller then fails open to direct submit.
    """
    model, base_url, api_key, timeout_s = _read_config()
    providers = [(model, base_url, api_key), *_read_fallback_chain()]
    last_exc: OrchestratorUnavailable | None = None
    for i, (m, b, k) in enumerate(providers):
        try:
            return await _attempt_call(m, b, k, timeout_s, system, user)
        except OrchestratorUnavailable as exc:
            last_exc = exc
            if i + 1 < len(providers):
                logger.warning(
                    "orchestrator provider %s unavailable (%s); trying next: %s",
                    m,
                    exc,
                    providers[i + 1][0],
                )
    raise last_exc or OrchestratorUnavailable("no orchestrator providers configured")
