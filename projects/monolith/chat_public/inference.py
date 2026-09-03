"""Text-only streaming client for OpenAI-compatible chat inference.

Public chat is text-in / text-out with NO tools and NO function-calling. This is
a deliberate direct httpx call to the OpenAI-compatible
``/v1/chat/completions`` endpoint rather than PydanticAI, precisely so that
guarantee is obvious on the page: the request body carries only
model/messages/max_tokens/stream, and there is no ``tools=`` argument anywhere
that could be populated by accident. The reserved-headroom slot in ``limits.py``
keeps public load bounded (the slot is held for the whole stream).

The base URL is injected from the environment (``CHAT_PUBLIC_INFERENCE_URL``)
with an empty-string default and no hardcoded endpoint. GKE points it at Meta
Spark, while local environments can retain a keyless compatible server.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from chat_public import limits
import shared.inference

logger = logging.getLogger(__name__)

# OpenAI-compatible base URL. Empty default plus values injection means there is
# no hidden network destination in code.
INFERENCE_URL = os.environ.get("CHAT_PUBLIC_INFERENCE_URL", "")

# Pinned contributor-tier model, overridable for tests and operations.
MODEL = os.environ.get("CHAT_PUBLIC_MODEL", shared.inference.META_SPARK_MODEL)

# A streamed generation runs for many seconds; allow a generous read timeout but
# a short connect timeout so an unreachable endpoint fails fast rather than
# hanging a held in-flight slot.
_TIMEOUT = httpx.Timeout(
    float(os.environ.get("CHAT_PUBLIC_INFERENCE_TIMEOUT_SECONDS", "120")),
    connect=float(os.environ.get("CHAT_PUBLIC_INFERENCE_CONNECT_TIMEOUT_SECONDS", "5")),
)


class InferenceError(RuntimeError):
    """The inference endpoint is unset, unreachable, or returned a bad response."""


@dataclass
class TokenDelta:
    """One incremental text chunk from the model stream."""

    text: str


@dataclass
class Usage:
    """Final token accounting for a turn.

    ``estimated`` is True when vLLM did not report usage and the counts are a
    coarse char-based fallback so the per-session budget still advances.
    """

    prompt_tokens: int
    completion_tokens: int
    estimated: bool = False


def _require_url() -> str:
    if not INFERENCE_URL:
        raise InferenceError(
            "CHAT_PUBLIC_INFERENCE_URL is not set; refusing to call inference."
        )
    return INFERENCE_URL


def _payload(messages: list[dict[str, str]], *, max_tokens: int, stream: bool) -> dict:
    """Build the request body. No ``tools``/``functions`` key ever, by design."""
    body: dict = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if stream:
        # Ask vLLM for a trailing usage chunk so per-turn token accounting uses
        # real prompt+completion counts rather than an estimate.
        body["stream_options"] = {"include_usage": True}
    return body


async def stream_chat(
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
) -> AsyncIterator[TokenDelta | Usage]:
    """Stream a text completion for ``messages`` from the shared vLLM.

    Yields a ``TokenDelta`` per text chunk as it arrives, then exactly one
    ``Usage`` at the end. Raises ``InferenceError`` if the endpoint is unset,
    unreachable, or returns a non-2xx status.
    """
    url = _require_url()
    body = _payload(messages, max_tokens=max_tokens, stream=True)
    prompt_tokens = 0
    completion_tokens = 0
    usage_seen = False
    text_parts: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "POST",
                f"{url}/v1/chat/completions",
                headers=shared.inference.auth_headers(url),
                json=body,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    usage = chunk.get("usage")
                    if usage:
                        prompt_tokens = int(usage.get("prompt_tokens") or 0)
                        completion_tokens = int(usage.get("completion_tokens") or 0)
                        usage_seen = True
                    for choice in chunk.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            text_parts.append(delta)
                            yield TokenDelta(text=delta)
    except httpx.HTTPError as exc:
        raise InferenceError(f"inference request failed: {type(exc).__name__}") from exc

    if usage_seen:
        yield Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        return
    # vLLM did not report usage: fall back to a coarse estimate so budgets move.
    prompt_estimate = limits.estimate_tokens(
        "".join(m.get("content", "") for m in messages)
    )
    completion_estimate = limits.estimate_tokens("".join(text_parts))
    yield Usage(
        prompt_tokens=prompt_estimate,
        completion_tokens=completion_estimate,
        estimated=True,
    )


async def complete(messages: list[dict[str, str]], *, max_tokens: int) -> str:
    """Non-streaming completion, used by the compaction summariser.

    Returns the assistant message content. Raises ``InferenceError`` on a bad
    endpoint or an unexpected response shape.
    """
    url = _require_url()
    body = _payload(messages, max_tokens=max_tokens, stream=False)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{url}/v1/chat/completions",
                headers=shared.inference.auth_headers(url),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise InferenceError(f"inference request failed: {type(exc).__name__}") from exc
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise InferenceError(f"unexpected LLM response shape: {exc}") from exc
