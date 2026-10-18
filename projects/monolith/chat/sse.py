import asyncio
import json
from collections.abc import Awaitable, Callable

import anyio
from fastapi.responses import StreamingResponse


class SSEEmitter:
    """Async queue-backed SSE event emitter.

    Tools call emit() to push events. The endpoint iterates stream()
    to drain them as text/event-stream lines.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._closed = False

    def emit(self, event_type: str, data: dict) -> None:
        if self._closed:
            return
        payload = json.dumps({"type": event_type, "data": data})
        self._queue.put_nowait(f"data: {payload}\n\n")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)

    async def stream(self):
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                break
            yield chunk


class SSEStreamingResponse(StreamingResponse):
    """Own a producer for exactly the lifetime of the response body."""

    def __init__(self, emitter: SSEEmitter, produce: Callable[[], Awaitable[None]]):
        async def run():
            try:
                await produce()
            finally:
                emitter.close()

        async def generate():
            producer = asyncio.create_task(run())
            try:
                async for event in emitter.stream():
                    yield event
                await producer
            finally:
                if not producer.done():
                    producer.cancel()
                # Starlette's disconnect task group has already been cancelled.
                # Shield the join, including repeated direct Task.cancel calls.
                joined = asyncio.gather(producer, return_exceptions=True)
                cancelled = False
                with anyio.CancelScope(shield=True):
                    while not joined.done():
                        try:
                            await asyncio.shield(joined)
                        except asyncio.CancelledError:
                            cancelled = True
                emitter.close()
                if cancelled:
                    raise asyncio.CancelledError

        super().__init__(generate(), media_type="text/event-stream")

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A failed ASGI send can leave generate suspended at its yield.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()
