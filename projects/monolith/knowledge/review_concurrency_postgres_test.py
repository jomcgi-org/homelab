"""Review, dispute and retelling races against the migrated PostgreSQL schema.

Run via the registered bdd_test and shared.testing.plugin's real pg fixture.
Each actor owns a separate connection and transaction. PostgreSQL's lock graph,
not a sleep, proves that the losing operation reached the contested row before
we release the winner. Unique scopes isolate the independently committed data
inside the harness's disposable database.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from queue import Queue
from threading import Event
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select

from knowledge.disputes import create_dispute
from knowledge.extraction import EXTRACTION_VERSION, apply_extraction
from knowledge.freshness import VOLATILE, commit_successful_review
from knowledge.models import (
    AtomRawProvenance,
    Chunk,
    Dispute,
    Note,
    RawInput,
    ReviewOutcome,
)
from knowledge.raw_write import write_raw
from knowledge.review_admission import due_candidates
from knowledge.store import KnowledgeStore

WAIT_SECONDS = 5
HOLDER_WAIT_SECONDS = 15
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
VECTOR = [1.0] + [0.0] * 1023
TITLE = "PR #6821 is open"
BODY = (
    "When the source changes, the review must inspect the replacement evidence "
    "because an earlier observation cannot establish the changed claim."
)


class _Embedder:
    """Replace remote embedding only; duplicate lookup still executes pgvector SQL."""

    async def embed(self, _text):
        return list(VECTOR)

    async def embed_batch(self, texts):
        return [list(VECTOR) for _ in texts]


@pytest.fixture
def lane(pg, monkeypatch):
    identity = uuid4().hex[:12]
    engines = {}

    def application_name(actor):
        return f"knowledge-review-{identity}-{actor}"

    def engine_for(actor):
        if actor not in engines:
            engines[actor] = create_engine(
                pg.url,
                poolclass=NullPool,
                connect_args={
                    "application_name": application_name(actor),
                    "connect_timeout": WAIT_SECONDS,
                    "options": "-c lock_timeout=15000 -c statement_timeout=20000",
                },
            )
        return engines[actor]

    monkeypatch.setattr("knowledge.extraction.EmbeddingClient", _Embedder)
    observer = engine_for("observer")
    note_id = f"review-race-{identity}"
    scope = f"repo:review-race/{identity}"
    with Session(observer) as session:
        note = Note(
            note_id=note_id,
            path=f"notes/{note_id}.md",
            title=TITLE,
            content=BODY,
            content_hash=f"hash-{identity}",
            type="fact",
            scope=scope,
            verification_state="verified",
            # Saturation makes retelling change only revision and provenance.
            confidence=1.0,
            observed_at=NOW - timedelta(days=2),
            review_after=NOW - timedelta(days=1),
            review_policy=VOLATILE,
        )
        session.add_all([note])
        session.flush()
        session.add_all(
            [Chunk(note_fk=note.id, chunk_index=0, chunk_text=BODY, embedding=VECTOR)]
        )
        session.commit()
        snapshot = SimpleNamespace(
            note_id=note_id,
            note_pk=note.id,
            scope=scope,
            revision=note.revision,
            content_hash=note.content_hash,
            observed_at=note.observed_at,
            review_after=note.review_after,
            observer=observer,
            engine=engine_for,
            application_name=application_name,
        )
    try:
        yield snapshot
    finally:
        for engine in engines.values():
            engine.dispose()


def _note(session, lane):
    return session.exec(select(Note).where(Note.note_id == lane.note_id)).one()


def _outcomes(session, lane):
    return session.exec(
        select(ReviewOutcome)
        .where(ReviewOutcome.note_id == lane.note_id)
        .order_by(ReviewOutcome.id)
    ).all()


def _review(session, lane, *, observed=NOW, revision=None):
    return commit_successful_review(
        session,
        note_id=lane.note_id,
        expected_revision=lane.revision if revision is None else revision,
        expected_content_hash=lane.content_hash,
        evidence=["GitHub reports PR #6821 open"],
        evidence_observed_at=observed,
        now=NOW + timedelta(minutes=5),
    )


def _run_review(lane, actor, *, observed=NOW):
    with Session(lane.engine(actor)) as session:
        # Keep a stale identity-map instance alive across the blocked SELECT.
        # populate_existing must refresh it after the winning writer commits.
        cached = _note(session, lane)
        assert cached.revision == lane.revision
        assert cached.last_reviewed_at is None
        result = _review(session, lane, observed=observed)
        session.commit()
        return result


def _dispute(session, lane):
    # Keep production raw-row persistence and the ORM dispute fence. This
    # supported seam omits object storage and extraction-queue registration.
    return create_dispute(
        session,
        lane.note_id,
        "The source changed after the review started",
        ["A newer authoritative observation contradicts the claim"],
        {"reporter_subject": "review-race", "reporter_authority": "standing"},
        raw_writer=write_raw,
    )


def _run_dispute(lane):
    with Session(lane.engine("dispute")) as session:
        result = _dispute(session, lane)
        session.commit()
        return result


def _pid(session):
    return session.execute(text("SELECT pg_backend_pid()")).scalar_one()


def _wait_for_blocked(lane, actor, blocker_pid, future):
    """Require observable row-lock contention, with a bounded diagnostic wait."""
    deadline = monotonic() + WAIT_SECONDS
    poll = Event()
    while monotonic() < deadline:
        if future.done():
            result = future.result()  # Surface the worker's actual error, if any.
            pytest.fail(
                f"{actor} finished without waiting for the row lock: {result!r}"
            )
        with lane.observer.connect() as connection:
            blocked_pid = connection.execute(
                text("""
                    SELECT pid FROM pg_stat_activity
                     WHERE application_name = :actor
                       AND wait_event_type = 'Lock'
                       AND :blocker = ANY(pg_blocking_pids(pid))
                """),
                {"actor": lane.application_name(actor), "blocker": blocker_pid},
            ).scalar_one_or_none()
        if blocked_pid is not None:
            assert blocked_pid != blocker_pid
            return
        # This polls a condition; it does not establish ordering by elapsed time.
        poll.wait(0.01)
    pytest.fail(f"{actor} did not wait on PostgreSQL backend {blocker_pid}")


def _assert_note(session, lane, *, revision, reviewed=None):
    note = _note(session, lane)
    assert note.revision == revision
    assert note.content_hash == lane.content_hash
    assert note.observed_at == lane.observed_at
    assert note.last_reviewed_at == reviewed
    assert note.review_after == (
        reviewed + timedelta(hours=24) if reviewed is not None else lane.review_after
    )
    assert note.review_policy == VOLATILE
    assert note.verification_state == "verified"
    assert note.confidence == 1.0
    assert (note.title, note.content) == (TITLE, BODY)


@pytest.mark.parametrize("commit_dispute", [True, False], ids=["commit", "rollback"])
def test_dispute_fence_serializes_waiting_review(lane, commit_dispute):
    with (
        Session(lane.engine("dispute")) as holder,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        dispute = _dispute(holder, lane)
        assert dispute["status"] == "disputed"
        blocker = _pid(holder)
        review = pool.submit(_run_review, lane, "review")
        try:
            _wait_for_blocked(lane, "review", blocker, review)
            # Neither the revision fence nor the dispute can leak before commit.
            with Session(lane.observer) as observer:
                _assert_note(observer, lane, revision=lane.revision)
                assert (
                    observer.exec(
                        select(Dispute).where(Dispute.note_id == lane.note_id)
                    ).all()
                    == []
                )
                assert _outcomes(observer, lane) == []
            if commit_dispute:
                holder.commit()
            else:
                holder.rollback()
        finally:
            holder.rollback()
        result = review.result(timeout=WAIT_SECONDS)
    assert result.renewed is (not commit_dispute)
    assert result.reason == ("revision_changed" if commit_dispute else "verified")
    with Session(lane.observer) as session:
        _assert_note(
            session,
            lane,
            revision=lane.revision + int(commit_dispute),
            reviewed=None if commit_dispute else NOW,
        )
        disputes = session.exec(
            select(Dispute).where(Dispute.note_id == lane.note_id)
        ).all()
        assert len(disputes) == int(commit_dispute)
        raws = session.exec(
            select(RawInput).where(RawInput.raw_id == dispute["raw_id"])
        ).all()
        assert len(raws) == int(commit_dispute)
        (outcome,) = _outcomes(session, lane)
        assert (outcome.status, outcome.reason, outcome.note_revision) == (
            "failed" if commit_dispute else "success",
            result.reason,
            lane.revision,
        )


def test_review_lock_orders_dispute_after_renewal_and_blocks_future_reviews(lane):
    with (
        Session(lane.engine("review")) as holder,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        assert _review(holder, lane).renewed
        blocker = _pid(holder)
        dispute = pool.submit(_run_dispute, lane)
        try:
            _wait_for_blocked(lane, "dispute", blocker, dispute)
            with Session(lane.observer) as observer:
                _assert_note(observer, lane, revision=lane.revision)
                assert _outcomes(observer, lane) == []
            holder.commit()
        finally:
            holder.rollback()
        assert dispute.result(timeout=WAIT_SECONDS)["status"] == "disputed"
    with Session(lane.observer) as session:
        _assert_note(session, lane, revision=lane.revision + 1, reviewed=NOW)
        assert KnowledgeStore(session).get_note_by_id(lane.note_id)["disputed"] is True
        candidates, _ = due_candidates(session, now=NOW + timedelta(days=2), limit=100)
        assert lane.note_id not in {candidate.note_id for candidate in candidates}
        # Even evidence read at the new revision cannot renew an open dispute.
        refused = _review(
            session,
            lane,
            revision=lane.revision + 1,
            observed=NOW + timedelta(minutes=1),
        )
        assert refused.reason == "disputed_or_invalidated"
        session.commit()
    with Session(lane.observer) as session:
        _assert_note(session, lane, revision=lane.revision + 1, reviewed=NOW)
        assert [
            (row.status, row.reason, row.note_revision)
            for row in _outcomes(session, lane)
        ] == [
            ("success", "verified", lane.revision),
            ("failed", "disputed_or_invalidated", lane.revision + 1),
        ]


@pytest.mark.parametrize("offset_minutes", [-1, 0, 1], ids=["older", "same", "newer"])
def test_competing_renewals_refresh_locked_row_and_never_rewind_evidence(
    lane, offset_minutes
):
    observed = NOW + timedelta(minutes=offset_minutes)
    with (
        Session(lane.engine("first-review")) as holder,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        assert _review(holder, lane).renewed
        blocker = _pid(holder)
        second = pool.submit(_run_review, lane, "second-review", observed=observed)
        try:
            _wait_for_blocked(lane, "second-review", blocker, second)
            holder.commit()
        finally:
            holder.rollback()
        result = second.result(timeout=WAIT_SECONDS)
    newer = offset_minutes > 0
    assert result.renewed is newer
    assert result.reason == ("verified" if newer else "duplicate_or_older_review")
    with Session(lane.observer) as session:
        _assert_note(session, lane, revision=lane.revision, reviewed=max(NOW, observed))
        first, second = _outcomes(session, lane)
        assert (first.status, first.evidence_observed_at) == ("success", NOW)
        assert (second.status, second.reason, second.evidence_observed_at) == (
            "success" if newer else "failed",
            result.reason,
            observed,
        )
        assert first.note_revision == second.note_revision == lane.revision


def _raw(lane):
    raw_id = f"review-extraction-{uuid4().hex}"
    with Session(lane.observer) as session:
        session.add_all(
            [
                RawInput(
                    raw_id=raw_id,
                    path=f"raws/{raw_id}.md",
                    source="agent-report",
                    content_hash=raw_id,
                    extra={"scope": lane.scope},
                )
            ]
        )
        session.commit()
    return raw_id


def _extract(lane, actor, raw_id, *, locked=None, proceed=None):
    payload = json.dumps(
        {
            "assertions": [
                {
                    "title": TITLE,
                    "body": BODY,
                    "scope": lane.scope,
                    "verification_state": "verified",
                    "confidence": 1.0,
                    "observed_at": NOW.isoformat(),
                    "evidence": [],
                    "edges": {},
                }
            ],
            "dispute_resolution": None,
            "doc_drift": [],
            "notes": "",
        }
    )
    with Session(lane.engine(actor)) as session:
        if locked is not None:

            @event.listens_for(session, "before_commit")
            def pause_after_writes(db):
                # apply_extraction commits internally. Flush its real ORM writes
                # before pausing so the holder owns the actual note row lock.
                db.flush()
                locked.put(_pid(db), timeout=WAIT_SECONDS)
                assert proceed.wait(HOLDER_WAIT_SECONDS), (
                    "extraction holder was not released"
                )

        return apply_extraction(session, raw_id, f"```json\n{payload}\n```")


def _assert_retellings(session, lane, raw_ids):
    assert len(session.exec(select(Note).where(Note.scope == lane.scope)).all()) == 1
    provenance = session.exec(
        select(AtomRawProvenance)
        .join(RawInput, RawInput.id == AtomRawProvenance.raw_fk)
        .where(RawInput.raw_id.in_(raw_ids), AtomRawProvenance.atom_fk == lane.note_pk)
    ).all()
    assert len(provenance) == len(raw_ids)
    assert len({row.raw_fk for row in provenance}) == len(raw_ids)
    assert all(row.derived_note_id == lane.note_id for row in provenance)
    assert all(row.gardener_version == EXTRACTION_VERSION for row in provenance)
    raws = session.exec(select(RawInput).where(RawInput.raw_id.in_(raw_ids))).all()
    assert all(raw.extra["extraction_passes"] == 1 for raw in raws)


def _assert_duplicate(result):
    assert result["failed"] is False and result["replayed"] is False
    assert result["atoms"] == []
    assert [row["reason_code"] for row in result["rejected"]] == ["duplicate"]


def test_extraction_retelling_fences_waiting_review_without_changing_hash(lane):
    raw_id = _raw(lane)
    locked, proceed = Queue(maxsize=1), Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        extraction = pool.submit(
            _extract, lane, "extraction", raw_id, locked=locked, proceed=proceed
        )
        try:
            blocker = locked.get(timeout=WAIT_SECONDS)
            review = pool.submit(_run_review, lane, "review")
            _wait_for_blocked(lane, "review", blocker, review)
        finally:
            proceed.set()
        _assert_duplicate(extraction.result(timeout=WAIT_SECONDS))
        assert review.result(timeout=WAIT_SECONDS).reason == "revision_changed"
    with Session(lane.observer) as session:
        _assert_note(session, lane, revision=lane.revision + 1)
        _assert_retellings(session, lane, [raw_id])
        (outcome,) = _outcomes(session, lane)
        assert (outcome.status, outcome.reason, outcome.note_revision) == (
            "failed",
            "revision_changed",
            lane.revision,
        )


def test_review_then_stale_extraction_preserves_successful_lease(lane):
    raw_id = _raw(lane)
    with (
        Session(lane.engine("review")) as holder,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        assert _review(holder, lane).renewed
        blocker = _pid(holder)
        extraction = pool.submit(_extract, lane, "extraction", raw_id)
        try:
            _wait_for_blocked(lane, "extraction", blocker, extraction)
            holder.commit()
        finally:
            holder.rollback()
        _assert_duplicate(extraction.result(timeout=WAIT_SECONDS))
    with Session(lane.observer) as session:
        _assert_note(session, lane, revision=lane.revision + 1, reviewed=NOW)
        _assert_retellings(session, lane, [raw_id])
        (outcome,) = _outcomes(session, lane)
        assert (outcome.status, outcome.note_revision) == ("success", lane.revision)
        refused = _review(session, lane, observed=NOW + timedelta(minutes=1))
        assert refused.reason == "revision_changed"
        session.commit()
    with Session(lane.observer) as session:
        _assert_note(session, lane, revision=lane.revision + 1, reviewed=NOW)


def test_competing_extractions_advance_revision_for_each_retelling(lane):
    raw_ids = [_raw(lane), _raw(lane)]
    locked, proceed = Queue(maxsize=1), Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            _extract,
            lane,
            "first-extraction",
            raw_ids[0],
            locked=locked,
            proceed=proceed,
        )
        try:
            blocker = locked.get(timeout=WAIT_SECONDS)
            second = pool.submit(_extract, lane, "second-extraction", raw_ids[1])
            _wait_for_blocked(lane, "second-extraction", blocker, second)
        finally:
            proceed.set()
        _assert_duplicate(first.result(timeout=WAIT_SECONDS))
        _assert_duplicate(second.result(timeout=WAIT_SECONDS))
    with Session(lane.observer) as session:
        _assert_note(session, lane, revision=lane.revision + 2)
        _assert_retellings(session, lane, raw_ids)
        refused = _review(session, lane, revision=lane.revision + 1)
        assert refused.reason == "revision_changed"
        session.commit()
