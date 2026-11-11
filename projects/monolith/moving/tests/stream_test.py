"""Moving's upstream-only streaming contract, without a provider or database."""

from types import SimpleNamespace

import httpx
import pytest

from moving import chat
from shared.testing.text_stream import AdapterContract, Harness, frame, text


@pytest.fixture
def adapter(monkeypatch):
    messages = [{"role": "user", "content": "A synthetic question"}]
    endpoint = "https://moving.invalid/base/"
    monkeypatch.setattr(chat, "MOVING_CHAT_MODEL", "moving-test-model")
    monkeypatch.setattr(chat, "MAX_TOKENS", 123)
    timeout = httpx.Timeout(31, connect=2)
    monkeypatch.setattr(chat, "_TIMEOUT", timeout)

    def assert_output(output, expected, usage):
        assert all(isinstance(item, str) for item in output)
        assert "".join(output) == expected

    return SimpleNamespace(
        stream=lambda: chat.stream_chat(messages, endpoint),
        text=lambda event: event,
        assert_output=assert_output,
        endpoint=endpoint,
        model=chat.MOVING_CHAT_MODEL,
        messages=messages,
        cap=chat.MAX_TOKENS,
        timeout=timeout,
        caller="moving",
        http_error=httpx.HTTPError,
    )


class TestMovingStream(AdapterContract):
    @pytest.mark.asyncio
    async def test_url_normalization_stays_with_caller(self, monkeypatch, adapter):
        monkeypatch.setattr(chat, "INFERENCE_URL", adapter.endpoint + "/")
        endpoint = chat._inference_url()
        assert endpoint == adapter.endpoint.rstrip("/")
        harness = Harness(monkeypatch, [])
        assert [
            item async for item in chat.stream_chat(adapter.messages, endpoint)
        ] == []
        harness.assert_request(
            endpoint, adapter.model, adapter.messages, adapter.cap, adapter.timeout
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "usage", ["raw usage", [1], {"prompt_tokens": "not a count"}]
    )
    async def test_usage_remains_raw(self, monkeypatch, adapter, usage):
        harness = Harness(monkeypatch, [frame({"usage": usage}), text("ok")])
        assert [item async for item in adapter.stream()] == ["ok"]
        assert harness.usage_calls == [(usage, adapter.model, "moving")]

    @pytest.mark.asyncio
    async def test_bad_choices_skip_accounting(self, monkeypatch, adapter):
        harness = Harness(
            monkeypatch, [frame({"usage": {"prompt_tokens": 1}, "choices": [None]})]
        )
        with pytest.raises(AttributeError):
            await anext(adapter.stream())
        harness.assert_closed()
        assert harness.usage_calls == []
