from __future__ import annotations

import json


def format_sse(event_type: str, data: dict) -> str:
    return f"data: {json.dumps({'type': event_type, 'data': data})}\n\n"
