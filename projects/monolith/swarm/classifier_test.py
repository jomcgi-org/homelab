import pytest
import httpx

from swarm.classifier import classify_task_with_outcome


class FakeResponse:
    def __init__(self, content):
        self.content = content

    async def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


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
