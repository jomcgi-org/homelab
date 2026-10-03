"""Bounded, replayable backfill of review policy from original evidence dates."""

from collections import Counter
from datetime import datetime

from sqlalchemy.exc import OperationalError
from sqlmodel import select

from knowledge.freshness import metadata, preserve_deadline
from knowledge.models import Note


def backfill(
    session,
    *,
    now: datetime,
    apply: bool = False,
    after: str = "",
    batch_size: int = 500,
    max_batches: int = 20,
    pending_only: bool = False,
) -> dict:
    """Return counts and the last stable note id; resume using next_after.

    Scheduled application selects only unclassified rows and needs no saved
    cursor. Each invocation is bounded and commits atomically; errors propagate
    without a success receipt. No creation/index time is used as evidence.
    """
    if not 1 <= batch_size <= 500 or not 1 <= max_batches <= 20:
        raise ValueError("backfill bounds: batch_size 1..500, max_batches 1..20")
    policies = Counter()
    freshness = Counter()
    count = 0
    cursor = after
    exhausted = False
    try:
        for _ in range(max_batches):
            query = (
                select(Note)
                .where(Note.note_id > cursor, Note.deleted_at.is_(None))
                .order_by(Note.note_id)
                .limit(batch_size)
            )
            if pending_only:
                query = query.where(Note.review_policy.is_(None))
            if apply:
                query = query.with_for_update()
            rows = session.exec(query).all()
            if not rows:
                exhausted = True
                break
            for note in rows:
                # A detached projection keeps dry-run genuinely non-mutating.
                projected = Note(
                    title=note.title,
                    content=note.content,
                    observed_at=note.observed_at,
                    last_reviewed_at=note.last_reviewed_at,
                    review_after=note.review_after,
                )
                preserve_deadline(projected, now=now, existing=note)
                policies[projected.review_policy] += 1
                freshness[metadata(projected, now=now)["freshness"]] += 1
                if apply:
                    note.review_policy = projected.review_policy
                    note.review_after = projected.review_after
                count += 1
            cursor = str(rows[-1].note_id)
            if len(rows) < batch_size:
                exhausted = True
                break
        if apply:
            session.commit()
    except OperationalError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    return {
        "dry_run": not apply,
        "count": count,
        "policies": dict(policies),
        "freshness": dict(freshness),
        "next_after": None if exhausted else cursor,
    }
