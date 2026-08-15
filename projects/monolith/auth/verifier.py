"""Bearer token verification and ordered verifier resolution."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Protocol

import jwt

from auth.errors import AuthError, AuthErrorReason
from auth.jwks import JwksCache, JwksFetcher, TimeFunction, fetch_jwks
from auth.principal import Authority, Principal, PrincipalKind
from auth.settings import AuthSettings

logger = logging.getLogger("monolith.auth")


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> Principal | None:
        """Return a verified principal, or None when the token is not recognized."""


class AuthentikStandingVerifier:
    """Verify standing Authentik authority using its configured JWKS."""

    def __init__(
        self,
        settings: AuthSettings,
        *,
        fetch: JwksFetcher = fetch_jwks,
        now: TimeFunction = time.monotonic,
    ) -> None:
        self._settings = settings
        self._jwks = JwksCache(
            settings.authentik_jwks_url,
            settings.jwks_cache_ttl_s,
            fetch=fetch,
            now=now,
        )

    async def verify(self, token: str) -> Principal | None:
        if not self._settings.identity_is_configured:
            raise AuthError(AuthErrorReason.UNCONFIGURED)

        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise AuthError(AuthErrorReason.MALFORMED) from exc

        if header.get("alg") != "RS256":
            raise AuthError(AuthErrorReason.MALFORMED)
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthError(AuthErrorReason.MALFORMED)

        # Ownership check: is this token ours? Decode without verification to check iss.
        # If the issuer does not match, decline so the next verifier can be tried.
        try:
            unverified_claims = jwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=["RS256"],
            )
        except jwt.InvalidTokenError:
            # A token nobody can parse is nobody's; raise for consistency with header failures.
            raise AuthError(AuthErrorReason.MALFORMED) from None

        iss = unverified_claims.get("iss")
        if iss != self._settings.authentik_issuer:
            logger.debug(
                "declining token: issuer mismatch (expected %s, got %s)",
                self._settings.authentik_issuer,
                iss,
            )
            return None

        jwk = await self._jwks.get_key(kid)
        if jwk is None:
            jwk = await self._jwks.get_key(kid, force_refresh=True)
        if jwk is None:
            raise AuthError(AuthErrorReason.UNKNOWN_KID)

        # The document was fetched successfully to get here, so nothing at this
        # point can be a reachability problem: a key PyJWT cannot construct is a
        # malformed or unsupported entry. PyJWK.from_dict raises PyJWKError or
        # InvalidKeyError, NOT PyJWKClientError, which is a PyJWKClient concern
        # and would never match here.
        try:
            key = jwt.PyJWK.from_dict(jwk, algorithm="RS256").key
        except Exception as exc:
            raise AuthError(AuthErrorReason.JWKS_MALFORMED) from exc

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self._settings.authentik_audience,
                issuer=self._settings.authentik_issuer,
                # PyJWT validates exp only when the claim is PRESENT, so without
                # this a token minted without an expiry would be accepted
                # forever. iss and aud are already implied by passing issuer and
                # audience, and sub is checked below, but state all four so the
                # requirement survives a future edit to either.
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError(AuthErrorReason.EXPIRED) from exc
        except jwt.ImmatureSignatureError as exc:
            raise AuthError(AuthErrorReason.NOT_YET_VALID) from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError(AuthErrorReason.WRONG_AUDIENCE) from exc
        except jwt.InvalidIssuerError as exc:
            # This is unreachable after the ownership gate above, but kept as defence in depth.
            raise AuthError(AuthErrorReason.WRONG_ISSUER) from exc
        except jwt.InvalidSignatureError as exc:
            raise AuthError(AuthErrorReason.BAD_SIGNATURE) from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError(AuthErrorReason.MALFORMED) from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AuthError(AuthErrorReason.MALFORMED)
        groups_claim = claims.get("groups", [])
        if not isinstance(groups_claim, list) or not all(
            isinstance(group, str) for group in groups_claim
        ):
            raise AuthError(AuthErrorReason.MALFORMED)
        email_claim = claims.get("email")
        email = email_claim if isinstance(email_claim, str) and email_claim else None
        scope_claim = claims.get("scope", "")
        if not isinstance(scope_claim, str):
            raise AuthError(AuthErrorReason.MALFORMED)

        # #4943 lands the authentik workload blueprints; pin this discriminator
        # to whatever claim those blueprints guarantee rather than inferring from
        # email absence once it exists.
        kind = PrincipalKind.HUMAN if email is not None else PrincipalKind.WORKLOAD
        return Principal(
            subject=subject,
            actor=(),
            scope=tuple(scope_claim.split()),
            groups=tuple(groups_claim),
            email=email,
            kind=kind,
            authority=Authority.STANDING,
        )


class TokenResolver:
    """Resolve bearer material against verifiers in registration order."""

    def __init__(self, verifiers: Sequence[TokenVerifier]) -> None:
        self._verifiers = tuple(verifiers)

    async def resolve(self, token: str) -> Principal:
        for verifier in self._verifiers:
            principal = await verifier.verify(token)
            if principal is not None:
                return principal
        # A present token never downgrades to anonymous when no verifier owns it.
        raise AuthError(AuthErrorReason.UNRECOGNIZED)


def build_default_resolver(settings: AuthSettings | None = None) -> TokenResolver:
    configured = settings or AuthSettings.from_env()
    # Delegation verification from #4944 registers after the standing verifier.
    verifiers: list[TokenVerifier] = [AuthentikStandingVerifier(configured)]
    return TokenResolver(verifiers)
