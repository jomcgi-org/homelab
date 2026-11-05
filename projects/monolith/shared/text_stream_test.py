"""The text-only seam owns HTTP lifetime and decoding, never accounting."""

import asyncio
from contextlib import aclosing

import httpx
import pytest

from shared.testing.text_stream import COUNTS, MIXED, Harness, frame, text
from shared.text_stream import RawUsage, TextDelta, stream_text

MESSAGES = [{"role": "user", "content": "Synthetic prompt"}]
TIMEOUT = httpx.Timeout(27, connect=4)


def stream(**extra):
    return stream_text(
        endpoint="https://transport.invalid/base/",
        model="transport-test-model",
        messages=MESSAGES,
        max_tokens=117,
        timeout=TIMEOUT,
        **extra,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("done", [False, True])
async def test_frames_usage_order_and_request(monkeypatch, done):
    first = {"prompt_tokens": 1}
    parts = MIXED + [
        frame({"usage": first, "choices": [{"delta": {"content": "!"}}]}),
        frame({"usage": COUNTS}),
    ]
    if done:
        parts += [
            "data:   [DONE]  \n\n",
            text("ignored"),
            frame(None),
            httpx.ReadError("ignored"),
        ]
    harness = Harness(monkeypatch, parts)
    assert [item async for item in stream()] == [
        TextDelta("Hello"),
        TextDelta(" world"),
        RawUsage(first),
        TextDelta("!"),
        RawUsage(COUNTS),
    ]
    harness.assert_request(
        "https://transport.invalid/base/",
        "transport-test-model",
        MESSAGES,
        117,
        TIMEOUT,
    )
    harness.assert_closed()
    assert harness.usage_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, {}, False, 0, "", []])
async def test_false_usage_is_not_an_event(monkeypatch, usage):
    harness = Harness(monkeypatch, [frame({"usage": usage}), text("ok")])
    assert [item async for item in stream()] == [TextDelta("ok")]
    harness.assert_closed()
    assert harness.usage_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", ["bad", [1], {"prompt_tokens": "bad"}])
async def test_malformed_usage_is_raw_and_precedes_choices(monkeypatch, usage):
    harness = Harness(monkeypatch, [frame({"usage": usage, "choices": [None]})])
    events = stream()
    assert await anext(events) == RawUsage(usage)
    with pytest.raises(AttributeError):
        await anext(events)
    harness.assert_closed()
    assert harness.usage_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [[], None, 3, "bad", {"choices": [None]}, {"choices": [{"delta": "bad"}]}],
)
async def test_malformed_shape_raises_and_closes(monkeypatch, payload):
    harness = Harness(monkeypatch, [frame(payload)])
    with pytest.raises(AttributeError):
        await anext(stream())
    harness.assert_closed()
    assert harness.usage_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status", "read"])
async def test_http_errors_are_unwrapped(monkeypatch, failure):
    error = httpx.ReadError("synthetic failure")
    harness = Harness(
        monkeypatch,
        [error] if failure == "read" else [],
        status=503 if failure == "status" else 200,
    )
    with pytest.raises(httpx.HTTPError) as caught:
        await anext(stream())
    if failure == "read":
        assert caught.value is error
    else:
        assert isinstance(caught.value, httpx.HTTPStatusError)
    harness.assert_closed()
    assert harness.usage_calls == []


@pytest.mark.asyncio
async def test_cancel_read_closes_contexts(monkeypatch):
    harness = Harness(monkeypatch, [None])
    events = stream()
    task = asyncio.create_task(anext(events))
    await asyncio.wait_for(harness.reading.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    harness.assert_closed()
    assert harness.usage_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [text("partial"), frame({"usage": COUNTS})])
async def test_early_close_at_either_event_closes_contexts(monkeypatch, first):
    harness = Harness(monkeypatch, [first, text("unread")])
    events = stream()
    await anext(events)
    await events.aclose()
    harness.assert_closed()
    assert harness.usage_calls == []


@pytest.mark.asyncio
async def test_consumer_failure_closes_suspended_transport(monkeypatch):
    harness = Harness(monkeypatch, [frame({"usage": COUNTS}), text("unread")])
    with pytest.raises(ValueError, match="consumer failure"):
        async with aclosing(stream()) as events:
            async for _ in events:
                raise ValueError("consumer failure")
    harness.assert_closed()
    assert harness.usage_calls == []


@pytest.mark.parametrize(
    "argument",
    ["tools", "functions", "body", "json", "request_body", "kwargs", "stream_options"],
)
def test_no_request_extension_arguments(argument):
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        stream(**{argument: {}})
