"""Async OpenRouter client for model completions and pricing.

Provides a thin httpx-based client with injected transport so it can be unit-tested
without network access. Pricing is loaded on demand via /models and stored per-model
as $/1M-tokens tuples so cost accounting is local and fast.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


import httpx


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


@dataclass
class ChatResult:
    message: dict  # full assistant message (content + any tool_calls)
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


_TRANSIENT = (
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)


def _merge_payload(required: dict, extra_body: dict | None) -> dict:
    """Merge extra_body under required fields so model/messages/tools cannot be overwritten."""
    payload = dict(extra_body or {})
    payload.update(required)
    return payload


class OpenRouterClient:
    def __init__(
        self,
        *,
        api_key: str,
        transport=None,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 120.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url
        # extra_headers is merged AFTER Authorization so a caller can override it,
        # and exists because reaching a base_url through Cloudflare Access needs
        # CF-Access-Client-Id / CF-Access-Client-Secret. Access authenticates on
        # those two headers only; it ignores Authorization, which here carries the
        # placeholder key the local endpoint does not check either.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(timeout),
            headers={"Authorization": f"Bearer {api_key}", **(extra_headers or {})},
        )
        self._prices: dict[str, tuple[float, float]] = {}

    async def _post_completions(self, payload: dict) -> httpx.Response:
        """POST /chat/completions, retrying transient transport errors, 429, and 5xx."""
        resp: httpx.Response | None = None
        last_exc: Exception | None = None
        for attempt in range(5):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
            except _TRANSIENT as exc:
                last_exc = exc
                if attempt < 4:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                raise
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < 4:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                resp.raise_for_status()
            elif resp.status_code >= 400:
                resp.raise_for_status()
            return resp
        if resp is not None:
            return resp
        assert last_exc is not None
        raise last_exc

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int = 8192,
        extra_body: dict | None = None,
    ) -> Completion:
        """Send a chat completion request, retrying on 429 and 5xx with exponential backoff.

        Returns a Completion with text, token counts, and wall-clock latency in ms.
        Other 4xx errors are raised immediately without retry.
        """
        t0 = time.monotonic()
        payload = _merge_payload(
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            extra_body,
        )
        data = (await self._post_completions(payload)).json()
        message = data.get("choices", [{}])[0].get("message", {}) or {}
        text = message.get("content") or ""
        usage = data.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        latency_ms = int((time.monotonic() - t0) * 1000)
        return Completion(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        extra_body: dict | None = None,
    ) -> ChatResult:
        """One turn of a tool-using conversation.

        Passes ``tools`` through so the provider enforces the function-call schema
        (the API serializes arguments, so file content never has to be hand-written
        JSON). Returns the full assistant message, which may carry ``tool_calls``.
        """
        t0 = time.monotonic()
        required: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            required["tools"] = tools
        payload = _merge_payload(required, extra_body)
        data = (await self._post_completions(payload)).json()
        message = data.get("choices", [{}])[0].get("message", {}) or {}
        usage = data.get("usage", {})
        return ChatResult(
            message=message,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    async def load_prices(self) -> None:
        """Fetch current model pricing from /models and cache as $/1M-tokens tuples."""
        resp = await self._client.get("/models")
        resp.raise_for_status()
        data = resp.json()
        for model in data.get("data", []):
            try:
                mid = model["id"]
                pricing = model["pricing"]
                prompt_per_million = float(pricing["prompt"]) * 1_000_000
                completion_per_million = float(pricing["completion"]) * 1_000_000
                self._prices[mid] = (prompt_per_million, completion_per_million)
            except (KeyError, TypeError, ValueError):
                # One malformed entry must not abort the full load.
                continue

    def cost_usd(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        """Return the estimated USD cost for a completion given token counts.

        Prices are stored as $/1M-tokens so we divide by 1_000_000.
        Returns 0.0 if the model is not in the price cache.
        """
        prices = self._prices.get(model)
        if prices is None:
            return 0.0
        p_price, c_price = prices
        return (p_price * prompt_tokens + c_price * completion_tokens) / 1_000_000

    async def aclose(self) -> None:
        await self._client.aclose()
