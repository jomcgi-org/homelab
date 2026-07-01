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


class OpenRouterClient:
    def __init__(
        self,
        *,
        api_key: str,
        transport=None,
        base_url: str = "https://openrouter.ai/api/v1",
    ) -> None:
        self._base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(120.0),
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self._prices: dict[str, tuple[float, float]] = {}

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int = 8192,
    ) -> Completion:
        """Send a chat completion request, retrying on 429 and 5xx with exponential backoff.

        Returns a Completion with text, token counts, and wall-clock latency in ms.
        Other 4xx errors are raised immediately without retry.
        """
        t0 = time.monotonic()
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        resp: httpx.Response | None = None
        for attempt in range(5):
            resp = await self._client.post("/chat/completions", json=payload)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < 4:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                # Exhausted retries: raise the last bad response.
                resp.raise_for_status()
            elif resp.status_code >= 400:
                resp.raise_for_status()
            break

        assert resp is not None
        data = resp.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
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
