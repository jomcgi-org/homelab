"""Bounded, deduplicated, idempotent admission of due volatile facts to review.

One run reads a capped page of due volatile notes, verifies them against GitHub
outside any database transaction, then records each outcome (and, on success,
the renewal) in its own short transaction. Bounds: a batch cap, a request
budget shared through the verifier's response cache, and a wall-clock deadline.
Concurrency is one worker; the CronWorkflow uses ``Forbid`` and each renewal
locks the row and rechecks the revision captured here, so a concurrent write
cannot be renewed over. A second runner holding later evidence may renew again,
which is harmless because the new lease starts from that newer evidence.

Admission is idempotent because every attempt leaves a ``ReviewOutcome`` whose
retry time gates the next one: ``unsupported`` waits for a new revision,
``failed`` for a day, ``unavailable`` for an exponential backoff, and a renewed
note is no longer due. An unavailable or unsupported source never renews.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import aliased
from sqlmodel import select

from knowledge.freshness import (
    VOLATILE,
    commit_successful_review,
    record_outcome,
    utc,
)
from knowledge.models import Dispute, Note, ReviewOutcome
from knowledge.review_verifier import BudgetExhausted, GitHubVerifier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limits:
    batch: int = 20
    max_requests: int = 60
    deadline_seconds: float = 240.0

    def __post_init__(self) -> None:
        if not 1 <= self.batch <= 100 or not 1 <= self.max_requests <= 500:
            raise ValueError("admission bounds: batch 1..100, max_requests 1..500")
        if self.deadline_seconds <= 0:
            raise ValueError("admission deadline must be positive")


@dataclass(frozen=True)
class Candidate:
    """What was read at admission: the revision the renewal must still match."""

    note_id: str
    revision: int
    content_hash: str
    title: str
    content: str | None


def _due_volatile(clock: datetime):
    return (
        Note.review_policy == VOLATILE,
        Note.deleted_at.is_(None),
        Note.verification_state.not_in(("disputed", "invalidated")),
        or_(Note.valid_until.is_(None), Note.valid_until > clock),
        or_(Note.review_after.is_(None), Note.review_after <= clock),
        Note.note_id.not_in(
            select(Dispute.note_id).where(
                Dispute.state.in_(("open", "resolution_failed"))
            )
        ),
    )


def _blocked(clock: datetime):
    """The note's latest outcome is at this revision and still stops admission."""
    newer = aliased(ReviewOutcome)
    return (
        select(ReviewOutcome.id)
        .where(
            ReviewOutcome.note_id == Note.note_id,
            ~select(newer.id)
            .where(newer.note_id == ReviewOutcome.note_id, newer.id > ReviewOutcome.id)
            .exists(),
            ReviewOutcome.note_revision == Note.revision,
            ReviewOutcome.status != "success",
            or_(
                ReviewOutcome.next_attempt_at.is_(None),
                ReviewOutcome.next_attempt_at > clock,
            ),
        )
        .correlate(Note)
        .exists()
    )


def due_candidates(
    session, *, now: datetime, limit: int
) -> tuple[list[Candidate], int]:
    """Oldest-due unblocked volatile notes, and how many due notes are blocked.

    Blocking is applied in SQL so a pile of permanently unsupported notes can
    never starve the ones that can still be verified.
    """
    clock = utc(now)
    rows = session.exec(
        select(Note)
        .where(*_due_volatile(clock), ~_blocked(clock))
        .order_by(Note.review_after.asc().nulls_first(), Note.id)
        .limit(limit)
    ).all()
    blocked = session.exec(
        select(func.count())
        .select_from(Note)
        .where(*_due_volatile(clock), _blocked(clock))
    ).one()
    return [
        Candidate(row.note_id, row.revision, row.content_hash, row.title, row.content)
        for row in rows
    ], blocked


def run_admission(
    session,
    *,
    verifier: GitHubVerifier,
    clock: Callable[[], datetime],
    limits: Limits | None = None,
    apply: bool = False,
) -> dict:
    """Review a bounded batch; the caller's session is committed per note.

    Dry run only counts what would be admitted and makes no request.
    """
    limits = limits or Limits()
    started = utc(clock())
    candidates, blocked = due_candidates(session, now=started, limit=limits.batch)
    counts: Counter = Counter(candidates=len(candidates), blocked=blocked)
    note_ids = [candidate.note_id for candidate in candidates]
    outcomes = []
    if not apply:
        session.rollback()
        return {"dry_run": True, **counts, "note_ids": note_ids}
    counts.update(
        dict.fromkeys(
            (
                "renewed",
                "aborted",
                "failed",
                "unsupported",
                "unavailable",
                "errors",
                "budget_exhausted",
                "deadline_reached",
            ),
            0,
        )
    )
    # Release the read transaction before any network request.
    session.commit()
    for candidate in candidates:
        if (utc(clock()) - started).total_seconds() >= limits.deadline_seconds:
            counts["deadline_reached"] += 1
            break
        try:
            verdict = verifier.verify(title=candidate.title, content=candidate.content)
        except BudgetExhausted:
            counts["budget_exhausted"] += 1
            break
        now = utc(clock())
        try:
            if verdict.status == "success":
                result = commit_successful_review(
                    session,
                    note_id=candidate.note_id,
                    expected_revision=candidate.revision,
                    expected_content_hash=candidate.content_hash,
                    evidence=verdict.evidence,
                    evidence_observed_at=verdict.observed_at,
                    now=now,
                )
                counts["renewed" if result.renewed else "aborted"] += 1
            else:
                record_outcome(
                    session,
                    note_id=candidate.note_id,
                    revision=candidate.revision,
                    status=verdict.status,
                    reason=verdict.reason,
                    now=now,
                    evidence=verdict.evidence,
                    evidence_observed_at=verdict.observed_at,
                )
                counts[verdict.status] += 1
            session.commit()
            outcomes.append(
                {
                    "note_id": candidate.note_id,
                    "outcome": ("renewed" if result.renewed else "aborted")
                    if verdict.status == "success"
                    else verdict.status,
                }
            )
        except OperationalError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            counts["errors"] += 1
            logger.exception("knowledge review admission failed for a note")
    counts["requests"] = verifier.requests
    return {"dry_run": False, **counts, "note_ids": note_ids, "outcomes": outcomes}
