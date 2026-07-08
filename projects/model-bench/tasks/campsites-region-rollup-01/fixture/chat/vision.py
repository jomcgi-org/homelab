"""Vision client -- calls Qwen 3 via llama.cpp /v1/chat/completions for image description."""

import asyncio
import base64
import logging
import os

import httpx

logger = logging.getLogger(__name__)

LLAMA_CPP_URL = os.environ.get("LLAMA_CPP_URL", "")

VISION_SYSTEM_PROMPT = (
    "Describe this image concisely for semantic search. "
    "Focus on the key subjects, actions, and notable details."
)

# Retry configuration — generous enough to survive a full model reload (~3-5 min).
VISION_MAX_RETRIES = 12
VISION_RETRY_BASE_DELAY = 2.0  # seconds
VISION_RETRY_MAX_DELAY = 30.0  # cap per-retry wait
VISION_RETRY_TIMEOUT = 300.0  # 5 min total deadline

# Separate connect/read timeouts so connection failures fail fast for retries
# while slow vision inference still has time to complete.
VISION_CONNECT_TIMEOUT = 5.0
VISION_READ_TIMEOUT = 60.0
VISION_MAX_TOKENS = int(os.environ.get("VISION_MAX_TOKENS", "8192"))


def _is_retryable(exc: Exception) -> bool:
    """Return True for transient errors worth retrying."""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    if isinstance(exc, httpx.ReadTimeout):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return True
    return False


class VisionClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = base_url or LLAMA_CPP_URL

    async def describe(self, image_bytes: bytes, content_type: str) -> str:
        """Describe an image using Qwen 3 vision, returning a text summary.

        Retries with exponential backoff on transient errors (connection
        failures, timeouts, 5xx) for up to 5 minutes so the bot can survive
        a llama-server restart / model reload.
        """
        b64 = base64.b64encode(image_bytes).decode()
        data_uri = f"data:{content_type};base64,{b64}"

        payload = {
            "model": "qwen3.6-27b",
            "messages": [
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image."},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                },
            ],
            "max_tokens": VISION_MAX_TOKENS,
            # Disable thinking so tokens are used for the description, not
            # <think> reasoning that fills the entire max_tokens budget.
            "chat_template_kwargs": {"enable_thinking": False},
        }

        timeout = httpx.Timeout(VISION_READ_TIMEOUT, connect=VISION_CONNECT_TIMEOUT)
        elapsed = 0.0
        last_exc: Exception | None = None

        for attempt in range(VISION_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{self.base_url}/v1/chat/completions",
                        json=payload,
                    )
                    resp.raise_for_status()
                    try:
                        content = resp.json()["choices"][0]["message"]["content"]
                    except (KeyError, IndexError) as e:
                        raise ValueError(
                            f"unexpected vision response shape: {e}"
                        ) from e
                    if not content:
                        raise ValueError("vision response returned empty content")
                    return content
            except Exception as exc:
                last_exc = exc
                if not _is_retryable(exc):
                    raise

                delay = min(
                    VISION_RETRY_BASE_DELAY * (2**attempt),
                    VISION_RETRY_MAX_DELAY,
                )
                elapsed += delay
                if elapsed > VISION_RETRY_TIMEOUT:
                    break

                logger.warning(
                    "Vision call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    VISION_MAX_RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

        raise last_exc  # type: ignore[misc]
