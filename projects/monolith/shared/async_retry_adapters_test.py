"""Fake HTTP contract tests for both callers of the retry executor."""

import asyncio

import httpx
import pytest

from chat import vision
from shared import embedding, inference


@pytest.fixture(params=["vision", "embedding"])
def adapter(request, monkeypatch):
    module, prefix = (
        (vision, "VISION") if request.param == "vision" else (embedding, "EMBED")
    )
    # Construct before patching: settings and sleep must resolve at invocation.
    client = (
        vision.VisionClient("http://fake")
        if module is vision
        else embedding.EmbeddingClient(base_url="http://fake", model="custom-model")
    )
    events = []
    instances = []
    outcomes = []
    usage = []

    class FakeClient:
        def __init__(self, *, timeout):
            self.timeout = timeout
            self.active = False
            instances.append(self)

        async def __aenter__(self):
            assert not any(c.active for c in instances)
            self.active = True
            events.append(("enter",))
            return self

        async def __aexit__(self, *args):
            self.active = False
            events.append(("exit",))

        async def post(self, url, **kwargs):
            assert self.active
            events.append(("post", url, kwargs))
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            if isinstance(outcome, httpx.Response):
                return outcome
            return httpx.Response(200, json=outcome, request=httpx.Request("POST", url))

    async def sleep(delay):
        assert not any(c.active for c in instances)
        assert events[-1][0] == "log"
        events.append(("sleep", delay))

    def log(*args):
        assert not any(c.active for c in instances)
        events.append(("log", *args))

    def record_usage(*args):
        assert instances[-1].active
        usage.append(args)

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    monkeypatch.setattr(module.logger, "warning", log)
    monkeypatch.setattr(inference, "auth_headers", lambda url: {"X-Test": url})
    monkeypatch.setattr(inference, "record_usage", record_usage)
    monkeypatch.setattr(inference, "chat_reasoning_effort", lambda: "minimal")

    success = (
        {"choices": [{"message": {"content": "picture"}}], "usage": {"total_tokens": 7}}
        if module is vision
        else {
            "data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]
        }
    )

    async def invoke():
        result = (
            await client.describe(b"image", "image/png")
            if module is vision
            else await client.embed_batch(["a", "b"])
        )
        assert not any(c.active for c in instances)
        assert events[-1] == ("exit",)
        assert result == ("picture" if module is vision else [[1.0], [2.0]])
        return result

    return module, prefix, events, instances, outcomes, usage, success, invoke


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failures,budget,base,cap,attempts,delays",
    [
        (2, 300, 2, 30, 3, [2, 4]),
        (12, 300, 2, 30, 12, [2, 4, 8, 16] + [30] * 8),
        (12, 0, 2, 30, 1, []),
        (12, 3, 2, 30, 2, [2]),
        (12, 6, 2, 30, 3, [2, 4]),
        (4, 300, 3, 5, 5, [3, 5, 5, 5]),
    ],
)
async def test_retry_execution(
    adapter, monkeypatch, failures, budget, base, cap, attempts, delays
):
    module, prefix, events, instances, outcomes, usage, success, invoke = adapter
    for setting, value in [
        ("MAX_RETRIES", 12),
        ("RETRY_BASE_DELAY", base),
        ("RETRY_MAX_DELAY", cap),
        ("RETRY_TIMEOUT", budget),
    ]:
        monkeypatch.setattr(module, f"{prefix}_{setting}", value)
    errors = [httpx.ConnectError(str(i)) for i in range(failures)]
    outcomes.extend([*errors, success])
    if failures < attempts:
        await invoke()
    else:
        with pytest.raises(httpx.ConnectError) as caught:
            await invoke()
        assert caught.value is errors[attempts - 1]
    assert len(instances) == attempts
    assert [e[1] for e in events if e[0] == "sleep"] == delays
    assert sum(e[0] == "exit" for e in events) == attempts
    assert not any(c.active for c in instances)
    label = "Vision" if module is vision else "Embedding"
    assert [e for e in events if e[0] == "log"] == [
        (
            "log",
            f"{label} call failed (attempt %d/%d), retrying in %.1fs: %s",
            i + 1,
            12,
            delay,
            errors[i],
        )
        for i, delay in enumerate(delays)
    ]
    assert len(usage) == (1 if failures < attempts and module is vision else 0)


