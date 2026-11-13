"""Default-off HTTP write authorization after verified principal resolution."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from starlette.responses import JSONResponse

from auth.api import (
    AgentWriteBinding,
    Authority,
    Principal,
    current_principal,
    current_write_binding,
)
from core.env import flag

WRITE_POLICY_ENV = "AGENTS_WRITE_POLICY_ENFORCED"
WRITE_TOOLS = frozenset(
    {
        "report_knowledge",
        "report_distress",
        "dispute_fact",
        "post_message",
        "ack_message",
    }
)
BindingResolver = Callable[[Principal], AgentWriteBinding | None]
logger = logging.getLogger(__name__)


class AgentWritePolicy:
    """Inspect the same JSON bytes MCP consumes, then replay every ASGI frame.

    JSON-RPC batches are inspected too, even if the downstream MCP version
    rejects them. Malformed JSON cannot execute a tool and is left to MCP.
    """

    def __init__(self, app: Any, binding_resolver: BindingResolver | None = None):
        self.app = app
        self.binding_resolver = binding_resolver

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        binding = None
        principal = current_principal()
        if self.binding_resolver is not None:
            try:
                candidate = self.binding_resolver(principal)
                if isinstance(candidate, AgentWriteBinding) and candidate.valid_for(
                    principal
                ):
                    binding = candidate
            except Exception:
                logger.warning("agent write binding unavailable", exc_info=True)
        token = current_write_binding.set(binding)
        try:
            if flag(WRITE_POLICY_ENV):
                frames = []
                body = bytearray()
                while True:
                    frame = await receive()
                    frames.append(frame)
                    if frame["type"] == "http.disconnect":
                        return
                    body.extend(frame.get("body", b""))
                    if not frame.get("more_body", False):
                        break
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    payload = None
                messages = payload if isinstance(payload, list) else [payload]
                writes = any(
                    isinstance(message, dict)
                    and message.get("method") == "tools/call"
                    and isinstance(message.get("params"), dict)
                    and isinstance(message["params"].get("name"), str)
                    and message["params"].get("name") in WRITE_TOOLS
                    for message in messages
                )
                operator = (
                    principal.authority is Authority.STANDING
                    and principal.has_group("operators")
                )
                if writes and not operator and binding is None:
                    await JSONResponse(
                        {"detail": "write principal not permitted"}, status_code=403
                    )(scope, receive, send)
                    return
                original_receive = receive
                iterator = iter(frames)

                async def replay():
                    frame = next(iterator, None)
                    return frame if frame is not None else await original_receive()

                receive = replay
            await self.app(scope, receive, send)
        finally:
            current_write_binding.reset(token)
