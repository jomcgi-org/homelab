"""Disjoint fact totals using only the public reader's views."""

import math
from datetime import timedelta

from core.clock import as_utc
from sqlalchemy import case, func, or_, true, union
from sqlmodel import select

from knowledge.freshness import current_predicate
from knowledge.public_models import PublicNote, PublicNoteLink


def fact_summary(session, *, now) -> dict:
    return fact_summary_snapshot(session, now=now)[0]


def fact_summary_snapshot(session, *, now):
    # The links view filters only the source. Require both ends to be public
    # and active before counting a contradiction, and deduplicate fact IDs.
    active = select(PublicNote.note_id).where(
        PublicNote.verification_state != "invalidated",
        or_(PublicNote.valid_until.is_(None), PublicNote.valid_until > now),
    )
    links = (
        select(PublicNoteLink.source, PublicNoteLink.target)
        .where(
            PublicNoteLink.edge_type == "contradicts",
            PublicNoteLink.source.in_(active),
            PublicNoteLink.target.in_(active),
        )
        .cte("active_public_contradictions")
    )
    contradicted = union(select(links.c.source), select(links.c.target))
    basis = func.coalesce(PublicNote.last_reviewed_at, PublicNote.observed_at)
    bucket = case(
        (
            or_(
                PublicNote.verification_state == "invalidated",
                PublicNote.valid_until <= now,
            ),
            "retired",
        ),
        (
            or_(
                PublicNote.disputed.is_(True),
                PublicNote.verification_state == "disputed",
                PublicNote.note_id.in_(contradicted),
            ),
            "contradicted",
        ),
        (PublicNote.verification_state != "verified", "unverified"),
        (PublicNote.valid_from > now, "not_yet_valid"),
        (current_predicate(now=now, model=PublicNote), "valid"),
        (
            or_(basis.is_(None), basis > now, PublicNote.review_after.is_(None)),
            "review_unknown",
        ),
        else_="review_due",
    ).label("bucket")
    summary = (
        select(bucket, func.count().label("n"))
        .where(PublicNote.type == "fact")
        .group_by(bucket)
        .cte("public_fact_buckets")
    )
    # Same SQL statement/snapshot as the counts, including under READ COMMITTED.
    # A concurrent publication cannot introduce a deadline between these reads.
    fields = (
        PublicNote.review_after,
        PublicNote.valid_from,
        PublicNote.valid_until,
        PublicNote.observed_at,
        PublicNote.last_reviewed_at,
    )
    deadlines = select(
        *(
            func.min(case((field > now, field), else_=None)).label(f"deadline_{index}")
            for index, field in enumerate(fields)
        )
    ).cte("public_fact_deadlines")
    rows = session.execute(
        select(summary.c.bucket, summary.c.n, *deadlines.c).select_from(
            deadlines.outerjoin(summary, true())
        )
    ).all()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    deadline = min(
        [now + timedelta(seconds=60), midnight]
        + [as_utc(value) for value in rows[0][2:] if value is not None]
    )
    counts = {row.bucket: int(row.n) for row in rows if row.bucket is not None}
    pending = {
        key: counts.get(key, 0)
        for key in ("unverified", "review_due", "review_unknown", "not_yet_valid")
    }
    return {
        "total": sum(counts.values()),
        "valid": counts.get("valid", 0),
        "to_confirm": sum(pending.values()),
        "contradicted": counts.get("contradicted", 0),
        "retired": counts.get("retired", 0),
        "to_confirm_breakdown": pending,
    }, deadline


def fact_cache_control(deadline, *, served_at):
    remaining = math.floor((deadline - served_at).total_seconds())
    if remaining <= 0:
        return "no-store"
    return f"public, max-age={remaining}, s-maxage={remaining}, must-revalidate"
