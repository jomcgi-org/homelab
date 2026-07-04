"""Tests for chat.orchestrator_client (ADR 036 Phase 1)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from chat.orchestrator_client import (
    OrchestratorUnavailable,
    _read_config,
    _read_fallback_chain,
    call,
    call_tool,
)


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
        assert args[0] == "https://openrouter.ai/api/v1/chat/completions"
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


_TOOL_SCHEMA = {
    "name": "submit_plan",
    "description": "Submit the delegation plan for this task.",
    "parameters": {"type": "object", "properties": {}},
}


_FALLBACK_ENV = {
    "ORCHESTRATOR_MODEL": "nvidia/nemotron-3-ultra-550b-a55b",
    "ORCHESTRATOR_BASE_URL": "https://integrate.api.nvidia.com/v1",
    "ORCHESTRATOR_API_KEY": "nvidia-key",
    "ORCHESTRATOR_FALLBACKS": json.dumps(
        [
            {
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            }
        ]
    ),
    "OPENROUTER_API_KEY": "openrouter-key",
}

# A three-tier chain: NVIDIA primary -> DeepSeek -> in-cluster Qwen (no auth).
_CHAIN_ENV = {
    "ORCHESTRATOR_MODEL": "nvidia/nemotron-3-ultra-550b-a55b",
    "ORCHESTRATOR_BASE_URL": "https://integrate.api.nvidia.com/v1",
    "ORCHESTRATOR_API_KEY": "nvidia-key",
    "ORCHESTRATOR_FALLBACKS": json.dumps(
        [
            {
                "model": "deepseek/deepseek-v4-flash",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
            {
                "model": "qwen3.6-27b",
                "base_url": "http://inference.inference.svc.cluster.local:8080/v1",
                "api_key_env": "",
            },
        ]
    ),
    "OPENROUTER_API_KEY": "openrouter-key",
}


class TestCallFallback:
    @pytest.mark.asyncio
    async def test_falls_back_to_secondary_on_primary_failure(self):
        """A primary transport failure retries on the next chain provider, which
        supplies the result (its model/base URL/key)."""
        payload = {"choices": [{"message": {"content": "fallback brief"}}]}
        instance = _mock_client(
            post_side_effect=[
                httpx.ConnectError("primary refused"),
                _ok_response(payload),
            ]
        )
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", _FALLBACK_ENV, clear=False),
        ):
            result = await call("system", "user")

        assert result.content == "fallback brief"
        assert instance.post.call_count == 2
        # The primary attempt targeted NVIDIA/Nemotron...
        first_args, _ = instance.post.call_args_list[0]
        assert first_args[0] == "https://integrate.api.nvidia.com/v1/chat/completions"
        # ...and the fallback attempt targeted DeepSeek/OpenRouter with its key.
        second_args, second_kwargs = instance.post.call_args_list[1]
        assert second_args[0] == "https://openrouter.ai/api/v1/chat/completions"
        assert second_kwargs["headers"]["Authorization"] == "Bearer openrouter-key"
        assert second_kwargs["json"]["model"] == "deepseek/deepseek-v4-flash"

    @pytest.mark.asyncio
    async def test_walks_full_chain_to_last_provider(self):
        """Primary and first fallback both fail; the third tier (Qwen, no auth)
        succeeds. Confirms an ordered N-tier walk, not a single fallback."""
        payload = {"choices": [{"message": {"content": "qwen brief"}}]}
        instance = _mock_client(
            post_side_effect=[
                httpx.ConnectError("nvidia 429"),
                httpx.ConnectError("openrouter 500"),
                _ok_response(payload),
            ]
        )
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", _CHAIN_ENV, clear=False),
        ):
            result = await call("system", "user")

        assert result.content == "qwen brief"
        assert instance.post.call_count == 3
        third_args, third_kwargs = instance.post.call_args_list[2]
        assert (
            third_args[0]
            == "http://inference.inference.svc.cluster.local:8080/v1/chat/completions"
        )
        # Empty api_key_env => no-auth Bearer (in-cluster Qwen).
        assert third_kwargs["headers"]["Authorization"] == "Bearer "
        assert third_kwargs["json"]["model"] == "qwen3.6-27b"

    @pytest.mark.asyncio
    async def test_raises_when_entire_chain_fails(self):
        instance = _mock_client(
            post_side_effect=[
                httpx.ConnectError("nvidia refused"),
                httpx.ConnectError("openrouter refused"),
                httpx.ConnectError("qwen refused"),
            ]
        )
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", _CHAIN_ENV, clear=False),
        ):
            with pytest.raises(OrchestratorUnavailable):
                await call("system", "user")

        assert instance.post.call_count == 3

    @pytest.mark.asyncio
    async def test_no_chain_configured_is_single_attempt(self, monkeypatch):
        """With no ORCHESTRATOR_FALLBACKS the single-attempt contract holds."""
        monkeypatch.delenv("ORCHESTRATOR_FALLBACKS", raising=False)
        instance = _mock_client(post_side_effect=httpx.ConnectError("refused"))
        env = {"ORCHESTRATOR_MODEL": "m", "OPENROUTER_API_KEY": "k"}
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", env, clear=False),
        ):
            with pytest.raises(OrchestratorUnavailable):
                await call("system", "user")

        assert instance.post.call_count == 1


class TestCallToolFallback:
    @pytest.mark.asyncio
    async def test_tool_call_falls_back_on_unusable_primary_shape(self):
        """A primary response missing tool_calls (weaker forced-tool support)
        degrades to the fallback provider, which returns a valid plan."""
        args_payload = {
            "enabled_subrecipes": [],
            "steps": [],
            "done_criteria": [],
        }
        bad = _ok_response({"choices": [{"message": {"content": "no tool call"}}]})
        good = _ok_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "submit_plan",
                                        "arguments": json.dumps(args_payload),
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        )
        instance = _mock_client(post_side_effect=[bad, good])
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", _FALLBACK_ENV, clear=False),
        ):
            args, response = await call_tool("system", "user", schema=_TOOL_SCHEMA)

        assert args == args_payload
        assert instance.post.call_count == 2
        second_args, second_kwargs = instance.post.call_args_list[1]
        assert second_kwargs["json"]["model"] == "deepseek/deepseek-v4-flash"


class TestReadFallbackChain:
    def test_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("ORCHESTRATOR_FALLBACKS", raising=False)
        assert _read_fallback_chain() == []

    def test_empty_when_unparseable(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_FALLBACKS", "{not json")
        assert _read_fallback_chain() == []

    def test_parses_ordered_chain_and_resolves_keys(self, monkeypatch):
        monkeypatch.setenv(
            "ORCHESTRATOR_FALLBACKS",
            json.dumps(
                [
                    {
                        "model": "deepseek/deepseek-v4-flash",
                        "base_url": "https://openrouter.ai/api/v1",
                        "api_key_env": "OPENROUTER_API_KEY",
                    },
                    {
                        "model": "qwen3.6-27b",
                        "base_url": "http://inference.inference.svc.cluster.local:8080/v1",
                        "api_key_env": "",
                    },
                ]
            ),
        )
        monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
        chain = _read_fallback_chain()
        assert chain == [
            (
                "deepseek/deepseek-v4-flash",
                "https://openrouter.ai/api/v1",
                "openrouter-key",
            ),
            (
                "qwen3.6-27b",
                "http://inference.inference.svc.cluster.local:8080/v1",
                "",
            ),
        ]

    def test_skips_entries_without_a_model(self, monkeypatch):
        monkeypatch.setenv(
            "ORCHESTRATOR_FALLBACKS",
            json.dumps([{"base_url": "https://x/v1"}, {"model": "keep/me"}]),
        )
        chain = _read_fallback_chain()
        assert [m for m, _b, _k in chain] == ["keep/me"]


class TestReadConfig:
    def test_timeout_defaults_to_60_when_unset(self, monkeypatch):
        """The initial-compile budget defaults to 60s (Task 8): it covers both the
        route-decision call and the submit_plan tool call."""
        monkeypatch.delenv("ORCHESTRATOR_TIMEOUT_S", raising=False)
        _model, _base_url, _api_key, timeout_s = _read_config()
        assert timeout_s == 60.0

    def test_timeout_respects_env_value(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_TIMEOUT_S", "42")
        _model, _base_url, _api_key, timeout_s = _read_config()
        assert timeout_s == 42.0

    def test_malformed_timeout_falls_back_to_60(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_TIMEOUT_S", "not-a-number")
        _model, _base_url, _api_key, timeout_s = _read_config()
        assert timeout_s == 60.0


class TestCallTool:
    @pytest.mark.asyncio
    async def test_parses_tool_call_arguments(self):
        args_payload = {
            "enabled_subrecipes": ["query"],
            "steps": [{"sub_recipe": "query", "context": "look it up"}],
            "done_criteria": [],
        }
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "submit_plan",
                                    "arguments": json.dumps(args_payload),
                                }
                            }
                        ]
                    }
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        }
        instance = _mock_client(mock_response=_ok_response(payload))

        env = {"ORCHESTRATOR_MODEL": "m", "OPENROUTER_API_KEY": "k"}
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", env, clear=False),
        ):
            args, response = await call_tool(
                "system", "user", schema=_TOOL_SCHEMA, timeout_s=60
            )

        assert args == args_payload
        assert response.prompt_tokens == 50
        assert response.completion_tokens == 10
        assert response.latency_ms >= 0

        instance.post.assert_called_once()
        _, kwargs = instance.post.call_args
        payload_sent = kwargs["json"]
        assert payload_sent["tools"] == [{"type": "function", "function": _TOOL_SCHEMA}]
        assert payload_sent["tool_choice"] == {
            "type": "function",
            "function": {"name": "submit_plan"},
        }

    @pytest.mark.asyncio
    async def test_http_400_raises_orchestrator_unavailable(self):
        resp = MagicMock()
        resp.status_code = 400
        error = httpx.HTTPStatusError(
            "400 Bad Request", request=MagicMock(), response=resp
        )
        resp.raise_for_status.side_effect = error
        instance = _mock_client(mock_response=resp)

        env = {"ORCHESTRATOR_MODEL": "m", "OPENROUTER_API_KEY": "k"}
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", env, clear=False),
        ):
            with pytest.raises(OrchestratorUnavailable):
                await call_tool("system", "user", schema=_TOOL_SCHEMA)

    @pytest.mark.asyncio
    async def test_missing_tool_calls_raises_orchestrator_unavailable(self):
        payload = {"choices": [{"message": {"content": "no tool call here"}}]}
        instance = _mock_client(mock_response=_ok_response(payload))

        env = {"ORCHESTRATOR_MODEL": "m", "OPENROUTER_API_KEY": "k"}
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", env, clear=False),
        ):
            with pytest.raises(OrchestratorUnavailable):
                await call_tool("system", "user", schema=_TOOL_SCHEMA)

    @pytest.mark.asyncio
    async def test_malformed_json_arguments_raises_orchestrator_unavailable(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "submit_plan",
                                    "arguments": "{not json",
                                }
                            }
                        ]
                    }
                }
            ]
        }
        instance = _mock_client(mock_response=_ok_response(payload))

        env = {"ORCHESTRATOR_MODEL": "m", "OPENROUTER_API_KEY": "k"}
        with (
            patch("chat.orchestrator_client.httpx.AsyncClient", return_value=instance),
            patch.dict("os.environ", env, clear=False),
        ):
            with pytest.raises(OrchestratorUnavailable):
                await call_tool("system", "user", schema=_TOOL_SCHEMA)
