import asyncio
import json


class SSEEmitter:
    """Async queue-backed SSE event emitter.

    Tools call emit() to push events. The endpoint iterates stream()
    to drain them as text/event-stream lines.
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
