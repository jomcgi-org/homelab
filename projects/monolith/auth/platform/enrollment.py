"""Verify IdP completion receipts separately from standing login tokens."""

import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache

import jwt
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from auth.api import Authority, Principal, PrincipalKind
from auth.jwks import JwksCache
from auth.platform.models import PlatformIdentity, PlatformInvitation, PlatformUser, now
from auth.platform.service import (
    audit,
    email_address,
    gate,
    identifier,
    identity_user,
    user_view,
    utc,
)

AUDIENCE = "platform-registration"
TOKEN_TYPE = "platform-registration+jwt"


@dataclass(frozen=True)
class Completion:
    issuer: str
    subject: str
    username: str
    invitation_id: str
    invitation_digest: str = field(repr=False)
    request_id: str
    email: str | None = None
    phase: str = "completed"


class ReceiptVerifier:
    def __init__(self, issuer, jwks):
        self.issuer = issuer
        self.jwks = jwks

    async def verify(self, token, *, phase="completed"):
        gate("enrollment")
        if not self.issuer or not isinstance(token, str) or len(token) > 8192:
            raise HTTPException(401, "Invalid enrollment completion proof.")
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (
                header.get("alg") != "RS256"
                or header.get("typ") != TOKEN_TYPE
                or not isinstance(kid, str)
                or not 1 <= len(kid) <= 128
            ):
                raise ValueError()
            key = await self.jwks.get_key(kid)
            if key is None:
                key = await self.jwks.get_key(kid, force_refresh=True)
            if key is None:
                raise ValueError()
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_dict(key).key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=AUDIENCE,
                options={"require": ["iss", "sub", "aud", "exp", "iat", "nbf", "jti"]},
            )
            if (
                claims["aud"] != AUDIENCE
                or claims.get("invitation_bound") is not True
                or claims.get("phase") != phase
            ):
                raise ValueError()
            if any(type(claims[name]) is not int for name in ("iat", "nbf", "exp")):
                raise ValueError()
            if (
                not 0 < claims["exp"] - claims["iat"] <= 120
                or claims["iat"] > time.time()
                or claims["nbf"] < claims["iat"]
            ):
                raise ValueError()
            if any(
                not isinstance(claims.get(name), str)
                or not 1 <= len(claims[name]) <= 500
                for name in ("sub", "username", "jti")
            ):
                raise ValueError()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,31}", claims["username"]):
                raise ValueError()
            digest = claims.get("invitation_digest")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError()
            email = email_address(claims["email"]) if claims.get("email") else None
            return Completion(
                self.issuer,
                claims["sub"],
                claims["username"],
                identifier(claims.get("invitation_id")),
                digest,
                identifier(claims["jti"]),
                email,
                phase,
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, HTTPException):
            raise HTTPException(401, "Invalid enrollment completion proof.") from None


@lru_cache(maxsize=1)
def receipt_verifier():
    return ReceiptVerifier(
        os.getenv("PLATFORM_AUTH_ENROLLMENT_ISSUER", ""),
        JwksCache(os.getenv("PLATFORM_AUTH_ENROLLMENT_JWKS_URL", ""), 300),
    )


def activate(session, completion: Completion):
    """The adapter's signed proof binds one recipient and one stable identity.

    The IdP adapter must validate the capability before account creation and
    sign only for its pending or authenticated user. Ordinary OIDC tokens are rejected by
    the verifier. No application grant or campaign membership is created here.
    """
    gate("enrollment")
    if completion.phase != "completed":
        raise HTTPException(403, "Enrollment completion proof required.")
    row = session.exec(
        select(PlatformInvitation)
        .where(PlatformInvitation.id == completion.invitation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    principal = Principal(
        subject=completion.subject,
        issuer=completion.issuer,
        email=completion.email,
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
        actor=(),
        scope=(),
        groups=(),
        username=completion.username,
        user_type="external",
    )
    user = identity_user(session, principal, lock=True)
    if row is None or row.token_digest != completion.invitation_digest:
        raise HTTPException(403, "Enrollment binding does not match.")
    if row.status == "accepted":
        if (
            user is not None
            and user.active
            and row.accepted_user_id == user.id
            and row.accepted_issuer == completion.issuer
            and row.accepted_subject == completion.subject
        ):
            return {
                **user_view(session, user),
                "activation_pending": not row.identity_activated,
            }
        raise HTTPException(403, "Invitation belongs to another or disabled identity.")
    if row.status != "pending" or utc(row.expires_at) <= now():
        raise HTTPException(410, "Invitation expired or revoked.")
    if user is not None and (not user.active or user.username != completion.username):
        raise HTTPException(403, "Platform identity is disabled or does not match.")
    try:
        if user is None:
            user = PlatformUser(
                username=completion.username,
                email=completion.email,
                display_name=completion.username,
            )
            session.add(user)
            session.flush()
            session.add(
                PlatformIdentity(
                    user_id=user.id,
                    issuer=completion.issuer,
                    subject=completion.subject,
                )
            )
        row.status = "accepted"
        row.accepted_user_id = user.id
        row.accepted_issuer = completion.issuer
        row.accepted_subject = completion.subject
        audit(
            session,
            principal,
            "activate",
            user.id,
            completion.request_id,
            "Verified IdP enrollment completion",
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            409, "Identity already exists or enrollment changed. Retry safely."
        ) from None
    return {
        **user_view(session, user),
        "activation_pending": not row.identity_activated,
    }


def acknowledge_activation(session, completion: Completion):
    """The adapter acknowledges activation once; accepted links cannot reset accounts."""
    gate("enrollment")
    if completion.phase != "activated":
        raise HTTPException(403, "Identity activation proof required.")
    row = session.exec(
        select(PlatformInvitation)
        .where(PlatformInvitation.id == completion.invitation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if (
        row is None
        or row.status != "accepted"
        or row.token_digest != completion.invitation_digest
        or row.accepted_issuer != completion.issuer
        or row.accepted_subject != completion.subject
    ):
        raise HTTPException(403, "Enrollment binding does not match.")
    user = session.get(PlatformUser, row.accepted_user_id)
    if user is None or not user.active:
        raise HTTPException(403, "Platform account is disabled.")
    if not row.identity_activated:
        row.identity_activated = True
        principal = Principal(
            subject=completion.subject,
            issuer=completion.issuer,
            email=completion.email,
            kind=PrincipalKind.HUMAN,
            authority=Authority.STANDING,
            actor=(),
            scope=(),
            groups=(),
            username=completion.username,
        )
        audit(
            session,
            principal,
            "identity_activated",
            user.id,
            completion.request_id,
            "IdP activation acknowledged",
        )
        session.commit()
    return {"activation_pending": False}
