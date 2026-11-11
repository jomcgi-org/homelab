"""The friends lane's session cookie as an API credential.

The client-only friends UI calls private APIs from the browser. Envoy's OIDC
filter decrypts the session cookie before forwarding, so the API port receives
the signed ID token the Svelte BFF used to relay as a header. Each API still
verifies that token against its own audience.
"""

import os

from fastapi import HTTPException, Request

BROWSER_COOKIE = "grimoire-id-token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def browser_origin() -> str:
    """The one origin whose pages may send the session cookie, or "" when off."""
    return os.getenv("GRIMOIRE_BROWSER_ORIGIN", "").rstrip("/")


def browser_token(request: Request) -> str | None:
    """Return the cookie credential, refusing a cross-site state change.

    A cookie rides along on requests another site makes, so a write must name
    the configured origin exactly. Browsers send Origin on every non-GET fetch
    and form post; a missing or repeated header fails closed. With no origin
    configured the cookie is never a credential, which keeps direct browser
    access off until the gateway route is enabled with it.
    """
    origin = browser_origin()
    token = request.cookies.get(BROWSER_COOKIE)
    if not origin or not token:
        return None
    if request.method not in _SAFE_METHODS:
        origins = request.headers.getlist("origin")
        if len(origins) != 1 or origins[0].rstrip("/") != origin:
            raise HTTPException(403, "cross-origin request refused")
        if request.headers.get("sec-fetch-site", "same-origin") != "same-origin":
            raise HTTPException(403, "cross-origin request refused")
    return token


# The account API paths the friends gateway forwards from a browser. The
# enrollment routes are capability callbacks with no identity dependency, so
# the prefix is never routed whole. `auth/platform/browser_identity_test.py`
# audits these against the router.
PLATFORM_BROWSER_API_PREFIXES = (
    "/api/auth/platform/commands",
    "/api/auth/platform/invitations",
    "/api/auth/platform/management",
    "/api/auth/platform/self",
)
