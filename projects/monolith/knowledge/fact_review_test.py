"""Evidence, replay, scope, bounds and concurrent-change contracts for prose review."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge import fact_review as review
from knowledge.extraction import ExtractionOutputInvalid
from knowledge.freshness import STANDARD, record_outcome, utc
from knowledge.models import Dispute, FactReviewRun, Note, ReviewOutcome
from knowledge.review_sources import EvidenceUnavailable

NOW = datetime(2026, 10, 17, 6, tzinfo=UTC)
SOURCE = {
    "id": "s1",
    "subject": "file:projects/example/app.py",
    "url": "https://github.com/jomcgi-org/homelab/blob/"
    + "a" * 40
    + "/projects/example/app.py",
    "text": "if not inputs: raise ValueError('empty inputs')",
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_ENABLED", "true")
    engine = create_engine(f"sqlite:///{tmp_path / 'facts.db'}").execution_options(
        schema_translate_map={
            table.schema: None for table in SQLModel.metadata.tables.values()
        }
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def note(db, name="fact", **fields):
    defaults = {
        "note_id": name,
        "path": name + ".md",
        "title": "Empty inputs are refused before dispatch",
        "content": "The service refuses empty inputs in projects/example/app.py.",
        "content_hash": "original-hash",
        "type": "fact",
        "scope": "repo:jomcgi-org/homelab",
        "verification_state": "verified",
        "observed_at": NOW - timedelta(days=100),
        "review_after": NOW - timedelta(days=10),
        "review_policy": STANDARD,
    }
    defaults.update(fields)
    row = Note(**defaults)
    db.add(row)
    db.commit()
    return row


def prepare(db, key="invocation", sources=None):
    return review.prepare(
        db, key, collector=lambda claim: (sources or [SOURCE], NOW), now=NOW
    )


def output(db, key="invocation", **changes):
    run = db.get(FactReviewRun, key)
    body = {
        "claim_hash": review._digest(run.claim),
        "verdict": "holds",
        "rationale": "The exact source rejects empty inputs before any dispatch occurs.",
        "citations": [{"source_id": "s1", "quote": "raise ValueError('empty inputs')"}],
    }
    body.update(changes)
    return "```json\n" + json.dumps(body) + "\n```"


def test_renewal_preserves_original_observation_and_labels(db):
    row = note(db)
    before = (row.revision, row.verification_state, row.confidence)
    prepare(db)
    result = review.apply(db, "invocation", output(db), now=NOW + timedelta(minutes=1))
    db.refresh(row)
    assert result["outcome"] == "renewed"
    assert utc(row.observed_at) == NOW - timedelta(days=100)
    assert utc(row.last_reviewed_at) == NOW
    assert utc(row.review_after) == NOW + timedelta(days=90)
    assert (row.revision, row.verification_state, row.confidence) == before
    outcome = db.exec(select(ReviewOutcome)).one()
    assert outcome.status == "success" and SOURCE["url"] in outcome.evidence[1]


def test_replay_uses_pinned_sources_and_commits_only_once(db):
    note(db)
    first = prepare(db)
    assert (
        review.prepare(
            db, "invocation", collector=lambda _: pytest.fail("refetched"), now=NOW
        )
        == first
    )
    result = review.apply(db, "invocation", output(db), now=NOW)
    assert (
        review.apply(db, "invocation", "invalid replay", now=NOW)["outcome"]
        == result["outcome"]
    )
    assert len(db.exec(select(ReviewOutcome)).all()) == 1


def test_interrupted_source_collection_resumes_same_claim(db):
    row = note(db)
    with pytest.raises(RuntimeError):
        review.prepare(
            db,
            "invocation",
            collector=lambda _: (_ for _ in ()).throw(RuntimeError("worker lost")),
            now=NOW,
        )
    pinned = db.get(FactReviewRun, "invocation").claim
    row.title = "Concurrent replacement"
    db.commit()
    prompt = prepare(db)
    assert pinned["title"] in prompt and "Concurrent replacement" not in prompt
    assert review.apply(db, "invocation", output(db), now=NOW)["outcome"] == "failed"


def test_source_collection_has_no_transaction_and_cannot_replace_a_winner(db):
    note(db)

    def concurrent_collection(_):
        assert not db.in_transaction()
        with Session(db.bind) as other:
            run = other.get(FactReviewRun, "invocation")
            run.sources = [SOURCE]
            run.observed_at = NOW
            other.commit()
        return [
            {**SOURCE, "text": "later result must not replace pinned evidence"}
        ], NOW

    prompt = review.prepare(db, "invocation", collector=concurrent_collection, now=NOW)
    assert "later result must not replace" not in prompt
    assert db.get(FactReviewRun, "invocation").sources == [SOURCE]


@pytest.mark.parametrize(
    "change",
    [
        {"citations": []},
        {"claim_hash": "0" * 64},
        {"citations": [{"source_id": "s1", "quote": "fabricated evidence quotation"}]},
        {
            "citations": [
                {"source_id": "s2", "quote": "raise ValueError('empty inputs')"}
            ]
        },
    ],
)
def test_unbound_or_missing_evidence_cannot_renew(db, change):
    row = note(db)
    prepare(db)
    with pytest.raises(ExtractionOutputInvalid):
        review.apply(db, "invocation", output(db, **change), now=NOW)
    db.rollback()
    db.refresh(row)
    assert row.last_reviewed_at is None


def test_every_source_subject_is_required_for_renewal(db):
    note(db)
    prepare(db, sources=[SOURCE, {**SOURCE, "id": "s2", "subject": "github:2"}])
    with pytest.raises(ExtractionOutputInvalid, match="omitted"):
        review.apply(db, "invocation", output(db), now=NOW)


@pytest.mark.parametrize(
    "change", ["revision", "scope", "deleted", "disputed", "expired", "stale_evidence"]
)
def test_intervening_changes_and_stale_evidence_cannot_renew(db, change):
    row = note(db)
    prepare(db)
    if change == "revision":
        row.title = "Replacement claim"
    elif change == "scope":
        row.scope = "repo:another/repo"
    elif change == "deleted":
        row.deleted_at = NOW
    elif change == "disputed":
        db.add(Dispute(note_id=row.note_id, reason="new evidence"))
    elif change == "expired":
        row.valid_until = NOW
    db.commit()
    result = review.apply(
        db,
        "invocation",
        output(db),
        now=NOW + timedelta(hours=1) if change == "stale_evidence" else NOW,
    )
    assert result["outcome"] in {"failed", "aborted"}
    db.refresh(row)
    assert row.last_reviewed_at is None


def test_changed_fact_requests_a_dispute_without_silent_claim_rewrite(db, monkeypatch):
    row = note(db)
    prepare(db)
    calls = []
    monkeypatch.setattr(review, "create_dispute", lambda *args: calls.append(args))
    result = review.apply(db, "invocation", output(db, verdict="changed"), now=NOW)
    assert result["outcome"] == "failed" and len(calls) == 1
    assert calls[0][1] == row.note_id
    db.refresh(row)
    assert row.last_reviewed_at is None and row.content_hash == "original-hash"


def test_unsupported_waits_for_revision_but_strict_unsupported_is_eligible(db):
    row = note(db)
    record_outcome(
        db,
        note_id=row.note_id,
        revision=row.revision,
        status="unsupported",
        reason="claim does not match template",
        now=NOW,
    )
    db.commit()
    assert prepare(db) != review.NO_REVIEW
    review.apply(
        db, "invocation", output(db, verdict="unsupported", citations=[]), now=NOW
    )
    assert prepare(db, "second") == review.NO_REVIEW
    row.title = "New claim revision"
    db.commit()
    assert prepare(db, "third") != review.NO_REVIEW


@pytest.mark.parametrize(
    "fields",
    [
        {"scope": "personal:someone"},
        {"scope": "environment:homelab"},
        {"verification_state": "legacy"},
        {"verification_state": "invalidated"},
        {"type": "concept"},
        {"observed_at": NOW, "review_after": NOW + timedelta(days=1)},
    ],
)
def test_admission_scope_and_due_bounds(db, fields):
    note(db, **fields)
    assert prepare(db) == review.NO_REVIEW


def test_missing_sources_and_outage_never_grant_freshness(db):
    row = note(db)
    assert (
        review.prepare(db, "none", collector=lambda _: ([], None), now=NOW)
        == review.NO_REVIEW
    )
    assert db.exec(select(ReviewOutcome)).one().status == "unsupported"
    row.title = "New revision"
    db.commit()

    def unavailable(_):
        raise EvidenceUnavailable("source offline")

    assert (
        review.prepare(db, "outage", collector=unavailable, now=NOW) == review.NO_REVIEW
    )
    assert prepare(db, "backoff") == review.NO_REVIEW
    db.refresh(row)
    assert row.last_reviewed_at is None


def test_daily_limit_and_kill_switch(db, monkeypatch):
    note(db)
    db.add_all(
        [
            FactReviewRun(invocation_key=str(i), note_id="prior", created_at=NOW)
            for i in range(review.MAX_DAILY)
        ]
    )
    db.commit()
    assert prepare(db) == review.NO_REVIEW
    monkeypatch.setenv("KG_FACT_REVIEW_ENABLED", "false")
    assert prepare(db, "disabled") == review.NO_REVIEW
    assert db.get(FactReviewRun, "disabled") is None
