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

    Warning: On the SSE transport, the Principal is the stream-opener's and
    remains pinned for the entire session. Per-message POST resolution via the
    context variable is discarded before tool dispatch. Any per-caller result
    scoping built on current_principal() from inside a tool must account for
    this: the identity is not re-verified for each MCP message.
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
