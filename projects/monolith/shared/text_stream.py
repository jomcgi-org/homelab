"""Text-only upstream transport; callers own policy and usage accounting.

This intentionally decodes single data lines, not general SSE events. URLs and
raw usage values retain the adapters' existing semantics, including failures
on malformed frame shapes. Consumers must close this generator on early exit.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

import shared.inference


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class RawUsage:
    # Keep unvalidated JSON: Moving forwards it unchanged, while Grimoire
    # converts counts (and raises on malformed values) before emitting text.
    value: Any


async def stream_text(
    *,
    endpoint: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    timeout: httpx.Timeout,
) -> AsyncIterator[TextDelta | RawUsage]:
    """Yield raw usage before text in each chunk; never instrument usage."""
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            endpoint + "/v1/chat/completions",
            headers=shared.inference.auth_headers(endpoint),
            json=body,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
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
                    yield RawUsage(usage)
                for choice in chunk.get("choices") or []:
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        yield TextDelta(text)
