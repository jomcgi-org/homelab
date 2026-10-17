"""Disjoint fact totals using only the public reader's views."""

from sqlalchemy import case, func, or_, union
from sqlmodel import select

from knowledge.freshness import current_predicate
from knowledge.public_models import PublicNote, PublicNoteLink


def fact_summary(session, *, now) -> dict:
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
    rows = session.execute(
        select(bucket, func.count().label("n"))
        .where(PublicNote.type == "fact")
        .group_by(bucket)
    ).all()
    counts = {row.bucket: int(row.n) for row in rows}
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
    }
