"""Async queue-backed SSE emitter for the public chat stream.

This is a deliberate copy of the private ``chat.sse.SSEEmitter``. The public
binary must not import anything under ``chat/`` (it is a forbidden private
module in ``app/main_public_imports_test.py`` and is pruned from the public
image by ``_PUBLIC_PRUNE_EXCLUDE`` in ``projects/monolith/BUILD``), so the
transport is duplicated here rather than imported. It is a few lines of stdlib
asyncio with no private dependencies, so duplication is cheaper than a shared
module that would have to live outside both domains.
"""

import asyncio
import json


class SSEEmitter:
    """Async queue-backed SSE event emitter.

    Producers call emit() to push events. The endpoint iterates stream() to
    drain them as text/event-stream lines.

    TODO(Phase 3): in Phase 1 the message endpoint is synchronous and fully
    pre-populates the queue (canned echo) before stream() is ever awaited, so
    there is no concurrent producer and asyncio.Queue's thread-safety is moot.
    When real vLLM token streaming lands, tokens arrive incrementally from an
    async context: make the endpoint async and await the model stream directly,
    or push from the threadpool via loop.call_soon_threadsafe(). Do not keep
    calling put_nowait() from a sync threadpool while stream() is awaited in the
    event loop; that pattern is not safe under a live producer.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    def emit(self, event_type: str, data: dict) -> None:
        payload = json.dumps({"type": event_type, "data": data})
        self._queue.put_nowait(f"data: {payload}\n\n")

    def close(self) -> None:
        self._queue.put_nowait(None)

    async def stream(self):
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                break
            yield chunk
