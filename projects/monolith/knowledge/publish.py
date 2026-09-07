"""Publish verified and unverified agent-derived facts to the public tier."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, func, or_, update
from sqlmodel import Session, select

from knowledge.models import Dispute, Note, NoteId
from knowledge.redact import redact_text

logger = logging.getLogger(__name__)

PUBLISHABLE_VERIFICATION_STATES = {"verified", "unverified"}
"""Verification states eligible for automatic publication."""

PUBLISHABLE_SCOPES = {
    "repo:jomcgi-org/homelab",
    "org:jomcgi-org",
    "environment:homelab",
}
"""Knowledge scopes eligible for automatic publication."""

PUBLISHABLE_TYPE = "fact"
"""The only note type eligible for automatic publication."""

_UPDATE_CHUNK_SIZE = 500
_SKIPPED_REDACTION_INFO_KEY = "knowledge.publish.skipped_redaction"


@dataclass(frozen=True)
class PublishReport:
    """Counts produced by one publication-lane run."""

    published: int
    unpublished: int
    skipped_redaction: int
    skipped_dispute: int


def _has_open_dispute() -> Any:
    """Return the correlated predicate for this schema's open-dispute marker."""
    return exists(
        select(Dispute.id).where(
            Dispute.note_id == Note.note_id,
            Dispute.state == "open",
        )
    )


def _publishable_base_predicates() -> tuple[Any, ...]:
    return (
        Note.deleted_at.is_(None),
        Note.verification_state.in_(PUBLISHABLE_VERIFICATION_STATES),
        Note.scope.in_(PUBLISHABLE_SCOPES),
        Note.type == PUBLISHABLE_TYPE,
        Note.visibility == "private",
    )


def select_publishable(session: Session) -> list[NoteId]:
    """Return private facts that satisfy policy and contain no secrets."""
    rows = session.execute(
        select(Note.note_id, Note.title, Note.content).where(
            *_publishable_base_predicates(),
            ~_has_open_dispute(),
        )
    ).all()

    publishable: list[NoteId] = []
    skipped_redaction = 0
    for row in rows:
        title_hit = redact_text(row.title)[1] > 0
        content_hit = redact_text(row.content or "")[1] > 0
        if title_hit or content_hit:
            skipped_redaction += 1
            fields = ",".join(
                field
                for field, hit in (("title", title_hit), ("content", content_hit))
                if hit
            )
            logger.warning(
                "knowledge.publish.skipped note_id=%s reason=redaction_hit fields=%s",
                row.note_id,
                fields,
            )
            continue
        publishable.append(NoteId(row.note_id))

    session.info[_SKIPPED_REDACTION_INFO_KEY] = skipped_redaction
    return publishable


def select_unpublishable(session: Session) -> list[NoteId]:
    """Return public non-legacy notes whose publication policy stopped holding."""
    rows = session.exec(
        select(Note.note_id).where(
            Note.visibility == "public",
            Note.verification_state != "legacy",
            or_(
                Note.deleted_at.is_not(None),
                Note.verification_state.not_in(PUBLISHABLE_VERIFICATION_STATES),
                Note.scope.is_(None),
                Note.scope.not_in(PUBLISHABLE_SCOPES),
                _has_open_dispute(),
            ),
        )
    ).all()
    return [NoteId(note_id) for note_id in rows]


def _count_skipped_disputes(session: Session) -> int:
    return int(
        session.exec(
            select(func.count())
            .select_from(Note)
            .where(*_publishable_base_predicates(), _has_open_dispute())
        ).one()
    )


def _chunks(note_ids: list[NoteId]) -> list[list[NoteId]]:
    return [
        note_ids[offset : offset + _UPDATE_CHUNK_SIZE]
        for offset in range(0, len(note_ids), _UPDATE_CHUNK_SIZE)
    ]


def apply(session: Session, dry_run: bool) -> PublishReport:
    """Apply the publication policy, committing each update batch separately."""
    publishable = select_publishable(session)
    skipped_redaction = int(session.info.pop(_SKIPPED_REDACTION_INFO_KEY, 0))
    unpublishable = select_unpublishable(session)
    skipped_dispute = _count_skipped_disputes(session)

    report = PublishReport(
        published=len(publishable),
        unpublished=len(unpublishable),
        skipped_redaction=skipped_redaction,
        skipped_dispute=skipped_dispute,
    )
    if dry_run:
        return report

    for chunk in _chunks(publishable):
        session.execute(
            update(Note)
            .where(Note.note_id.in_(chunk))
            .values(
                visibility="public",
                visibility_verified=True,
                published_at=func.now(),
            )
        )
        session.commit()

    for chunk in _chunks(unpublishable):
        session.execute(
            update(Note)
            .where(Note.note_id.in_(chunk))
            .values(visibility="private", published_at=None)
        )
        session.commit()

    return report
