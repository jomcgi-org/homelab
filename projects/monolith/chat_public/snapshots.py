"""Opt-in, read-only chat-transcript snapshots (ADR 005 follow-up, "share").

A snapshot is minted SERVER-SIDE from the stored, server-authoritative
transcript (``sessions.get_transcript``), never from client-supplied message
content: a forged request body must not be able to put words in the model's
mouth in a publicly-shareable artifact (integrity). Snapshots are immutable once
created and read-only thereafter.

DB IO is synchronous SQLModel, matching ``sessions.py``. Writes use the
``public_writer`` engine; reads use whichever engine the caller passes (the read
route hands in the default ``public_reader`` replica session).
"""

from __future__ import annotations

import logging
import secrets

from sqlmodel import Session, select

from chat_public.models import ChatSession, ChatSnapshot

logger = logging.getLogger(__name__)

# Only user/assistant turns are shared; stored system rows (rolling-summary
# scaffolding, if any) are never exposed in a public artifact.
_SHARABLE_ROLES = ("user", "assistant")


def create_snapshot(db: Session, session: ChatSession) -> ChatSnapshot:
    """Freeze a session's transcript into an immutable, shareable snapshot.

    Reads the server-authoritative transcript, keeps only user/assistant rows,
    serializes them to a ``[{role, content, touched}, ...]`` array (touched is
    the assistant turn's grounding, empty for user turns), mints an opaque
    CSPRNG id, inserts, and returns the row. The browser supplies nothing here:
    the content comes only from what the server already stored.
    """
    from chat_public import sessions  # local import: avoid an import cycle

    transcript = [
        {"role": m.role, "content": m.content, "touched": list(m.touched or [])}
        for m in sessions.get_transcript(db, session)
        if m.role in _SHARABLE_ROLES
    ]
    snapshot = ChatSnapshot(
        id=secrets.token_urlsafe(32),
        transcript=transcript,
        message_count=len(transcript),
        source_session_id=session.id,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def has_sharable_transcript(db: Session, session: ChatSession) -> bool:
    """True iff the session has at least one user/assistant turn to share.

    Used to reject an empty share before a snapshot row is minted, so a "share"
    with nothing to share never creates an orphan artifact."""
    from chat_public import sessions  # local import: avoid an import cycle

    return any(m.role in _SHARABLE_ROLES for m in sessions.get_transcript(db, session))


def load_snapshot(read_db: Session, snapshot_id: str | None) -> ChatSnapshot | None:
    """Load a snapshot by id, or None if missing. Read-only."""
    if not snapshot_id:
        return None
    return read_db.exec(
        select(ChatSnapshot).where(ChatSnapshot.id == snapshot_id)
    ).one_or_none()
