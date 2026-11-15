"""Grimoire's upstream adapter, exercised only against synthetic httpx."""

from types import SimpleNamespace

import httpx
import pytest

from grimoire_chat import inference, limits
from shared.testing.text_stream import AdapterContract, Harness, frame


@pytest.fixture
def adapter(monkeypatch):
    messages = [
        {"role": "system", "content": "Synthetic"},
        {"role": "user", "content": "question"},
    ]
    endpoint = "https://grimoire.invalid/base/"
    monkeypatch.setattr(inference, "INFERENCE_URL", endpoint)
    monkeypatch.setattr(inference, "MODEL", "grimoire-test-model")
    timeout = httpx.Timeout(43, connect=3)
    monkeypatch.setattr(inference, "_TIMEOUT", timeout)

    def assert_output(output, expected, usage):
        assert all(isinstance(item, inference.TokenDelta) for item in output[:-1])
        assert "".join(item.text for item in output[:-1]) == expected
        assert isinstance(output[-1], inference.Usage)
        assert sum(isinstance(item, inference.Usage) for item in output) == 1
        if usage is None:
            assert output[-1] == inference.Usage(
                limits.estimate_tokens("".join(m["content"] for m in messages)),
                limits.estimate_tokens(expected),
                estimated=True,
            )
        else:
            assert output[-1] == inference.Usage(
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
            )

    return SimpleNamespace(
        stream=lambda: inference.stream_chat(messages, max_tokens=321),
        text=lambda event: event.text,
        assert_output=assert_output,
        endpoint=endpoint,
        model=inference.MODEL,
        messages=messages,
        cap=321,
        timeout=timeout,
        caller="grimoire_chat",
        http_error=inference.InferenceError,
    )


class TestGrimoireStream(AdapterContract):
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "usage, error",
        [
            ("bad", AttributeError),
            ([1], AttributeError),
            ({"prompt_tokens": "bad"}, ValueError),
            ({"completion_tokens": [], "prompt_tokens": {}}, None),
            ({"completion_tokens": [1]}, TypeError),
        ],
    )
    async def test_usage_processed_before_same_chunk_text(
        self, monkeypatch, adapter, usage, error
    ):
        harness = Harness(
            monkeypatch,
            [
                frame(
                    {"usage": usage, "choices": [{"delta": {"content": "same chunk"}}]}
                )
            ],
        )
        stream = adapter.stream()
        if error:
            with pytest.raises(error):
                await anext(stream)
            assert harness.usage_calls == []
        else:
            output = [item async for item in stream]
            adapter.assert_output(output, "same chunk", usage)
            assert harness.usage_calls == [(usage, adapter.model, adapter.caller)]
        harness.assert_closed()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "usage",
        [{"total_tokens": 5}, {"prompt_tokens": None, "completion_tokens": "0"}],
    )
    async def test_reported_zero_counts_are_not_estimated(
        self, monkeypatch, adapter, usage
    ):
        harness = Harness(monkeypatch, [frame({"usage": usage})])
        output = [item async for item in adapter.stream()]
        adapter.assert_output(output, "", usage)
        assert output == [inference.Usage(0, 0)]
        assert harness.usage_calls == [(usage, adapter.model, adapter.caller)]

    @pytest.mark.asyncio
    async def test_every_usage_is_converted(self, monkeypatch, adapter):
        harness = Harness(
            monkeypatch,
            [
                frame({"usage": {"prompt_tokens": "bad"}}),
                frame({"usage": {"prompt_tokens": 1}}),
            ],
        )
        with pytest.raises(ValueError):
            await anext(adapter.stream())
        harness.assert_closed()
        assert harness.usage_calls == []
