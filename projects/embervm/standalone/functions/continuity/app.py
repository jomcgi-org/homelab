"""Stateful fixture for the standalone EmberVM session walkthrough."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

WORKSPACE = Path("/tmp/ember-quickstart-workspace")
MARKER = WORKSPACE / "marker.txt"


def _response(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, sort_keys=True),
        "isBase64Encoded": False,
    }


def handle(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Write or read a marker in the session VM's writable tmpfs."""
    del context
    try:
        request = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError as exc:
        return _response(400, {"error": f"invalid JSON: {exc.msg}"})

    operation = request.get("op")
    if operation == "write":
        value = request.get("value")
        if not isinstance(value, str) or not value:
            return _response(400, {"error": "write requires a non-empty string value"})
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(value, encoding="utf-8")
        return _response(200, {"op": "write", "value": value})

    if operation == "read":
        if not MARKER.exists():
            return _response(404, {"error": "marker not found"})
        return _response(
            200, {"op": "read", "value": MARKER.read_text(encoding="utf-8")}
        )

    return _response(400, {"error": "op must be write or read"})
