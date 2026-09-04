"""Raw ASGI authentication middleware for mounted applications."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from opentelemetry import trace
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

logger = logging.getLogger("monolith.auth")


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
        # Emitted BEFORE dispatch: identity is already settled here and does not
        # depend on the response, so a downstream exception still leaves a record
        # of who the caller was. authorization_present comes from the raw header
        # rather than the principal, because "no token arrived" and "a token
        # arrived and resolved" are the two cases this exists to tell apart.
        try:
            _record_principal(principal, authorization, scope.get("path", ""))
            await self.app(scope, receive, send)
        finally:
            reset_current_principal(context_token)


def _record_principal(principal, authorization: str | None, path: str) -> None:
    """Log and trace the resolved identity without ever touching credentials."""

    try:
        attributes = {
            "subject": principal.subject,
            "kind": principal.kind.value,
            "authority": principal.authority.value,
            "groups": ",".join(principal.groups),
            "authorization_present": authorization is not None,
        }
        # The root formatter is "%(levelname)s %(name)s: %(message)s"
        # (core/log.py), so extra= alone renders nothing. The values have to be
        # in the message itself to survive into the pod log.
        logger.info(
            "principal resolved path=%s subject=%s kind=%s authority=%s "
            "groups=%s authorization_present=%s",
            path,
            attributes["subject"],
            attributes["kind"],
            attributes["authority"],
            attributes["groups"],
            attributes["authorization_present"],
            extra={**attributes, "path": path},
        )

        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attributes(
                {f"monolith.auth.{key}": value for key, value in attributes.items()}
            )
    except Exception:
        # Instrumentation must never fail a request, but a permanently broken
        # path should not be invisible either.
        logger.debug("principal instrumentation failed", exc_info=True)


def _authorization_header(scope: dict) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            return value.decode("latin-1")
    return None
