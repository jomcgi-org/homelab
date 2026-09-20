"""Synchronize verified identities without moving memberships between people."""

import os

from auth.api import Principal
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from grimoire.models import AppUser


def sync_user(session: Session, principal: Principal) -> AppUser:
    email = (principal.email or "").strip().lower()
    if not principal.issuer or not principal.subject or not email or len(email) > 320:
        raise HTTPException(403, "verified identity required")
    user = session.exec(
        select(AppUser).where(
            AppUser.issuer == principal.issuer,
            AppUser.subject == principal.subject,
        )
    ).first()
    email_user = session.exec(
        select(AppUser)
        .where(AppUser.email == email)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if email_user is not None and (user is None or email_user.id != user.id):
        # Legacy email-only rows were explicitly provisioned by a DM. Only a
        # verified mailbox may claim one; never merge two established identities.
        if (
            user is None
            and email_user.issuer is None
            and (
                principal.email_verified
                or principal.issuer == os.getenv("AUTH_CLOUDFLARE_ACCESS_ISSUER")
            )
        ):
            user = email_user
        else:
            raise HTTPException(
                409, "email belongs to another account; contact the campaign owner"
            )
    if user is None:
        user = AppUser(email=email)
    user.issuer = principal.issuer
    user.subject = principal.subject
    user.email = email
    user.display_name = (principal.display_name or email)[:200]
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # Concurrent first logins may both observe no row. Re-read the winner,
        # but never return a row owned by a different identity.
        existing = session.exec(
            select(AppUser).where(
                AppUser.issuer == principal.issuer,
                AppUser.subject == principal.subject,
                AppUser.email == email,
            )
        ).first()
        if existing is not None:
            session.info["grimoire_user_id"] = existing.id
            return existing
        raise HTTPException(409, "account changed; sign in again") from exc
    session.refresh(user)
    # The DB session belongs to this HTTP request. All authorization lookups
    # use this stable ID, even if another login changes the email concurrently.
    session.info["grimoire_user_id"] = user.id
    return user
