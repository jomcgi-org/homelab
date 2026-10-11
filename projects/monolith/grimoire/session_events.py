"""Single transaction-owned write path for play, dice, and utterance events.

Callers authorize membership, author provenance, and permitted kinds before
calling. This helper flushes but never commits, so rollback also rolls back seq.
The play flag is an HTTP boundary dependency, not a storage write restriction.
"""

import os
from typing import get_args
from uuid import UUID, NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from grimoire.audience import Audience
from grimoire.models import EventKind, GameSession, PlayerCharacter, SessionEvent


class SessionEndedError(ValueError):
    """The authoritative session row is ended and cannot accept events."""


class InvalidEventKindError(ValueError):
    """The event kind is outside the storage contract."""


class InvalidEventAudienceError(ValueError):
    """The audience is outside this campaign or has conflicting provenance."""


class EventRequestConflictError(ValueError):
    """A retry token was reused for a different event."""


def play_enabled() -> bool:
    """Only the exact lowercase string 'true' enables play, read at call time."""
    return os.environ.get("GRIMOIRE_PLAY_ENABLED") == "true"


def require_play_enabled() -> None:
    """FastAPI dependency for every play route, hiding the surface when off."""
    if not play_enabled():
        raise HTTPException(status_code=404, detail="Not found")


def transcript_enabled() -> bool:
    """Read the text-only transcript switch at call time."""
    return os.environ.get("GRIMOIRE_TRANSCRIPT_ENABLED") == "true"


def require_transcript_enabled() -> None:
    """Hide transcript routes unless both transcript and play are enabled."""
    if not play_enabled() or not transcript_enabled():
        raise HTTPException(status_code=404, detail="Not found")


def append_event(
    session: Session,
    *,
    game_session: GameSession,
    kind: EventKind,
    audience: Audience,
    author_member_id: str | None,
    body: dict,
    request_id: UUID | None = None,
) -> SessionEvent:
    """Lock the persisted session, validate, allocate its next seq, and flush.

    The row lock is held until the caller commits or rolls back. Refreshing the
    locked row avoids accepting an ended session from a stale identity-map row.
    An identical request ID returns its existing event, including after end or
    retraction; callers still authorize membership and project the result.
    No database retries: IntegrityError exposes a broken serialization invariant.
    """
    locked = session.exec(
        select(GameSession)
        .where(GameSession.id == game_session.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if locked is None:
        raise ValueError("Game session does not exist")
    event_id = (
        str(
            uuid5(
                NAMESPACE_URL, f"grimoire:{locked.id}:{author_member_id}:{request_id}"
            )
        )
        if request_id is not None
        else None
    )
    if event_id is not None:
        existing = session.get(SessionEvent, event_id)
        if existing is not None:
            if (
                existing.kind != kind
                or existing.body != body
                or existing.audience != audience.kind
                or {UUID(pc) for pc in existing.audience_pc_ids}
                != {UUID(pc) for pc in audience.pc_ids}
            ):
                raise EventRequestConflictError(
                    "Retry token already used for a different message"
                )
            return existing
    if locked.status == "ended":
        raise SessionEndedError("Cannot append to an ended session")
    if kind not in get_args(EventKind):
        raise InvalidEventKindError("Unknown event kind")
    if (
        audience.author_member_id is not None
        and audience.author_member_id != author_member_id
    ):
        raise InvalidEventAudienceError("Conflicting author provenance")
    if audience.kind == "pcs":
        campaign_pc_ids = set(
            session.exec(
                select(PlayerCharacter.id).where(
                    PlayerCharacter.campaign_id == locked.campaign_id,
                    PlayerCharacter.id.in_(audience.pc_ids),
                )
            ).all()
        )
        if {UUID(pc) for pc in campaign_pc_ids} != {UUID(pc) for pc in audience.pc_ids}:
            raise InvalidEventAudienceError("Audience PCs must belong to this campaign")
    next_seq = session.exec(
        select(func.coalesce(func.max(SessionEvent.seq), 0) + 1).where(
            SessionEvent.session_id == locked.id
        )
    ).one()
    columns = audience.to_columns()
    if audience.kind == "pcs":
        columns["audience_pc_ids"] = sorted(campaign_pc_ids)
    columns["author_member_id"] = author_member_id
    row = SessionEvent(
        **({"id": event_id} if event_id else {}),
        campaign_id=locked.campaign_id,
        session_id=locked.id,
        seq=next_seq,
        kind=kind,
        body=body,
        **columns,
    )
    session.add(row)
    session.flush()
    return row
