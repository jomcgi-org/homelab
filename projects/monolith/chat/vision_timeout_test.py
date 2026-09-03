"""Tests for VisionClient -- retry behavior, timeout/connect errors, and env var fallback.

vision_errors_test.py covers HTTP status errors and malformed response shapes.
vision_test.py covers the happy path and base64 encoding.
This file covers:
  - Retry with exponential backoff on transient errors
  - Non-retryable errors propagate immediately
  - VisionClient() fallback to LLAMA_CPP_URL env var when no base_url given
  - Payload fields: model, max_tokens, system prompt content
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from chat.vision import VisionClient, VISION_SYSTEM_PROMPT
import shared.inference


def _make_mock_http_client(response=None, side_effect=None):
    """Build an async-context-manager mock for httpx.AsyncClient."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    if side_effect is not None:
        mock_client.post.side_effect = side_effect
    else:
        mock_client.post.return_value = response
    return mock_client


def _ok_response(content: str = "A nice image") -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


def _server_error_response(status_code: int = 503) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Service Unavailable",
        request=MagicMock(),
        response=resp,
    )
    return resp


# ---------------------------------------------------------------------------
# Retry on transient errors
# ---------------------------------------------------------------------------


class TestVisionClientRetry:
    @pytest.mark.asyncio
    async def test_retries_on_connect_error_then_succeeds(self):
        """describe() retries on ConnectError and returns when the server comes back."""
        client = VisionClient(base_url="http://fake:8080")

        with (
            patch("chat.vision.httpx.AsyncClient") as mock_cls,
            patch("chat.vision.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_http = _make_mock_http_client(
                side_effect=[
                    httpx.ConnectError("Connection refused"),
                    httpx.ConnectError("Connection refused"),
                    _ok_response("A cat sitting on a mat"),
                ]
            )
            mock_cls.return_value = mock_http
            result = await client.describe(b"\x89PNG", "image/png")

        assert result == "A cat sitting on a mat"
        assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    async def test_retries_on_connect_timeout_then_succeeds(self):
        """describe() retries on ConnectTimeout."""
        client = VisionClient(base_url="http://fake:8080")

        with (
            patch("chat.vision.httpx.AsyncClient") as mock_cls,
            patch("chat.vision.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_http = _make_mock_http_client(
                side_effect=[
                    httpx.ConnectTimeout("connect timed out"),
                    _ok_response("A dog"),
                ]
            )
            mock_cls.return_value = mock_http
            result = await client.describe(b"\x89PNG", "image/png")

        assert result == "A dog"

    @pytest.mark.asyncio
    async def test_retries_on_5xx_then_succeeds(self):
        """describe() retries on 5xx HTTP status errors."""
        client = VisionClient(base_url="http://fake:8080")

        with (
            patch("chat.vision.httpx.AsyncClient") as mock_cls,
            patch("chat.vision.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_http = _make_mock_http_client(
                side_effect=[
                    _server_error_response(503),
                    _ok_response("A bird"),
                ]
            )
            # httpx.AsyncClient().post() calls raise_for_status via
            # our code, but the mock returns the response directly.
            # We need the mock to raise on first call, return on second.
            # Re-wire: make post return the response, but the response's
            # raise_for_status raises.
            mock_cls.return_value = mock_http
            result = await client.describe(b"\x89PNG", "image/png")

        assert result == "A bird"

    @pytest.mark.asyncio
    async def test_retries_on_read_timeout_then_succeeds(self):
        """describe() retries on ReadTimeout."""
        client = VisionClient(base_url="http://fake:8080")

        with (
            patch("chat.vision.httpx.AsyncClient") as mock_cls,
            patch("chat.vision.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_http = _make_mock_http_client(
                side_effect=[
                    httpx.ReadTimeout("read timed out"),
                    _ok_response("A fish"),
                ]
            )
            mock_cls.return_value = mock_http
            result = await client.describe(b"\x89PNG", "image/png")

        assert result == "A fish"

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_exhausted(self):
        """describe() raises the last error after all retries are exhausted."""
        client = VisionClient(base_url="http://fake:8080")

        with (
            patch("chat.vision.httpx.AsyncClient") as mock_cls,
            patch("chat.vision.asyncio.sleep", new_callable=AsyncMock),
            patch("chat.vision.VISION_RETRY_TIMEOUT", 0),
        ):
            # Timeout=0 means the first retry delay exceeds the deadline
            mock_http = _make_mock_http_client(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_cls.return_value = mock_http
            with pytest.raises(httpx.ConnectError):
                await client.describe(b"\x89PNG", "image/png")

    @pytest.mark.asyncio
    async def test_exponential_backoff_delays(self):
        """Retry delays follow exponential backoff: 2, 4, 8, ..."""
        client = VisionClient(base_url="http://fake:8080")

        with (
            patch("chat.vision.httpx.AsyncClient") as mock_cls,
            patch("chat.vision.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_http = _make_mock_http_client(
                side_effect=[
                    httpx.ConnectError("refused"),
                    httpx.ConnectError("refused"),
                    httpx.ConnectError("refused"),
                    _ok_response("ok"),
                ]
            )
            mock_cls.return_value = mock_http
            await client.describe(b"\x89PNG", "image/png")

        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == [2.0, 4.0, 8.0]


# ---------------------------------------------------------------------------
# Non-retryable errors propagate immediately
# ---------------------------------------------------------------------------


class TestVisionClientNonRetryable:
    @pytest.mark.asyncio
    async def test_value_error_not_retried(self):
        """Non-retryable errors (e.g. malformed response) propagate immediately."""
        client = VisionClient(base_url="http://fake:8080")

        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock()
        bad_resp.json.return_value = {"choices": []}  # IndexError path

        with patch("chat.vision.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _make_mock_http_client(response=bad_resp)
            with pytest.raises(ValueError, match="unexpected vision response"):
                await client.describe(b"\x89PNG", "image/png")

    @pytest.mark.asyncio
    async def test_4xx_not_retried(self):
        """Client errors (4xx) are not retried."""
        client = VisionClient(base_url="http://fake:8080")

        resp_400 = MagicMock()
        resp_400.status_code = 400
        resp_400.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Bad Request", request=MagicMock(), response=resp_400
        )

        with (
            patch("chat.vision.httpx.AsyncClient") as mock_cls,
            patch("chat.vision.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_cls.return_value = _make_mock_http_client(response=resp_400)
            with pytest.raises(httpx.HTTPStatusError):
                await client.describe(b"\x89PNG", "image/png")

        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Env var fallback (no base_url provided to constructor)
# ---------------------------------------------------------------------------


class TestVisionClientEnvFallback:
    def test_uses_llama_cpp_url_when_no_base_url(self):
        """VisionClient() with no base_url stores the LLAMA_CPP_URL module constant."""
        with patch("chat.vision.LLAMA_CPP_URL", "http://env-llama:8080"):
            client = VisionClient()
        assert client.base_url == "http://env-llama:8080"

    def test_provided_base_url_overrides_env(self):
        """An explicit base_url is used even when LLAMA_CPP_URL is set."""
        with patch("chat.vision.LLAMA_CPP_URL", "http://env-llama:8080"):
            client = VisionClient(base_url="http://explicit:9090")
        assert client.base_url == "http://explicit:9090"

    @pytest.mark.asyncio
    async def test_describe_uses_client_base_url_in_request(self):
        """describe() constructs the request URL from self.base_url."""
        client = VisionClient(base_url="http://myhost:1234")

        with patch("chat.vision.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _make_mock_http_client(response=_ok_response())
            await client.describe(b"\x89PNG", "image/png")

        mock_http_client = mock_cls.return_value
        call_args = mock_http_client.post.call_args
        url = call_args[0][0]
        assert url == "http://myhost:1234/v1/chat/completions"


# ---------------------------------------------------------------------------
# Payload fields verification
# ---------------------------------------------------------------------------


class TestVisionClientPayload:
    @pytest.mark.asyncio
    async def test_payload_includes_correct_model(self):
        """describe() sends the Meta Spark model in the payload."""
        client = VisionClient(base_url="http://fake:8080")

        with patch("chat.vision.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _make_mock_http_client(response=_ok_response())
            await client.describe(b"\x89PNG", "image/png")

        payload = mock_cls.return_value.post.call_args.kwargs.get("json")
        assert payload["model"] == shared.inference.META_SPARK_MODEL

    @pytest.mark.asyncio
    async def test_payload_includes_max_tokens_default(self):
        """describe() sends the VISION_MAX_TOKENS default in the payload."""
        client = VisionClient(base_url="http://fake:8080")

        with patch("chat.vision.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _make_mock_http_client(response=_ok_response())
            await client.describe(b"\x89PNG", "image/png")

        payload = mock_cls.return_value.post.call_args.kwargs.get("json")
        assert payload["max_tokens"] == 8192

    @pytest.mark.asyncio
    async def test_payload_system_prompt_matches_module_constant(self):
        """describe() uses the VISION_SYSTEM_PROMPT constant as the system message."""
        client = VisionClient(base_url="http://fake:8080")

        with patch("chat.vision.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _make_mock_http_client(response=_ok_response())
            await client.describe(b"\x89PNG", "image/png")

        payload = mock_cls.return_value.post.call_args.kwargs.get("json")
        messages = payload["messages"]
        system_messages = [m for m in messages if m["role"] == "system"]
        assert len(system_messages) == 1
        assert system_messages[0]["content"] == VISION_SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_payload_omits_vendor_thinking_parameters(self):
        """describe() does not send Qwen chat-template parameters to Meta."""
        client = VisionClient(base_url="http://fake:8080")

        with patch("chat.vision.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _make_mock_http_client(response=_ok_response())
            await client.describe(b"\x89PNG", "image/png")

        payload = mock_cls.return_value.post.call_args.kwargs.get("json")
        assert "chat_template_kwargs" not in payload

    @pytest.mark.asyncio
    async def test_raises_on_null_content(self):
        """describe() raises ValueError when the model returns content: null."""
        client = VisionClient(base_url="http://fake:8080")

        with patch("chat.vision.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _make_mock_http_client(
                response=_ok_response(content=None)
            )
            with pytest.raises(ValueError, match="empty content"):
                await client.describe(b"\x89PNG", "image/png")

    @pytest.mark.asyncio
    async def test_raises_on_empty_string_content(self):
        """describe() raises ValueError when the model returns content: ""."""
        client = VisionClient(base_url="http://fake:8080")

        with patch("chat.vision.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _make_mock_http_client(
                response=_ok_response(content="")
            )
            with pytest.raises(ValueError, match="empty content"):
                await client.describe(b"\x89PNG", "image/png")
