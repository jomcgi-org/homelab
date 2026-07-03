"""ADR 036 orchestrator brief-compiler OpenRouter client.

Unary, short-timeout httpx client shaped like ``summarizer.build_llm_caller``
(summarizer.py:412), but for the paid OpenRouter escalation tier rather than
the local Qwen executor. Per the fail-open philosophy (ADR 036 Architecture:
"Fail open"), this client makes exactly one attempt: any timeout or HTTP error
raises ``OrchestratorUnavailable`` so the caller can fall back to direct
submit. There is no retry loop here (contrast with build_llm_caller's 3
retries), because escalations must degrade quickly, not stall on a paid call.

``call_tool`` is the typed sibling used for the runtime ``submit_plan``
plan (ADR 036 amendment, docs/plans/2026-07-03-deepseek-runtime-recipes.md):
it forces a tool call against a caller-supplied JSON Schema instead of
parsing free-text content. The probe at
``scratchpad/probe_submit_plan.py`` validated a forced tool call
(``tools`` + ``tool_choice``) at 12/12 across trials; OpenRouter's
``response_format: {"type": "json_schema", "json_schema": {...}}`` (also
12/12 in the probe) is the documented drop-in fallback mechanism should
tool-calling ever regress for the pinned model.
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
_DEFAULT_TIMEOUT_S = 10.0


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
    """Read the model, base URL, API key, and timeout from the environment.

    The model is expected to be pinned (never ``:auto``) so briefs stay
    attributable to a specific provider model (ADR 036 Risks table).
    """
    model = os.environ.get("ORCHESTRATOR_MODEL", "")
    base_url = os.environ.get("ORCHESTRATOR_BASE_URL", "") or _DEFAULT_BASE_URL
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    timeout_raw = os.environ.get("ORCHESTRATOR_TIMEOUT_S", "")
    try:
        timeout_s = float(timeout_raw) if timeout_raw else _DEFAULT_TIMEOUT_S
    except ValueError:
        timeout_s = _DEFAULT_TIMEOUT_S
    return model, base_url, api_key, timeout_s


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


async def call(system: str, user: str) -> OrchestratorResponse:
    """Send one system/user turn to the configured OpenRouter model and
    parse the response. Raises ``OrchestratorUnavailable`` on any timeout or
    HTTP error; makes a single attempt (no retries)."""
    model, base_url, api_key, timeout_s = _read_config()

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


async def call_tool(
    system: str,
    user: str,
    *,
    schema: dict,
    tool_name: str = "submit_plan",
    timeout_s: float | None = None,
) -> tuple[dict, OrchestratorResponse]:
    """Force a tool call against ``schema`` and parse its arguments.

    Sends ``tools=[{"type": "function", "function": schema}]`` with
    ``tool_choice`` pinned to ``tool_name``, then parses
    ``choices[0].message.tool_calls[0].function.arguments`` (a JSON string)
    into a dict. Returns ``(args, response)`` where ``response`` mirrors
    ``call()``'s usage/latency accounting (``response.content`` holds the
    raw arguments JSON string).

    Raises ``OrchestratorUnavailable`` on any timeout, HTTP error, missing
    ``tool_calls``, or JSON parse error. Single attempt, no retry loop, same
    fail-open contract as ``call()``. Uses ``timeout_s`` if given, else the
    configured default (see ``_read_config``).
    """
    model, base_url, api_key, default_timeout_s = _read_config()
    effective_timeout_s = timeout_s if timeout_s is not None else default_timeout_s

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(effective_timeout_s)
        ) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "tools": [{"type": "function", "function": schema}],
                    "tool_choice": {
                        "type": "function",
                        "function": {"name": tool_name},
                    },
                },
            )
            resp.raise_for_status()
            payload = resp.json()
    except httpx.TimeoutException as exc:
        raise OrchestratorUnavailable(
            f"orchestrator tool call timed out: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise OrchestratorUnavailable(f"orchestrator tool call failed: {exc}") from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        tool_call = payload["choices"][0]["message"]["tool_calls"][0]
        arguments_raw = tool_call["function"]["arguments"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OrchestratorUnavailable(
            f"unexpected orchestrator tool-call response shape: {exc}"
        ) from exc

    try:
        args = json.loads(arguments_raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise OrchestratorUnavailable(
            f"orchestrator tool-call arguments were not valid JSON: {exc}"
        ) from exc

    prompt_tokens, completion_tokens, cached_tokens = _extract_usage(payload)

    response = OrchestratorResponse(
        content=arguments_raw,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        latency_ms=latency_ms,
    )
    return args, response
