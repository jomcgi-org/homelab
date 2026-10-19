"""Durable coverage reconsideration, without allocating fact-review receipts."""

from datetime import timedelta

from sqlalchemy import func, text
from sqlmodel import select

from knowledge import review_coverage as coverage
from knowledge.models import FactReviewSourceBlock, Note
from knowledge.review_batch_sources import BatchSources
from knowledge.review_sources import EvidenceUnavailable

PROBE_INTERVAL = timedelta(hours=6)
PROBE_WINDOW = timedelta(minutes=15)
MAX_PROBES = 2


def record_block(session, run, outcome, identity, now):
    """Commit atomically with the first durable unsupported result."""
    if not coverage.enabled() or not identity:
        return
    note = session.exec(
        select(Note)
        .where(Note.note_id == run.note_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if (
        note is None
        or note.revision != run.revision
        or note.content_hash != run.content_hash
    ):
        return
    session.flush()
    block = session.get(FactReviewSourceBlock, run.note_id)
    if block is None:
        block = FactReviewSourceBlock(
            note_id=run.note_id,
            revision=run.revision,
            content_hash=run.content_hash,
            outcome_id=outcome.id,
            identity=identity,
            next_probe_at=now + PROBE_INTERVAL,
        )
        session.add(block)
    else:
        if block.outcome_id >= outcome.id:
            return
        block.revision, block.content_hash = run.revision, run.content_hash
        block.outcome_id, block.identity = outcome.id, identity
        block.released = False
        block.next_probe_at = now + PROBE_INTERVAL


def probe(session, now, *, fetch=None):
    """Reserve at most two probes per 15 minutes, surviving process restarts.

    The common admission lock reserves timestamps before network I/O. Failed
    probes keep the block and the six-hour cooldown. No outcome, claim, paid
    receipt or invocation fence is changed here.
    """
    if not coverage.enabled():
        return
    from knowledge import fact_review as review

    if session.get_bind().dialect.name != "sqlite":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": review.JOB_NAME},
        )
    used = session.exec(
        select(func.count())
        .select_from(FactReviewSourceBlock)
        .where(FactReviewSourceBlock.last_probe_at >= now - PROBE_WINDOW)
    ).one()
    remaining = max(0, MAX_PROBES - used)
    blocks = session.exec(
        select(FactReviewSourceBlock)
        .join(Note, Note.note_id == FactReviewSourceBlock.note_id)
        .where(
            *review._review_pool(now),
            FactReviewSourceBlock.revision == Note.revision,
            FactReviewSourceBlock.content_hash == Note.content_hash,
            FactReviewSourceBlock.released.is_(False),
            FactReviewSourceBlock.next_probe_at <= now,
        )
        .order_by(FactReviewSourceBlock.next_probe_at, FactReviewSourceBlock.note_id)
        .limit(remaining)
    ).all()
    pending = []
    for block in blocks:
        pending.append((block.note_id, block.outcome_id, dict(block.identity)))
        block.last_probe_at = now
        block.next_probe_at = now + PROBE_INTERVAL
    session.commit()
    if not pending:
        return
    # At most one pin plus four non-recursive trees per member. Cache hits
    # share directories across members; these reads never fetch file bodies.
    with BatchSources(fetch=fetch) as reader:
        try:
            main = reader.pin()
        except EvidenceUnavailable:
            return
        for note_id, outcome_id, old in pending:
            try:
                fresh = coverage.file_identity(
                    sorted(old["files"]),
                    main,
                    lambda suffix: reader.read(f"/repos/{reader.repo}" + suffix),
                )
            except EvidenceUnavailable:
                continue
            block = session.exec(
                select(FactReviewSourceBlock)
                .where(FactReviewSourceBlock.note_id == note_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            if (
                block.outcome_id == outcome_id
                and block.identity == old
                # A fresh probe's smaller request set can resolve a placeholder
                # without any source change. Only compare resolved identities.
                and all(item is not None for item in old["files"].values())
                and fresh != old
            ):
                block.released = True
            session.commit()