@pytest.mark.asyncio
async def test_request_shape_usage_and_timeouts(adapter, monkeypatch):
    module, _, events, instances, outcomes, usage, success, invoke = adapter
    connect_name = (
        "VISION_CONNECT_TIMEOUT" if module is vision else "EMBED_CONNECT_TIMEOUT"
    )
    read_name = (
        "VISION_READ_TIMEOUT" if module is vision else "EMBED_BATCH_READ_TIMEOUT"
    )
    monkeypatch.setattr(module, connect_name, 7.0)
    monkeypatch.setattr(module, read_name, 83.0)
    outcomes.append(success)
    await invoke()
    _, url, kwargs = next(e for e in events if e[0] == "post")
    assert len(instances) == 1
    assert instances[0].timeout.connect == 7
    assert instances[0].timeout.read == 83
    assert instances[0].timeout.write == 83
    assert instances[0].timeout.pool == 83
    if module is vision:
        assert url == "http://fake/v1/chat/completions"
        assert kwargs == {
            "headers": {"X-Test": "http://fake"},
            "json": {
                "model": inference.META_SPARK_MODEL,
                "reasoning_effort": "minimal",
                "messages": [
                    {"role": "system", "content": vision.VISION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this image."},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                            },
                        ],
                    },
                ],
                "max_tokens": vision.VISION_MAX_TOKENS,
            },
        }
        assert usage == [({"total_tokens": 7}, inference.META_SPARK_MODEL, "vision")]
    else:
        assert url == "http://fake/v1/embeddings"
        assert kwargs == {"json": {"input": ["a", "b"], "model": "custom-model"}}
        assert usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [httpx.WriteError, httpx.ReadError, httpx.PoolTimeout, httpx.RemoteProtocolError],
)
async def test_distinct_transport_predicates(adapter, error_type):
    module, _, events, instances, outcomes, _, success, invoke = adapter
    error = error_type("transport failure")
    outcomes.extend([error, success])
    if module is vision:
        with pytest.raises(error_type) as caught:
            await invoke()
        assert caught.value is error
        assert len(instances) == 1
        assert not any(e[0] == "sleep" for e in events)
    else:
        await invoke()
        assert len(instances) == 2
        assert [e[1] for e in events if e[0] == "sleep"] == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 503])
async def test_http_status(adapter, status):
    _, _, events, instances, outcomes, _, success, invoke = adapter
    response = httpx.Response(status, request=httpx.Request("POST", "http://fake"))
    outcomes.extend([response, success])
    if status == 400:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await invoke()
        assert caught.value.response is response
        assert len(instances) == 1
        assert not any(e[0] == "sleep" for e in events)
    else:
        await invoke()
        assert len(instances) == 2


@pytest.mark.asyncio
async def test_attempt_limit_patched_after_construction(adapter, monkeypatch):
    module, prefix, events, instances, outcomes, _, _, invoke = adapter
    monkeypatch.setattr(module, f"{prefix}_MAX_RETRIES", 2)
    error = httpx.ConnectError("last error")
    outcomes.extend([httpx.ConnectError("first error"), error])
    with pytest.raises(httpx.ConnectError) as caught:
        await invoke()
    assert caught.value is error
    assert len(instances) == 2
    assert [e[1] for e in events if e[0] == "sleep"] == [2, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"choices": [], "data": [{}]}])
async def test_malformed_response(adapter, body):
    _, _, events, instances, outcomes, usage, _, invoke = adapter
    outcomes.append(body)
    with pytest.raises(ValueError, match="unexpected .* response shape"):
        await invoke()
    assert len(instances) == 1
    assert events[-1] == ("exit",)
    assert usage == []
    assert not any(e[0] == "sleep" for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["operation", "sleep"])
async def test_cancellation(adapter, monkeypatch, failure_at):
    module, _, events, instances, outcomes, usage, _, invoke = adapter
    error = asyncio.CancelledError("cancelled")
    outcomes.append(error if failure_at == "operation" else httpx.ConnectError("retry"))

    async def cancel_sleep(delay):
        assert not any(c.active for c in instances)
        events.append(("sleep", delay))
        raise error

    if failure_at == "sleep":
        monkeypatch.setattr(module.asyncio, "sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError) as caught:
        await invoke()
    assert caught.value is error
    assert len(instances) == 1
    assert sum(e[0] == "exit" for e in events) == 1
    assert not any(c.active for c in instances)
    assert usage == []
    assert sum(e[0] == "sleep" for e in events) == (failure_at == "sleep")
