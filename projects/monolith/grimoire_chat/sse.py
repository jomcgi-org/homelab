"""SSE frame helpers for the grimoire chat stream.

The grimoire chat message endpoint streams real vLLM tokens incrementally. Like
``chat_public.sse`` (this is a verbatim copy), the path is a single async
generator: the endpoint awaits the model stream and yields SSE frames directly as
tokens arrive, so there is no second producer and no queue, sidestepping the
cross-thread / cross-task safety concern entirely.

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
