"""FastAPI and request-context authentication plumbing."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextvars import ContextVar, Token
from functools import lru_cache

from fastapi import Request
from starlette.responses import JSONResponse

from auth.errors import AuthError, AuthErrorReason
from auth.principal import Principal, anonymous_principal
from auth.verifier import TokenResolver, build_default_resolver

logger = logging.getLogger("monolith.auth")

_ANONYMOUS = anonymous_principal()
_current_principal: ContextVar[Principal] = ContextVar(
    "monolith_principal", default=_ANONYMOUS
)


@lru_cache(maxsize=1)
def get_default_resolver() -> TokenResolver:
    """Return the cached default token resolver built from environment.

    For tests that monkeypatch AUTH_* and need a fresh resolver, call
    get_default_resolver.cache_clear().
    """
    return build_default_resolver()


def current_principal() -> Principal:
    """Return the principal associated with the active async context."""

    return _current_principal.get()


def set_current_principal(principal: Principal) -> Token[Principal]:
    return _current_principal.set(principal)


def reset_current_principal(token: Token[Principal]) -> None:
    _current_principal.reset(token)


async def resolve_authorization(
    authorization: str | None,
    resolver: TokenResolver,
) -> Principal:
    """Resolve one Authorization value with explicit absent-token semantics."""

    if authorization is None:
        return _ANONYMOUS

    parts = authorization.strip().split(None, 1)
    if not parts:
        return _ANONYMOUS

    scheme = parts[0]
    if scheme.lower() != "bearer":
        logger.debug("ignoring non-bearer authorization scheme")
        return _ANONYMOUS

    credential = parts[1].strip() if len(parts) == 2 else ""
    if not credential:
        error = AuthError(AuthErrorReason.MALFORMED)
        _log_failure(error)
        raise error

    try:
        return await resolver.resolve(credential)
    except AuthError as error:
        _log_failure(error)
        raise


async def get_principal(request: Request) -> AsyncIterator[Principal]:
    """FastAPI dependency that verifies and stores the request principal."""

    existing = getattr(request.state, "principal", None)
    if isinstance(existing, Principal):
        principal = existing
    else:
        resolver = getattr(request.app.state, "auth_resolver", None)
        if resolver is None:
            resolver = get_default_resolver()
        principal = await resolve_authorization(
            request.headers.get("authorization"), resolver
        )
        request.state.principal = principal
    context_token = _current_principal.set(principal)
    try:
        yield principal
    finally:
        _current_principal.reset(context_token)


def auth_error_handler(request, exc):
    """Handle AuthError exceptions with reason in the response body."""
    from auth.errors import AuthError

    if isinstance(exc, AuthError):
        return JSONResponse(
            {"detail": exc.detail, "reason": exc.reason.value},
            status_code=exc.status_code,
            headers=exc.headers,
        )
    raise exc


def _log_failure(error: AuthError) -> None:
    logger.warning("authentication failed: %s", error.reason.value)
