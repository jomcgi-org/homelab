"""Batch amortization preserves per-fact coverage, revision and replay fences."""

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge import fact_review as review
from knowledge import fact_review_batch as batch
from knowledge.extraction import ExtractionOutputInvalid
from knowledge.freshness import STANDARD, utc
from knowledge.models import FactReviewBatch, FactReviewRun, Note, ReviewOutcome
from knowledge.review_batch_sources import BatchSources

NOW = datetime(2026, 10, 18, tzinfo=UTC)
SHA = "a" * 40
SOURCE = "if not inputs: raise ValueError('empty inputs')"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_ENABLED", "true")
    monkeypatch.setenv("KG_FACT_REVIEW_MAX_DAILY", "20000")
    engine = create_engine(f"sqlite:///{tmp_path / 'batch.db'}").execution_options(
        schema_translate_map={
            table.schema: None for table in SQLModel.metadata.tables.values()
        }
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def notes(db, count=3, *, start=0, paths=None):
    rows = [
        Note(
            note_id=f"fact-{i}",
            path=f"fact-{i}.md",
            title=f"Inputs {i} are checked before dispatch",
            content=f"Empty inputs are refused in {(paths or ['projects/example/app.py'])[i % len(paths or [0])]}",
            content_hash=f"hash-{i}",
            type="fact",
            scope="repo:jomcgi-org/homelab",
            verification_state="verified",
            observed_at=NOW - timedelta(days=100),
            review_after=NOW - timedelta(days=10),
            review_policy=STANDARD,
        )
        for i in range(start, start + count)
    ]
    db.add_all(rows)
    db.commit()
    return rows


def fetch(path):
    if "/git/ref/" in path:
        return {"object": {"sha": SHA}}
    assert path.endswith("?ref=" + SHA)
    return {
        "type": "file",
        "path": path.split("/contents/", 1)[1].split("?", 1)[0],
        "encoding": "base64",
        "content": base64.b64encode(SOURCE.encode()).decode(),
        "size": len(SOURCE),
    }


def prepare(db, key="batch", **kwargs):
    return batch.prepare(
        db, key, fetch=kwargs.pop("fetch", fetch), now=kwargs.pop("now", NOW), **kwargs
    )


def output(db, key="batch"):
    parent = db.get(FactReviewBatch, key)
    items = []
    for i, member in enumerate(parent.member_keys, 1):
        run = db.get(FactReviewRun, member)
        if run.sources:
            items.append(
                {
                    "id": f"f{i}",
                    "claim_hash": review._digest(run.claim),
                    "verdict": "holds",
                    "rationale": "The exact source rejects empty inputs before dispatch occurs.",
                    "citations": [
                        {
                            "source_id": run.sources[0]["id"],
                            "quote": "raise ValueError('empty inputs')",
                        }
                    ],
                }
            )
    return {"reviews": items}


def apply(db, result=None, **kwargs):
    return batch.apply(
        db,
        "batch",
        json.dumps(result or output(db)),
        now=kwargs.pop("now", NOW + timedelta(minutes=1)),
        **kwargs,
    )


def test_one_session_shares_sources_but_renews_32_separate_claims(db):
    rows = notes(db, 32)
    calls = []

    def read(path):
        assert not db.in_transaction()
        calls.append(path)
        return fetch(path)

    prompt = prepare(db, fetch=read)
    data = json.loads(
        prompt.split("BEGIN UNTRUSTED REVIEW DATA\n")[1].split("\nEND UNTRUSTED")[0]
    )
    assert len(calls) == 2 and len(data["sources"]) == 1 and len(data["claims"]) == 32
    result = apply(db, cost_usd=0.25)
    assert result["outcomes"] == {"renewed": 32}
    assert len(db.exec(select(ReviewOutcome)).all()) == 32
    for row in rows:
        db.refresh(row)
        assert utc(row.last_reviewed_at) == NOW
        assert utc(row.observed_at) == NOW - timedelta(days=100)
        assert row.verification_state == "verified"
    stats = batch.status(db, now=NOW + timedelta(minutes=2))
    assert stats["last_hour"]["distinct_completed"] == 32
    assert stats["last_24h"]["source_requests"] == 2
    assert stats["last_24h"]["source_cache_hits"] == 31
    assert stats["last_24h"]["model_cost_usd"] == 0.25
    assert stats["last_24h"]["batches_with_reported_cost"] == 1


def test_prepared_claims_are_fenced_and_cap_counts_facts_not_batches(db, monkeypatch):
    notes(db, 5)
    monkeypatch.setenv("KG_FACT_REVIEW_BATCH_SIZE", "2")
    monkeypatch.setenv("KG_FACT_REVIEW_MAX_DAILY", "3")
    prepare(db)
    prepare(db, "second")
    assert [len(row.member_keys) for row in db.exec(select(FactReviewBatch)).all()] == [
        2,
        1,
    ]
    assert len({row.note_id for row in db.exec(select(FactReviewRun)).all()}) == 3
    assert prepare(db, "capped") == review.NO_REVIEW


def test_replay_keeps_membership_evidence_and_first_response(db):
    notes(db)
    first = prepare(db)
    notes(db, 1, start=100)
    assert prepare(db, fetch=lambda _: pytest.fail("refetched")) == first
    result = apply(db)
    assert (
        batch.apply(db, "batch", "garbage", now=NOW)["outcomes"] == result["outcomes"]
    )
    assert len(db.exec(select(ReviewOutcome)).all()) == 3


def test_domain_api_routes_new_batches_and_legacy_receipts(db, monkeypatch):
    from knowledge import api

    notes(db, 1)
    monkeypatch.setattr("core.db.get_engine", lambda: db.bind)
    original = batch.BatchSources
    monkeypatch.setattr(batch, "BatchSources", lambda **_: original(fetch=fetch))
    assert "BEGIN UNTRUSTED REVIEW DATA" in api.build_fact_review_prompt(
        "batch", batched=True
    )
    assert api.apply_fact_review(db, "batch", json.dumps(output(db)))["outcomes"] == {
        "renewed": 1
    }
    notes(db, 1, start=100)
    review.prepare(
        db,
        "legacy",
        collector=lambda _: (
            [
                {
                    "id": "s1",
                    "subject": "file:projects/example/app.py",
                    "url": "https://example.com/source",
                    "text": SOURCE,
                }
            ],
            NOW,
        ),
        now=NOW,
    )
    # A rolling upgrade can recover an already prepared v2 receipt even when
    # its caller now requests batches. Membership and evidence stay pinned.
    assert "review ONE" in api.build_fact_review_prompt("legacy", batched=True)
    api.fail_fact_review_batch(db, "absent-batch", "nothing to release")


@pytest.mark.parametrize(
    "broken", ["missing", "duplicate", "hash", "quote", "foreign-source", "extra-field"]
)
def test_one_bad_verdict_cannot_renew_itself_or_discard_good_siblings(db, broken):
    rows = notes(db)
    prepare(db)
    data = output(db)
    item = data["reviews"][1]
    if broken == "missing":
        data["reviews"].pop(1)
    elif broken == "duplicate":
        data["reviews"].append(item)
    elif broken == "hash":
        item["claim_hash"] = "b" * 64
    elif broken == "quote":
        item["citations"][0]["quote"] = "a quotation not in the source"
    elif broken == "foreign-source":
        item["citations"][0]["source_id"] = "unknown-source"
    else:
        item["invented"] = True
    assert apply(db, data)["outcomes"] == {"renewed": 2, "unavailable": 1}
    db.refresh(rows[1])
    assert rows[1].last_reviewed_at is None


def test_shared_pool_does_not_allow_cross_claim_citations(db):
    notes(db, 2, paths=["projects/a.py", "projects/b.py"])
    prepare(db)
    data = output(db)
    data["reviews"][1]["citations"] = data["reviews"][0]["citations"]
    assert apply(db, data)["outcomes"] == {"renewed": 1, "unavailable": 1}


def test_changed_revision_and_stale_evidence_fail_independently(db):
    rows = notes(db)
    prepare(db)
    rows[1].title = "Concurrent replacement claim"
    db.commit()
    assert apply(db)["outcomes"] == {"renewed": 2, "failed": 1}
    notes(db, 1, start=10)
    prepare(db, "stale")
    result = batch.apply(
        db, "stale", json.dumps(output(db, "stale")), now=NOW + timedelta(hours=1)
    )
    # The replacement revision is also eligible; its older failed receipt does
    # not suppress a review of the new claim. Both bundles are now stale.
    assert result["outcomes"] == {"failed": 2}


def test_partial_apply_recovery_uses_original_response_and_individual_receipts(db):
    notes(db)
    prepare(db)
    original = output(db)
    calls = 0

    def guard(_):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("ownership lost")

    with pytest.raises(RuntimeError, match="ownership lost"):
        apply(db, original, transaction_guard=guard)
    db.rollback()
    assert len(db.exec(select(ReviewOutcome)).all()) == 1
    result = batch.apply(
        db, "batch", "a replacement model response", now=NOW + timedelta(minutes=2)
    )
    assert result["outcomes"] == {"renewed": 3}
    assert len(db.exec(select(ReviewOutcome)).all()) == 3


@pytest.mark.parametrize(
    "result", ["not JSON", '{"reviews":[]}', '{"reviews":"bad"}', "x" * 128001]
)
def test_whole_batch_invalid_output_is_durable_and_trips_failure_backoff(db, result):
    notes(db)
    prepare(db)
    with pytest.raises(ExtractionOutputInvalid, match="no usable verdicts"):
        batch.apply(db, "batch", result, now=NOW)
    assert db.get(FactReviewBatch, "batch").status == "complete"
    with pytest.raises(ExtractionOutputInvalid, match="no usable verdicts"):
        batch.apply(db, "batch", "replacement result", now=NOW)
    assert all(row.last_reviewed_at is None for row in db.exec(select(Note)).all())
    assert all(
        row.next_attempt_at is not None for row in db.exec(select(ReviewOutcome)).all()
    )


def test_transport_outage_makes_one_request_and_never_chains(db):
    notes(db, 32)
    calls = []

    def unavailable(path):
        calls.append(path)
        raise httpx.ConnectError("source outage")

    assert prepare(db, fetch=unavailable) == batch.SOURCE_UNAVAILABLE
    assert len(calls) == 1
    assert db.get(FactReviewBatch, "batch").metrics["source_requests"] == 1
    assert db.get(FactReviewBatch, "batch").result["outcomes"] == {"unavailable": 32}


def test_prompt_budget_does_not_turn_temporary_deferral_into_unsupported(
    db, monkeypatch
):
    notes(db)
    monkeypatch.setattr(batch, "MAX_PROMPT_BYTES", 100)
    assert prepare(db) == batch.SOURCE_UNAVAILABLE
    assert {row.status for row in db.exec(select(ReviewOutcome)).all()} == {
        "unavailable"
    }


def test_known_failure_releases_with_backoff_but_unresolved_batch_stays_fenced(db):
    notes(db)
    prepare(db)
    assert review._candidate(db, NOW) is None
    batch.fail(db, "batch", "provider failed", now=NOW)
    assert review._candidate(db, NOW) is None
    assert review._candidate(db, NOW + timedelta(minutes=6)) is not None
    batch.fail(db, "batch", "repeat", now=NOW)
    assert len(db.exec(select(ReviewOutcome)).all()) == 3


def test_interrupted_fetch_resumes_same_main_revision_and_saved_sources(db):
    notes(db, 2, paths=["projects/a.py", "projects/b.py"])

    def crash(path):
        if "/b.py?" in path:
            raise RuntimeError("worker lost")
        return fetch(path)

    with pytest.raises(RuntimeError, match="worker lost"):
        prepare(db, fetch=crash)
    calls = []

    def resumed(path):
        calls.append(path)
        assert "/git/ref/" not in path and "/a.py?" not in path
        return fetch(path)

    prepare(db, fetch=resumed)
    assert len(calls) == 1 and db.get(FactReviewBatch, "batch").repo_revision == SHA
    assert apply(db)["outcomes"] == {"renewed": 2}


def test_batch_source_request_and_time_limits_stop_further_network(monkeypatch):
    from knowledge import review_batch_sources as source_batch

    monkeypatch.setattr(source_batch, "MAX_REQUESTS", 2)
    calls = []
    with BatchSources(fetch=lambda path: calls.append(path) or {}) as collector:
        collector.read("/one")
        collector.read("/one")
        collector.read("/two")
        with pytest.raises(ValueError, match="request budget"):
            collector.read("/three")
        assert calls == ["/one", "/two"]
        collector.started -= 100
        with pytest.raises(ValueError, match="deadline"):
            collector.read("/one")


@pytest.mark.parametrize("value", ["0", "33", "bad"])
def test_invalid_batch_size_is_refused(monkeypatch, value):
    monkeypatch.setenv("KG_FACT_REVIEW_BATCH_SIZE", value)
    with pytest.raises(ValueError):
        batch.batch_size()
