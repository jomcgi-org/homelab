from unittest.mock import Mock

import httpx
import pytest

import shared.inference
from swarm.classifier import classify_task_with_outcome


class FakeResponse:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": self.content}}],
            "usage": self.usage,
        }


@pytest.mark.asyncio
async def test_records_response_usage(monkeypatch):
    usage = {
        "prompt_tokens": 21,
        "completion_tokens": 4,
        "total_tokens": 25,
    }
    record_usage = Mock()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return FakeResponse("CLASSIFICATION: planned", usage)

    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("shared.inference.record_usage", record_usage)

    classification, _, outcome, _ = await classify_task_with_outcome("build it")

    assert classification == "planned"
    assert outcome == "success"
    record_usage.assert_called_once_with(
        usage, shared.inference.META_SPARK_MODEL, "classifier"
    )


@pytest.mark.asyncio
async def test_classifies_plain_and_decorated_lines(monkeypatch):
    responses = ["CLASSIFICATION: planned", "```\n**CLASSIFICATION: one_shot**\n```"]

    async def async_post(*args, **kwargs):
        return FakeResponse(responses.pop(0))

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return await async_post(*args, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    classification, _, _, _ = await classify_task_with_outcome("build a feature")
    assert classification == "planned"
    classification, _, _, _ = await classify_task_with_outcome("answer a question")
    assert classification == "one_shot"


@pytest.mark.asyncio
async def test_unparseable_fails_closed(monkeypatch):
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return FakeResponse("maybe")

    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    classification, _latency, outcome, refusal = await classify_task_with_outcome(
        "task"
    )
    assert classification == "one_shot"
    assert outcome == "unparseable"
    assert refusal == "unparseable response"


@pytest.mark.asyncio
async def test_timeout_fails_closed(monkeypatch):
    async def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("slow")

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return await timeout(*args, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    classification, _latency, outcome, refusal = await classify_task_with_outcome(
        "task"
    )
    assert classification == "one_shot"
    assert outcome == "timeout"
    assert refusal == "classifier timeout"


@pytest.mark.asyncio
async def test_error_fails_closed(monkeypatch):
    async def failure(*args, **kwargs):
        raise RuntimeError("down")

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return await failure(*args, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    classification, _latency, outcome, refusal = await classify_task_with_outcome(
        "task"
    )
    assert classification == "one_shot"
    assert outcome == "error"
    assert refusal == "down"


@pytest.mark.asyncio
async def test_request_has_bounded_budget_without_vendor_params(monkeypatch):
    """The classifier keeps its budget without sending Qwen-only parameters."""
    captured = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            captured["json"] = kwargs["json"]
            return FakeResponse("CLASSIFICATION: planned")

    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    classification, _, outcome, _ = await classify_task_with_outcome("ship a feature")

    assert classification == "planned"
    assert outcome == "success"
    assert "chat_template_kwargs" not in captured["json"]
    assert captured["json"]["max_tokens"] >= 256
    assert captured["timeout"] >= 10
