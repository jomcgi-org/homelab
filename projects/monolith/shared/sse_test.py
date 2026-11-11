import json

from shared.sse import format_sse


def test_format_sse_wraps_one_json_frame():
    frame = format_sse("token", {"text": "hi"})
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    assert json.loads(frame[6:]) == {"type": "token", "data": {"text": "hi"}}
