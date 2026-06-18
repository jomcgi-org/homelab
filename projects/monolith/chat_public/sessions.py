"""Server-side session lifecycle for public chat (ADR 005, layer 1+4).

The session row is the single authority for every budget. The browser holds
only an opaque cookie (the session id); it never sends conversation history, and
the server ignores any client-supplied history entirely. This module owns
create, load, TTL expiry, message append, and counter increment. All budget
*decisions* live in ``limits.py``; this module performs the IO and the state
transitions.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from chat_public import limits
from chat_public.models import ChatMessage, ChatSession


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalise a possibly naive timestamp (SQLite round-trips lose tzinfo)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def hash_value(value: str | None) -> str | None:
    """Hash an IP or user-agent for pseudonymous storage. Never store the raw.

    Returns None for an absent value so the column stays NULL rather than a
    hash of the empty string.
    """
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_turnstile(token: str | None, ip: str | None) -> bool:
    """Verify a Cloudflare Turnstile token against siteverify.

    Phase 1 seam: this is a stubbed-accept. The real siteverify call (with the
    Turnstile secret from a OnePasswordItem, run in this FastAPI binary, never
    in SSR) lands in Phase 2. Returning True here lets the session and limit
    machinery be built and tested without the live challenge.

    TODO(Phase 2): call https://challenges.cloudflare.com/turnstile/v0/siteverify
    with the secret + token + remote IP and return its success flag; reject on
    failure so no verified session is minted without a solved challenge.
    """
    return True


def create_session(
    db: Session,
    *,
    turnstile_token: str | None = None,
    ip: str | None = None,
    country: str | None = None,
    user_agent: str | None = None,
) -> ChatSession:
    """Verify the (stubbed) Turnstile token and open a server-side session row.

    The opaque id is generated server-side from a CSPRNG, so the client cannot
    forge or guess one. Pseudonymous details are stored hashed.
    """
    outcome = "passed" if verify_turnstile(turnstile_token, ip) else "failed"
    session = ChatSession(
        id=secrets.token_urlsafe(32),
        ip_hash=hash_value(ip),
        turnstile_outcome=outcome,
        country=country,
        user_agent_hash=hash_value(user_agent),
        status="active",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _is_expired(session: ChatSession, now: datetime) -> bool:
    last_seen = _as_utc(session.last_seen_at) or now
    return now - last_seen > timedelta(seconds=limits.SESSION_TTL_SECONDS)


def load_active_session(db: Session, session_id: str | None) -> ChatSession | None:
    """Load a session by id iff it exists, is active, and is within TTL.

    A session past its TTL is flipped to ``expired`` and returned as None so a
    stale cookie cannot be reused. Returns None for a missing or non-active row,
    never leaking which case it was.
    """
    if not session_id:
        return None
    session = db.exec(
        select(ChatSession).where(ChatSession.id == session_id)
    ).one_or_none()
    if session is None or session.status != "active":
        return None
    if _is_expired(session, _utcnow()):
        session.status = "expired"
        db.add(session)
        db.commit()
        return None
    return session


def touch(db: Session, session: ChatSession) -> None:
    """Bump last_seen_at so an actively-used session keeps its TTL fresh."""
    session.last_seen_at = _utcnow()
    db.add(session)
    db.commit()


def append_message(
    db: Session,
    session: ChatSession,
    *,
    role: str,
    content: str,
    tokens: int = 0,
) -> ChatMessage:
    """Persist a single transcript message under the session."""
    message = ChatMessage(
        session_id=session.id,
        role=role,
        content=content,
        tokens=tokens,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def record_turn(
    db: Session,
    session: ChatSession,
    *,
    tokens: int,
) -> None:
    """Bump the per-session counters after a completed turn.

    One turn increments turn_count by one and adds the turn's token spend to
    total_tokens. last_seen_at is refreshed so the TTL tracks activity.
    """
    session.turn_count += 1
    session.total_tokens += tokens
    session.last_seen_at = _utcnow()
    db.add(session)
    db.commit()
