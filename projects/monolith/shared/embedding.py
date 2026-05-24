"""Embedding client -- calls voyage-4-nano via llama.cpp /v1/embeddings."""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

EMBEDDING_URL = os.environ.get("EMBEDDING_URL", "")

# Retry configuration — generous enough to survive a model reload.
EMBED_MAX_RETRIES = 12
EMBED_RETRY_BASE_DELAY = 2.0  # seconds
EMBED_RETRY_MAX_DELAY = 30.0  # cap per-retry wait
EMBED_RETRY_TIMEOUT = 300.0  # 5 min total deadline

EMBED_CONNECT_TIMEOUT = 5.0
EMBED_READ_TIMEOUT = 30.0
EMBED_BATCH_READ_TIMEOUT = 60.0


def _is_retryable(exc: Exception) -> bool:
    """Return True for transient errors worth retrying."""
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
            httpx.NetworkError,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return True
    return False


class EmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str = "voyage-4-nano",
    ):
        self.base_url = base_url or EMBEDDING_URL
        self.model = model

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string, returning a 1024-dim vector."""
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in a single HTTP call, returning vectors sorted by index.

        Retries with exponential backoff on transient errors (connection
        failures, timeouts, 5xx) for up to 5 minutes so the bot can
        survive a llama-server restart / model reload.
        """
        timeout = httpx.Timeout(EMBED_BATCH_READ_TIMEOUT, connect=EMBED_CONNECT_TIMEOUT)
        elapsed = 0.0
        last_exc: Exception | None = None

        for attempt in range(EMBED_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(  # nosemgrep: tainted-fastapi-http-request-httpx (self.base_url is a config value, not user input)
                        f"{self.base_url}/v1/embeddings",
                        json={"input": texts, "model": self.model},
                    )
                    resp.raise_for_status()
                    try:
                        data = resp.json()["data"]
                        sorted_data = sorted(data, key=lambda item: item["index"])
                        return [item["embedding"] for item in sorted_data]
                    except (KeyError, IndexError, TypeError) as e:
                        raise ValueError(
                            f"unexpected embedding response shape: {e}"
                        ) from e
            except Exception as exc:
                last_exc = exc
                if not _is_retryable(exc):
                    raise

                delay = min(
                    EMBED_RETRY_BASE_DELAY * (2**attempt),
                    EMBED_RETRY_MAX_DELAY,
                )
                elapsed += delay
                if elapsed > EMBED_RETRY_TIMEOUT:
                    break

                logger.warning(
                    "Embedding call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    EMBED_MAX_RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        raise last_exc  # type: ignore[misc]
