"""SSE frame helpers for the public chat stream.

The public chat message endpoint streams real vLLM tokens incrementally. Rather
than the queue-backed producer/consumer the private ``chat.sse`` uses (needed
there because a PydanticAI agent emits node events out-of-band while the response
is consumed), the public path is a single async generator: the endpoint awaits
the model stream and yields SSE frames directly as tokens arrive. That sidesteps
the cross-thread / cross-task safety concern entirely (there is no second
producer and no queue), which resolves the earlier Phase-2 caveat about
``asyncio.Queue`` under a live producer.

``format_sse`` is the one place the wire format is defined, so the frame shape is
identical across the token / done / busy / error events.
"""

from __future__ import annotations

import json


def format_sse(event_type: str, data: dict) -> str:
    """Render one ``text/event-stream`` frame.

    The body is a JSON object ``{"type": <event_type>, "data": <data>}`` on a
    single ``data:`` line followed by the blank-line terminator, matching the
    contract the SSR proxy passes straight through.
    """
    payload = json.dumps({"type": event_type, "data": data})
    return f"data: {payload}\n\n"
