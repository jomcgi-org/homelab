"""Tests for chat.orchestrator_client (ADR 036 Phase 1)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from chat.orchestrator_client import OrchestratorUnavailable, call


def _mock_client(mock_response=None, post_side_effect=None):
    """Build a MagicMock standing in for httpx.AsyncClient used as an async
    context manager (``async with httpx.AsyncClient(...) as client``)."""
    instance = MagicMock()
    if post_side_effect is not None:
        instance.post = AsyncMock(side_effect=post_side_effect)
    else:
        instance.post = AsyncMock(return_value=mock_response)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    return instance


def _ok_response(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


class TestCallHappyPath:
    @pytest.mark.asyncio
    async def test_parses_content_and_usage(self):
        payload = {
            "choices": [{"message": {"content": "brief text"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
        }
        instance = _mock_client(mock_response=_ok_response(payload))

        env = {
            "ORCHESTRATOR_MODEL": "deepseek/deepseek-chat-v4-flash",
            "ORCHESTRATOR_BASE_URL": "https://openrouter.ai/api/v1",
            "OPENROUTER_API_KEY": "test-key",
            "ORCHESTRATOR_TIMEOUT_S": "10",
        }
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", env, clear=False),
        ):
            result = await call("system prompt", "user prompt")

        assert result.content == "brief text"
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 20
        assert result.cached_tokens == 80
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_missing_usage_fields_return_none(self):
        payload = {"choices": [{"message": {"content": "brief text"}}]}
        instance = _mock_client(mock_response=_ok_response(payload))

        env = {"ORCHESTRATOR_MODEL": "m", "OPENROUTER_API_KEY": "k"}
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", env, clear=False),
        ):
            result = await call("system", "user")

        assert result.content == "brief text"
        assert result.prompt_tokens is None
        assert result.completion_tokens is None
        assert result.cached_tokens is None

    @pytest.mark.asyncio
    async def test_sends_pinned_model_and_auth_header(self):
        payload = {"choices": [{"message": {"content": "ok"}}]}
        instance = _mock_client(mock_response=_ok_response(payload))

        env = {
            "ORCHESTRATOR_MODEL": "deepseek/deepseek-chat-v4-flash",
            "ORCHESTRATOR_BASE_URL": "https://openrouter.ai/api/v1",
            "OPENROUTER_API_KEY": "secret-key",
        }
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", env, clear=False),
        ):
            await call("sys text", "usr text")

        instance.post.assert_called_once()
        args, kwargs = instance.post.call_args
        assert args[0] == "https://openrouter.ai/api/v1/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer secret-key"
        payload_sent = kwargs["json"]
        assert payload_sent["model"] == "deepseek/deepseek-chat-v4-flash"
        assert payload_sent["messages"][0] == {
            "role": "system",
            "content": "sys text",
        }
        assert payload_sent["messages"][1] == {"role": "user", "content": "usr text"}


class TestCallFailures:
    @pytest.mark.asyncio
    async def test_timeout_raises_orchestrator_unavailable(self):
        instance = _mock_client(post_side_effect=httpx.TimeoutException("timed out"))

        env = {"ORCHESTRATOR_MODEL": "m", "OPENROUTER_API_KEY": "k"}
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", env, clear=False),
        ):
            with pytest.raises(OrchestratorUnavailable):
                await call("system", "user")

    @pytest.mark.asyncio
    async def test_http_500_raises_orchestrator_unavailable(self):
        resp = MagicMock()
        resp.status_code = 500
        error = httpx.HTTPStatusError(
            "500 Internal Server Error", request=MagicMock(), response=resp
        )
        resp.raise_for_status.side_effect = error
        instance = _mock_client(mock_response=resp)

        env = {"ORCHESTRATOR_MODEL": "m", "OPENROUTER_API_KEY": "k"}
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", env, clear=False),
        ):
            with pytest.raises(OrchestratorUnavailable):
                await call("system", "user")

    @pytest.mark.asyncio
    async def test_no_retry_on_failure(self):
        """The client makes exactly one attempt; no retry loop."""
        instance = _mock_client(post_side_effect=httpx.ConnectError("refused"))

        env = {"ORCHESTRATOR_MODEL": "m", "OPENROUTER_API_KEY": "k"}
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", env, clear=False),
        ):
            with pytest.raises(OrchestratorUnavailable):
                await call("system", "user")

        assert instance.post.call_count == 1

    @pytest.mark.asyncio
    async def test_unexpected_response_shape_raises_orchestrator_unavailable(self):
        instance = _mock_client(mock_response=_ok_response({"unexpected": "shape"}))

        env = {"ORCHESTRATOR_MODEL": "m", "OPENROUTER_API_KEY": "k"}
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", env, clear=False),
        ):
            with pytest.raises(OrchestratorUnavailable):
                await call("system", "user")
