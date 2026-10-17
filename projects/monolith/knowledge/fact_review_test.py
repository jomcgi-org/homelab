"""Evidence, replay, scope, bounds and concurrent-change contracts for prose review."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
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


def test_coverage_counts_distinct_current_claims_not_attempts(db):
    note(db, "unassessed")
    for key, status in [
        ("unsupported", "unsupported"),
        ("unavailable", "unavailable"),
        ("failed", "failed"),
        ("revised", "unsupported"),
        ("strict-only", "unsupported"),
    ]:
        row = note(db, key)
        for _ in range(2):
            record_outcome(
                db,
                note_id=key,
                revision=row.revision,
                status=status,
                reason=("strict: " if key == "strict-only" else review.PREFIX) + "test",
                now=NOW,
            )
        if key == "revised":
            row.revision += 1
        db.commit()
    note(db, "current", observed_at=NOW, review_after=NOW + timedelta(days=1))
    note(db, "private-scope", scope="personal:joe")
    note(db, "no-scope", scope=None)
    note(db, "deleted", deleted_at=NOW)
    note(db, "expired", valid_until=NOW)
    note(db, "invalidated", verification_state="invalidated")
    note(db, "other-type", type="concept")
    note(db, "disputed")
    db.add(Dispute(note_id="disputed", reason="test", state="open"))
    db.commit()
    assert review.coverage(db, now=NOW) == {
        "scope": "repo:jomcgi-org/homelab",
        "due": 6,
        "outside_scope_due": 2,
        "latest_outcome": {
            "unassessed": 3,
            "unsupported": 1,
            "unavailable": 1,
            "failed": 1,
        },
    }


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
    monkeypatch.setenv("KG_FACT_REVIEW_MAX_DAILY", "3")
    note(db)
    db.add_all(
        [
            FactReviewRun(invocation_key=str(i), note_id="prior", created_at=NOW)
            for i in range(3)
        ]
    )
    db.commit()
    assert prepare(db) == review.NO_REVIEW
    monkeypatch.setenv("KG_FACT_REVIEW_MAX_DAILY", "20000")
    assert prepare(db, "raised-limit") != review.NO_REVIEW
    monkeypatch.setenv("KG_FACT_REVIEW_ENABLED", "false")
    assert prepare(db, "disabled") == review.NO_REVIEW
    assert db.get(FactReviewRun, "disabled") is None


def test_limit_is_rolling_and_replays_do_not_spend_it_again(db, monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_MAX_DAILY", "1")
    note(db)
    db.add(
        FactReviewRun(
            invocation_key="yesterday",
            note_id="prior",
            created_at=NOW - timedelta(days=1, seconds=1),
        )
    )
    db.commit()
    first = prepare(db)
    assert first != review.NO_REVIEW
    assert prepare(db) == first
    assert prepare(db, "over-limit") == review.NO_REVIEW
    assert (
        len(
            db.exec(
                select(FactReviewRun).where(FactReviewRun.note_id.is_not(None))
            ).all()
        )
        == 2
    )


def test_source_only_progress_is_distinct_from_no_eligible_work(db):
    row = note(db)
    assert (
        review.prepare(
            db,
            "source-only",
            collector=lambda _: ([], None),
            now=NOW,
            progress_signal=True,
        )
        == review.NO_MODEL
    )
    assert (
        review.prepare(
            db,
            "source-only",
            collector=lambda _: pytest.fail("refetched"),
            now=NOW,
            progress_signal=True,
        )
        == review.NO_MODEL
    )
    assert prepare(db, "source-only") == review.NO_REVIEW
    assert (
        review.prepare(db, "empty", now=NOW, progress_signal=True) == review.NO_REVIEW
    )
    db.refresh(row)
    assert row.last_reviewed_at is None


def test_untouched_claims_precede_retries_even_without_file_pointers(db):
    row = note(db, "retry")
    note(db, "untouched", content="The caller must reject unsupported inputs.")
    db.add(
        FactReviewRun(
            invocation_key="prior",
            note_id=row.note_id,
            revision=row.revision,
            status="complete",
            created_at=NOW - timedelta(days=2),
        )
    )
    db.commit()
    assert review._candidate(db, NOW).note_id == "untouched"
    row.revision += 1
    db.commit()
    assert review._candidate(db, NOW).note_id == "retry"


def test_throughput_counts_attempts_and_distinct_facts_separately(db):
    for key, note_id, outcome, created_at in [
        ("a1", "a", "unavailable", NOW),
        ("a2", "a", "unavailable", NOW),
        ("b1", "b", "renewed", NOW),
        ("b2", "b", "renewed", NOW),
        ("c1", "c", None, NOW),
        ("empty", None, None, NOW),
        ("old", "old", "renewed", NOW - timedelta(days=1, seconds=1)),
        ("future", "future", "renewed", NOW + timedelta(seconds=1)),
    ]:
        db.add(
            FactReviewRun(
                invocation_key=key,
                note_id=note_id,
                created_at=created_at,
                status="complete" if outcome else "prepared",
                result={"outcome": outcome} if outcome else {},
            )
        )
    db.commit()
    assert review.throughput(db, now=NOW) == {
        "window_hours": 24,
        "attempts": 5,
        "distinct_facts": 3,
        "repeat_attempts": 2,
        "completed_by_outcome": {
            "unavailable": {"attempts": 2, "distinct_facts": 1},
            "renewed": {"attempts": 2, "distinct_facts": 1},
        },
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("KG_FACT_REVIEW_MAX_DAILY", "0"),
        ("KG_FACT_REVIEW_INTERVAL_SECONDS", "-1"),
        ("KG_FACT_REVIEW_MAX_DAILY", "bad"),
    ],
)
def test_invalid_configuration_is_refused(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        review.settings()


@pytest.mark.parametrize("state", ["scheduled", "leased", "held", "unknown", "stopped"])
def test_registration_scales_only_unlocked_scheduled_jobs(db, monkeypatch, state):
    db.execute(
        text("""CREATE TABLE routine_jobs (
        name TEXT PRIMARY KEY, routine_kind TEXT NOT NULL,
        interval_secs INTEGER, next_run_at TIMESTAMP, payload TEXT,
        created_by TEXT, last_status TEXT, locked_by TEXT, locked_at TIMESTAMP
    )""")
    )
    monkeypatch.setenv("KG_FACT_REVIEW_INTERVAL_SECONDS", "900")
    assert review.ensure_job(db)
    db.execute(
        text("""UPDATE routine_jobs SET payload = :payload,
        next_run_at = :due, locked_by = :holder, locked_at = :locked_at,
        last_status = :status"""),
        {
            "payload": json.dumps({"mode": "fact-review"}),
            "due": None if state == "held" else "2026-10-17 07:00:00",
            "holder": "live-worker" if state == "leased" else None,
            "locked_at": "2026-10-17 06:00:00" if state == "leased" else None,
            "status": {
                "unknown": "invocation_outcome_unknown",
                "stopped": "factory_stopped",
            }.get(state, "ok"),
        },
    )
    before = dict(db.execute(text("SELECT * FROM routine_jobs")).mappings().one())
    monkeypatch.setenv("KG_FACT_REVIEW_INTERVAL_SECONDS", "10")
    assert review.ensure_job(db) is (state == "scheduled")
    after = dict(db.execute(text("SELECT * FROM routine_jobs")).mappings().one())
    expected = before | (
        {
            "interval_secs": 10,
            "payload": json.dumps({"mode": "fact-review", "review_protocol": 2}),
        }
        if state == "scheduled"
        else {}
    )
    assert after == expected
    assert not review.ensure_job(db)
