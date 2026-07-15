"""Minimal echo function for the EmberVM zip lane (R1, ADR embervm/002).

This is the smoke-test fixture that proves the whole zip lane end to end: a
`Workload` CR with `source.zip {runtime: python312, handler: app.handle}` boots
the runtime-python base, whose shim (see ../../shim.py) unpacks this archive,
imports `handle`, and serves the frozen HTTP-over-vsock guest contract. A task
submit restores a microVM, POSTs to the invoke path, and gets this handler's
response back.

`handle(event, context)` returns the marshaled event it received, so the caller
can assert the round-trip byte-for-byte. The response is an EXPLICIT response
dict in the shim's normative shape ({statusCode, headers, body, isBase64Encoded})
rather than a bare value, so the wire form is unambiguous:

  - statusCode 200,
  - a JSON body that is the event dict the shim built (httpMethod, path,
    queryStringParameters, headers, body, isBase64Encoded),
  - Content-Type application/json.

The handler is restore-safe (ADR embervm/002 "Restore-safe contract"): it reads
no wall-clock and draws no entropy at import or in the body, so every restored
invoke behaves identically. It is stdlib-only, so it imports cleanly on the
baked runtime subset with no extra dependency.
"""

from __future__ import annotations

import json
from typing import Any


def handle(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Echo the received event back as a JSON response body.

    The returned dict is the shim's normative response shape. `body` is the
    JSON encoding of the event the shim marshaled from the inbound request, so
    a caller can POST a payload and read its own marshaled event back out.
    """
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(event),
        "isBase64Encoded": False,
    }
