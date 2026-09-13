"""Verified identity dependencies for Grimoire's private campaign API."""

from auth.api import (
    Authority,
    Principal,
    PrincipalKind,
    get_default_resolver,
    get_principal,
)
from auth.dependencies import resolve_authorization
from auth.errors import AuthError, AuthErrorReason
from fastapi import Depends, HTTPException, Request


async def get_authenticated_identity(
    request: Request,
    principal: Principal = Depends(get_principal),
) -> Principal:
    """Return a verified human identity whose proxy projection is consistent.

    Gateway browser requests carry a signed Cloudflare Access assertion and an
    ``X-Auth-Email`` projection. Direct callers carry a standing bearer token.
    The shared principal resolver verifies either credential. A projection is
    never authority by itself, and when present it must match the signed email.
    """
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
) -> str:
    """Return the normalized email of a verified human principal."""
    email = principal.email
    if email is None:  # Kept explicit for type narrowing after the dependency.
        raise HTTPException(status_code=403, detail="verified email required")
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
