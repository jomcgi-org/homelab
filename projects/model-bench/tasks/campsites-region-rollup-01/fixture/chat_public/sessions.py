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
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from chat_public import limits
from chat_public.models import ChatMessage, ChatSession

logger = logging.getLogger(__name__)

# Optional salt mixed into the IP/user-agent hash so the stored pseudonym is not
# a bare sha256 of a guessable value (an attacker cannot precompute a rainbow
# table of IP hashes without the salt). No default: empty salt is acceptable for
# dev/test; production injects one via CHAT_PUBLIC_IP_HASH_SALT.
IP_HASH_SALT = os.environ.get("CHAT_PUBLIC_IP_HASH_SALT", "")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalise a possibly naive timestamp (SQLite round-trips lose tzinfo)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def hash_value(value: str | None, salt: str = "") -> str | None:
    """Hash an IP or user-agent for pseudonymous storage. Never store the raw.

    Returns None for an absent value so the column stays NULL rather than a
    hash of the empty string. An optional salt is prepended before hashing so the
    stored pseudonym is not a bare sha256 of a guessable value.
    """
    if not value:
        return None
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()


def create_session(
    db: Session,
    *,
    turnstile_outcome: str = "passed",
    ip: str | None = None,
    country: str | None = None,
    user_agent: str | None = None,
) -> ChatSession:
    """Open a server-side session row for an already-admitted request.

    Turnstile siteverify runs in the router (it is async network IO, see
    chat_public.turnstile.siteverify); this function is only reached once the
    challenge passed, and records the verified ``turnstile_outcome``.

    The opaque id is generated server-side from a CSPRNG, so the client cannot
    forge or guess one. The IP is stored only as a salted hash, never raw.
    """
    # ip_hash is retained for reactive abuse forensics and targeted blocking,
    # not a pre-emptive per-IP cap (dropped: see limits.py rationale).
    ip_hash = hash_value(ip, IP_HASH_SALT)
    session = ChatSession(
        id=secrets.token_urlsafe(32),
        ip_hash=ip_hash,
        turnstile_outcome=turnstile_outcome,
        country=country,
        user_agent_hash=hash_value(user_agent, IP_HASH_SALT),
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
    touched: list | None = None,
) -> ChatMessage:
    """Persist a single transcript message under the session.

    ``touched`` is the assistant turn's grounding (a [{id, title}, ...] list of
    the public notes it touched); it defaults to empty for user turns. Stored so
    a shared snapshot can render the same grounding chips as the live app.
    """
    message = ChatMessage(
        session_id=session.id,
        role=role,
        content=content,
        tokens=tokens,
        touched=touched or [],
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def append_messages(
    db: Session,
    session: ChatSession,
    entries: list[dict],
) -> list[ChatMessage]:
    """Persist several transcript messages under the session in one write.

    Each entry is a ``{role, content, tokens?, touched?}`` dict. Built and
    inserted with a single ``add_all`` (never ``session.add`` in a loop, per the
    monolith semgrep rule); the autoincrement id preserves insertion order, which
    is the transcript order ``get_transcript`` reads back. Used to seed a forked
    session from an immutable snapshot's frozen transcript.
    """
    messages = [
        ChatMessage(
            session_id=session.id,
            role=entry["role"],
            content=entry["content"],
            tokens=entry.get("tokens", 0),
            touched=entry.get("touched") or [],
        )
        for entry in entries
    ]
    db.add_all(messages)
    db.commit()
    return messages


def set_counters(
    db: Session,
    session: ChatSession,
    *,
    turn_count: int,
    total_tokens: int,
) -> None:
    """Set the per-session budget counters directly (not an increment).

    Used to seed a forked session so the carried-over history counts against the
    per-session turn/token ceilings from the first new turn (a fork inherits the
    conversation's spend; it cannot reset the budget by re-forking).
    """
    session.turn_count = turn_count
    session.total_tokens = total_tokens
    session.last_seen_at = _utcnow()
    db.add(session)
    db.commit()


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


def get_transcript(db: Session, session: ChatSession) -> list[ChatMessage]:
    """All stored transcript messages for a session, oldest first.

    The server-authoritative history (the browser never sends history). Used to
    build the model context and to decide compaction.
    """
    return list(
        db.exec(
            select(ChatMessage)
            .where(ChatMessage.session_id == session.id)
            .order_by(ChatMessage.id)
        ).all()
    )


async def compact_if_needed(
    db: Session,
    session: ChatSession,
    transcript: list[ChatMessage],
    *,
    summarize: Callable[[str | None, list[ChatMessage]], Awaitable[str]],
) -> tuple[str | None, list[ChatMessage]]:
    """Fold older turns into the rolling summary when the context is large.

    Returns ``(summary, tail)``: the summary text to prepend (or None) and the
    transcript messages to send verbatim. When the estimated live context (the
    current summary plus the full transcript) crosses the compaction trigger,
    everything older than the recent tail is summarised via ``summarize`` (which
    calls vLLM under the in-flight slot the caller already holds), the new summary
    is persisted to ``session.rolling_summary``, and only the recent tail is
    returned. Below the trigger the existing summary and full transcript pass
    through unchanged.

    ``transcript`` is the history BEFORE the current user message; the new
    message is appended to the model context by the caller, not here.

    The decision deliberately re-folds the older messages each time it triggers
    (no high-water column): a public session is hard-capped at limits.MAX_TURNS
    turns, so the older set is small and bounded, and re-folding keeps the schema
    unchanged. Frequency is naturally capped because after a compaction the live
    context is summary + tail, which stays below the trigger.
    """
    summary = session.rolling_summary
    keep = limits.COMPACTION_KEEP_MESSAGES

    estimated = limits.estimate_tokens(summary or "") + sum(
        limits.estimate_tokens(m.content) for m in transcript
    )
    if not limits.should_compact(estimated) or len(transcript) <= keep:
        return summary, transcript

    older = transcript[:-keep] if keep else list(transcript)
    recent = transcript[-keep:] if keep else []

    new_summary = await summarize(summary, older)
    session.rolling_summary = new_summary
    session.last_seen_at = _utcnow()
    db.add(session)
    db.commit()
    logger.info(
        "chat_public.compaction folded=%d kept=%d est_tokens=%d",
        len(older),
        len(recent),
        estimated,
    )
    return new_summary, recent
