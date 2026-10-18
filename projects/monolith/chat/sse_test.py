import asyncio
import json

import pytest

from chat.sse import SSEEmitter, SSEStreamingResponse


@pytest.mark.anyio
async def test_emit_and_drain():
    emitter = SSEEmitter()
    emitter.emit("node_discovered", {"note_id": "abc", "title": "Test"})
    emitter.emit("done", {})
    emitter.close()

    events = []
    async for chunk in emitter.stream():
        events.append(chunk)

    assert len(events) == 2
    first = json.loads(events[0].removeprefix("data: ").strip())
    assert first["type"] == "node_discovered"
    assert first["data"]["note_id"] == "abc"


@pytest.mark.anyio
async def test_close_terminates_stream():
    emitter = SSEEmitter()
    emitter.close()
    events = []
    async for chunk in emitter.stream():
        events.append(chunk)
    assert events == []


@pytest.mark.anyio
async def test_close_is_idempotent_and_rejects_late_events():
    emitter = SSEEmitter()
    emitter.emit("done", {})
    emitter.close()
    emitter.close()
    emitter.emit("error", {"message": "too late"})
    events = [chunk async for chunk in emitter.stream()]
    assert len(events) == 1
    assert json.loads(events[0].removeprefix("data: "))["type"] == "done"
    assert emitter._queue.empty()


@pytest.mark.anyio
async def test_unhandled_producer_exception_is_observed_and_closes_emitter():
    emitter = SSEEmitter()

    async def produce():
        raise RuntimeError("producer failed")

    response = SSEStreamingResponse(emitter, produce)
    with pytest.raises(RuntimeError, match="producer failed"):
        await asyncio.wait_for(anext(response.body_iterator), 3)
    assert emitter._closed


@pytest.mark.anyio
async def test_producer_cancellation_is_not_an_error_event():
    emitter = SSEEmitter()

    async def produce():
        raise asyncio.CancelledError

    response = SSEStreamingResponse(emitter, produce)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(anext(response.body_iterator), 3)
    assert emitter._closed and emitter._queue.empty()
