"""TTL-cached JSON Web Key Set retrieval.

Every failure here is an AuthError classified as an infrastructure fault, so a
JWKS problem renders 503 rather than escaping as an unhandled 500 or, worse,
being mistaken for a caller fault. Nothing in this module returns a sentinel
that a caller could read as success: a missing key returns None, which means
exactly "this key id is not in the document I have", and never "the fetch
failed".
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from auth.errors import AuthError, AuthErrorReason

JwksDocument = Mapping[str, Any]
JwksFetcher = Callable[[str], Awaitable[JwksDocument]]
TimeFunction = Callable[[], float]

# A JWKS fetch happens inline on the verification path while the cache lock is
# held, so an unbounded wait stalls every other verification behind it. 5s total
# with 2s to connect keeps a slow IdP from becoming a monolith-wide stall.
_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


async def fetch_jwks(url: str) -> JwksDocument:
    """Fetch a JWKS document using the process HTTP stack."""

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(url)
        response.raise_for_status()
        document = response.json()
    if not isinstance(document, Mapping):
        raise AuthError(AuthErrorReason.JWKS_MALFORMED)
    return document


def _find_key(document: JwksDocument, kid: str) -> dict[str, Any] | None:
    """Return the JWK matching kid, or None when the document does not carry it.

    A structurally invalid document raises rather than reporting "not found":
    a malformed JWKS is our fault, and reporting it as a missing key would
    surface an infrastructure fault to the caller as a 401.
    """

    keys = document.get("keys", [])
    if not isinstance(keys, list):
        raise AuthError(AuthErrorReason.JWKS_MALFORMED)
    for key in keys:
        if isinstance(key, Mapping) and key.get("kid") == kid:
            return dict(key)
    return None


class JwksCache:
    """Cache a JWKS document, refreshing on expiry and on an unknown key id.

    Two independent clocks, which is the part worth reading twice. The TTL
    governs the ORDINARY path: a cached document is served until it expires.
    The forced-refresh FLOOR governs the unknown-kid path, and it exists
    because that path is caller-triggered: without it, N requests bearing
    random key ids each cause an outbound fetch, serialized behind this lock.
    A failed fetch is remembered for the same window, so a down IdP fails fast
    instead of every request paying the full timeout.
    """

    def __init__(
        self,
        url: str,
        ttl_s: float,
        *,
        fetch: JwksFetcher = fetch_jwks,
        now: TimeFunction = time.monotonic,
    ) -> None:
        self._url = url
        self._ttl_s = ttl_s
        self._fetch = fetch
        # Monotonic on purpose: a wall-clock step (NTP correction, suspend)
        # must not expire or extend the cache.
        self._now = now
        self._document: JwksDocument | None = None
        self._fetched_at = 0.0
        # Negative infinity so the FIRST forced refresh is always allowed; the
        # floor only ever suppresses a second one inside the window.
        self._forced_at = float("-inf")
        self._error: AuthError | None = None
        self._lock = asyncio.Lock()

    async def get_key(
        self, kid: str, *, force_refresh: bool = False
    ) -> dict[str, Any] | None:
        document = await self._document_for(force_refresh=force_refresh)
        return _find_key(document, kid)

    async def _document_for(self, *, force_refresh: bool) -> JwksDocument:
        async with self._lock:
            now = self._now()

            if force_refresh:
                if now - self._forced_at < self._ttl_s:
                    # Inside the floor. Re-raise a remembered failure rather
                    # than retrying it, and otherwise serve what we hold so the
                    # caller gets an honest "kid not present" rather than a
                    # second fetch.
                    if self._error is not None:
                        raise self._error
                    return self._document if self._document is not None else {}
                self._forced_at = now
                return await self._fetch_locked(now)

            if now - self._fetched_at < self._ttl_s:
                if self._error is not None:
                    raise self._error
                if self._document is not None:
                    return self._document

            return await self._fetch_locked(now)

    async def _fetch_locked(self, now: float) -> JwksDocument:
        """Fetch and cache. Callers must hold the lock."""

        try:
            document = await self._fetch(self._url)
        except AuthError as exc:
            self._remember_failure(exc, now)
            raise
        except Exception as exc:
            error = AuthError(AuthErrorReason.JWKS_UNREACHABLE)
            self._remember_failure(error, now)
            raise error from exc

        if not isinstance(document, Mapping):
            error = AuthError(AuthErrorReason.JWKS_MALFORMED)
            self._remember_failure(error, now)
            raise error

        self._document = document
        self._fetched_at = now
        self._error = None
        return document

    def _remember_failure(self, error: AuthError, now: float) -> None:
        self._error = error
        self._fetched_at = now
