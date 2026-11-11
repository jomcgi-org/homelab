"""A browser login audience is accepted only on this module's HTTP routes."""

import os
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from auth.api import AuthentikStandingVerifier, AuthSettings, Principal, get_principal
from auth.browser_cookie import browser_token


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
    """Prefer a signed login token over the shared resolver's principal.

    The Svelte BFF relays the token as a header. The friends lane's browser UI
    carries the same token in its session cookie, which is a credential only
    under the Origin rules in ``auth.browser_cookie``.
    """
    tokens = request.headers.getlist("x-platform-token")
    if len(tokens) > 1:
        raise HTTPException(403, "Ambiguous platform identity.")
    # An empty header is still a presented credential and must fail closed.
    token = tokens[0] if tokens else browser_token(request)
    if token is not None:
        principal = await login_verifier().verify(token)
        if principal is None:
            raise HTTPException(403, "Platform identity required.")
    return principal
