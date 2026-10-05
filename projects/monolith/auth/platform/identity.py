"""A browser login audience is accepted only on this module's HTTP routes."""

import os
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from auth.api import AuthentikStandingVerifier, AuthSettings, Principal, get_principal


@lru_cache(maxsize=1)
def login_verifier():
    return AuthentikStandingVerifier(
        AuthSettings(
            authentik_issuer=os.getenv("PLATFORM_AUTH_LOGIN_ISSUER", ""),
            authentik_jwks_url=os.getenv("PLATFORM_AUTH_LOGIN_JWKS_URL", ""),
            authentik_audience=os.getenv("PLATFORM_AUTH_LOGIN_AUDIENCE", ""),
            jwks_cache_ttl_s=300,
            allow_username_identity=True,
        )
    )


async def browser_or_operator(
    request: Request, principal: Annotated[Principal, Depends(get_principal)]
):
    tokens = request.headers.getlist("x-platform-token")
    if tokens:
        if len(tokens) != 1:
            raise HTTPException(403, "Ambiguous platform identity.")
        principal = await login_verifier().verify(tokens[0])
        if principal is None:
            raise HTTPException(403, "Platform identity required.")
    return principal
