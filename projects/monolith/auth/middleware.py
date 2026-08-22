"""Raw ASGI authentication middleware for mounted applications."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse

from auth.dependencies import (
    reset_current_principal,
    resolve_authorization,
    set_current_principal,
)
from auth.errors import AuthError
from auth.verifier import TokenResolver

ASGIApp = Callable[
    [dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]],
    Awaitable[None],
]


class PrincipalMiddleware:
    """Resolve bearer identity before dispatching an HTTP ASGI request.

    The principal a tool reads via current_principal() follows the message only
    when the MCP mount is stateless streamable HTTP (see framework/core.py).
    Under SSE, or stateful streamable HTTP, the server task is started by the
    session opener and every later message runs in that task's context, so
    current_principal() stays pinned to the opener even though this middleware
    resolved (and may have rejected) each POST correctly.
    """

    def __init__(self, app: ASGIApp, resolver: TokenResolver) -> None:
        self.app = app
        self.resolver = resolver

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        authorization = _authorization_header(scope)
        try:
            principal = await resolve_authorization(authorization, self.resolver)
        except AuthError as error:
            response = JSONResponse(
                {"detail": error.detail, "reason": error.reason.value},
                status_code=error.status_code,
                headers=error.headers,
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["principal"] = principal
        context_token = set_current_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_principal(context_token)


def _authorization_header(scope: dict) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            return value.decode("latin-1")
    return None
