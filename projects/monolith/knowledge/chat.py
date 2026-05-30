"""Notes chat: RAG over public (or all) notes streamed from in-cluster Qwen.

Rate limiting for the public endpoint: a per-IP token bucket that allows
RATE_LIMIT_RPM requests per minute with a burst equal to RATE_LIMIT_BURST.
Limit state lives in-process; across multiple replicas each pod enforces
independently (fine for homelab abuse-prevention, not accounting-grade).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

# Public endpoint rate limit: 5 req/min per IP, burst up to 5.
RATE_LIMIT_RPM = int(os.environ.get("PUBLIC_CHAT_RATE_LIMIT_RPM", "5"))
RATE_LIMIT_BURST = int(os.environ.get("PUBLIC_CHAT_RATE_LIMIT_BURST", "5"))

# vLLM / llama.cpp endpoint (OpenAI-compatible).
LLAMA_CPP_URL = os.environ.get("LLAMA_CPP_URL", "")
MODEL_NAME = os.environ.get("NOTES_CHAT_MODEL", "qwen3.6-27b")
MAX_CONTEXT_NOTES = 6
MAX_TOKENS = 512

_SYSTEM_PROMPT = (
    "You are a concise assistant that answers questions about Joe's personal knowledge base. "
    "You are given a selection of relevant note snippets as context. "
    "Answer only from the provided context. If the context does not contain enough information "
    "to answer confidently, say so. Keep answers short and precise — this is a knowledge lookup, "
    "not a chat. Do not invent facts not supported by the notes."
)


class _Bucket:
    """Token bucket for one IP address."""

    __slots__ = ("tokens", "last_refill", "_lock")

    def __init__(self) -> None:
        self.tokens: float = float(RATE_LIMIT_BURST)
        self.last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    async def consume(self) -> tuple[bool, int, float]:
        """Try to consume one token.

        Returns (allowed, remaining, reset_at_epoch).
        reset_at_epoch is the wall-clock second when the bucket will be full.
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            refill = elapsed * (RATE_LIMIT_RPM / 60.0)
            self.tokens = min(float(RATE_LIMIT_BURST), self.tokens + refill)
            self.last_refill = now

            remaining_tokens = max(0, int(self.tokens) - 1)
            seconds_to_full = (RATE_LIMIT_BURST - self.tokens) / (RATE_LIMIT_RPM / 60.0)
            reset_at = time.time() + max(0.0, seconds_to_full)

            if self.tokens >= 1:
                self.tokens -= 1
                return True, remaining_tokens, reset_at
            return False, 0, reset_at


class PublicNotesRateLimiter:
    """Per-IP token-bucket rate limiter for the public chat endpoint."""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, ip: str) -> tuple[bool, int, float]:
        """Return (allowed, remaining, reset_epoch) for the given IP."""
        async with self._lock:
            if ip not in self._buckets:
                self._buckets[ip] = _Bucket()
        return await self._buckets[ip].consume()


# Module-level singleton shared across requests.
public_limiter = PublicNotesRateLimiter()


def _build_context(results: list[dict]) -> str:
    """Format top search results as a context block for the LLM."""
    parts = []
    for r in results[:MAX_CONTEXT_NOTES]:
        title = r.get("title", "untitled")
        snippet = r.get("snippet", "").strip()
        section = r.get("section", "")
        if section:
            parts.append(f"## {title} — {section}\n{snippet}")
        else:
            parts.append(f"## {title}\n{snippet}")
    return "\n\n".join(parts)


async def stream_chat_response(
    question: str,
    context: str,
    base_url: str | None = None,
) -> AsyncIterator[str]:
    """Yield SSE text lines streaming the Qwen answer.

    Each yielded string is a complete SSE line (``data: {...}\\n\\n``).
    Yields a terminal ``data: [DONE]\\n\\n`` when the stream ends.
    """
    url = (base_url or LLAMA_CPP_URL).rstrip("/") + "/v1/chat/completions"

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Context from notes:\n\n{context}\n\nQuestion: {question}",
        },
    ]

    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": True,
        "max_tokens": MAX_TOKENS,
        # Disable thinking to keep latency acceptable for a web UI.
        "chat_template_kwargs": {"enable_thinking": False},
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    logger.error("notes_chat: upstream %d %s", resp.status_code, body[:200])
                    yield 'data: {"type":"error","message":"inference unavailable"}\n\n'
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    # Parse delta content and re-emit as our own event shape.
                    try:
                        import json

                        obj = json.loads(chunk)
                        delta = obj["choices"][0]["delta"]
                        text = delta.get("content") or ""
                        if text:
                            yield f'data: {json.dumps({"type": "text_chunk", "text": text})}\n\n'
                    except Exception:
                        pass

        yield 'data: {"type":"done"}\n\n'
    except httpx.TimeoutException:
        logger.warning("notes_chat: upstream timeout")
        yield 'data: {"type":"error","message":"inference timed out"}\n\n'
    except Exception:
        logger.exception("notes_chat: unexpected error")
        yield 'data: {"type":"error","message":"internal error"}\n\n'
