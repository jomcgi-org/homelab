"""Verified identity dependencies for Grimoire's private campaign API."""

import os
from functools import lru_cache

from auth.api import (
    AuthSettings,
    AuthentikStandingVerifier,
    Authority,
    Principal,
    PrincipalKind,
    get_default_resolver,
    get_principal,
)
from auth.dependencies import resolve_authorization
from auth.errors import AuthError, AuthErrorReason
from core.db import get_session
from fastapi import Depends, HTTPException, Request
from sqlmodel import Session

from grimoire.accounts import sync_user


@lru_cache(maxsize=1)
def grimoire_verifier() -> AuthentikStandingVerifier:
    # This audience is accepted only by the Grimoire dependency, never by the
    # shared resolver for operator APIs or MCP.
    return AuthentikStandingVerifier(
        AuthSettings(
            authentik_jwks_url=os.getenv("GRIMOIRE_AUTH_JWKS_URL", ""),
            authentik_issuer=os.getenv("GRIMOIRE_AUTH_ISSUER", ""),
            authentik_audience=os.getenv("GRIMOIRE_AUTH_AUDIENCE", ""),
            jwks_cache_ttl_s=300,
        )
    )


async def get_authenticated_identity(
    request: Request,
    principal: Principal = Depends(get_principal),
) -> Principal:
    """Return a verified human identity whose proxy projection is consistent.

    The friend BFF carries a dedicated signed ID token in X-Grimoire-Token.
    Private gateway requests carry a signed Cloudflare Access assertion and an
    ``X-Auth-Email`` projection. Direct callers carry a standing bearer token.
    The shared principal resolver verifies either credential. A projection is
    never authority by itself, and when present it must match the signed email.
    """
    token = request.headers.get("x-grimoire-token")
    if token:
        if len(request.headers.getlist("x-grimoire-token")) != 1:
            raise AuthError(AuthErrorReason.MALFORMED)
        principal = await grimoire_verifier().verify(token)
        if principal is None:
            raise HTTPException(403, "Grimoire identity required")
    if principal.authority is Authority.ANONYMOUS:
        access_assertions = request.headers.getlist("cf-access-jwt-assertion")
        if len(access_assertions) > 1:
            raise AuthError(AuthErrorReason.MALFORMED)
        if access_assertions:
            resolver = getattr(request.app.state, "auth_resolver", None)
            if resolver is None:
                resolver = get_default_resolver()
            principal = await resolve_authorization(
                f"Bearer {access_assertions[0]}",
                resolver,
            )

    if (
        principal.authority is not Authority.STANDING
        or principal.kind is not PrincipalKind.HUMAN
        or principal.email is None
    ):
        raise HTTPException(
            status_code=403,
            detail="verified human identity required",
        )

    email = principal.email.strip().lower()
    if not email or len(email) > 320:
        raise HTTPException(status_code=403, detail="verified email required")

    projected = request.headers.getlist("x-auth-email")
    if len(projected) > 1:
        raise HTTPException(
            status_code=403,
            detail="ambiguous X-Auth-Email header",
        )
    if projected and projected[0].strip().lower() != email:
        raise HTTPException(status_code=403, detail="identity projection mismatch")

    return principal


async def get_authenticated_email(
    principal: Principal = Depends(get_authenticated_identity),
    session: Session = Depends(get_session),
) -> str:
    """Return the normalized email of a verified human principal."""
    email = principal.email
    if email is None:  # Kept explicit for type narrowing after the dependency.
        raise HTTPException(status_code=403, detail="verified email required")
    if principal.issuer:
        return sync_user(session, principal).email
    return email.strip().lower()


async def get_grimoire_operator_email(
    principal: Principal = Depends(get_authenticated_identity),
) -> str:
    """Require the existing standing operators-group authorization rule."""
    if principal.authority is not Authority.STANDING or not principal.has_group(
        "operators"
    ):
        raise HTTPException(status_code=403, detail="operator role required")
    email = principal.email
    if email is None:
        raise HTTPException(status_code=403, detail="verified email required")
    email = email.strip().lower()
    return email
