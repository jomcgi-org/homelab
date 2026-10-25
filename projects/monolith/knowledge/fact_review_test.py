"""Evidence, replay, scope, bounds and concurrent-change contracts for prose review."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx
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


@pytest.mark.parametrize("staged", [False, True])
def test_witness_prompt_instructions_are_default_off(db, monkeypatch, staged):
    from knowledge import fact_review_batch

    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", str(staged).lower())
    note(db)
    single = prepare(db)
    batched = fact_review_batch._prompt([db.get(FactReviewRun, "invocation")])
    for prompt in (single, batched):
        assert ("A literal-witness source" in prompt) is staged


@pytest.mark.parametrize("staged", [False, True])
def test_response_byte_limit_preserves_flag_off_character_limit(
    db, monkeypatch, staged
):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", str(staged).lower())
    note(db)
    quote = "é" * 1800
    prepare(db, sources=[SOURCE | {"text": quote}])
    result = output(db, citations=[{"source_id": "s1", "quote": quote}] * 10).replace(
        "\\u00e9", "é"
    )
    assert len(result) < 32_000 < len(result.encode("utf-8"))
    if staged:
        with pytest.raises(ExtractionOutputInvalid, match="bounded JSON"):
            review.apply(db, "invocation", result, now=NOW)
    else:
        assert review.apply(db, "invocation", result, now=NOW)["outcome"] == "renewed"


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
        "unavailable_by_cause": {
            "other_or_unrecorded": {
                "attempts": 2,
                "distinct_facts": 1,
                "repeat_attempts": 1,
            }
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


LARGE_PATH = "projects/monolith/knowledge/extraction.py"


def large_fetch(raw=None, *, revision="a" * 40, calls=None, failure=None):
    raw = (
        raw
        if raw is not None
        else Path(__file__).with_name("extraction.py").read_bytes()
    )
    sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()

    def fetch(url):
        if calls is not None:
            calls.append(url)
        if failure:
            raise failure
        if "/git/ref/" in url:
            return {"object": {"sha": revision}}
        if "/git/trees/" in url:
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": "extraction.py",
                        "size": len(raw),
                        "sha": sha,
                        "type": "blob",
                        "mode": "100644",
                    }
                ],
            }
        assert url.endswith("/git/blobs/" + sha)
        return {
            "size": len(raw),
            "sha": sha,
            "encoding": "base64",
            "content": base64.b64encode(raw).decode(),
        }

    return fetch


def literal_note(db, name="literal", *, two=False):
    title = f"`{LARGE_PATH}` contains the exact text `RAW_BODY_CAP = 60_000`."
    body = (
        f"`{LARGE_PATH}` contains the exact text `DOC_DRIFT_CAP = 10`." if two else None
    )
    return note(db, name, title=title, content=body)


def production_prepare(db, key, *, fetch=None, now=NOW):
    from knowledge.review_sources import collect_sources

    fetch = fetch or large_fetch()
    return review.prepare(
        db,
        key,
        collector=lambda claim: collect_sources(claim, fetch=fetch),
        probe_fetch=fetch,
        now=now,
    )


def witness_output(db, key, **changes):
    run = db.get(FactReviewRun, key)
    citations = [
        {"source_id": source["id"], "quote": source["text"]} for source in run.sources
    ]
    return output(db, key, **({"citations": citations} | changes))


def test_large_source_real_collector_prepare_apply_and_restart_replay(db, monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    literal_note(db, two=True)
    now = datetime.now(UTC)
    prompt = production_prepare(db, "large", now=now)
    assert "omitted_bytes" in prompt
    engine = db.get_bind()
    db.commit()
    with Session(engine) as restarted:
        assert (
            review.prepare(
                restarted,
                "large",
                collector=lambda _: pytest.fail("refetch"),
                probe_fetch=lambda _: pytest.fail("reprobe"),
                now=now,
            )
            == prompt
        )
        answer = witness_output(restarted, "large")
        assert (
            review.apply(restarted, "large", answer, now=now + timedelta(minutes=1))[
                "outcome"
            ]
            == "renewed"
        )
    with Session(engine) as restarted:
        result = review.apply(restarted, "large", "malformed replay", now=now)
        assert result["replayed"] and result["outcome"] == "renewed"
        assert len(restarted.exec(select(ReviewOutcome)).all()) == 1


@pytest.mark.parametrize(
    "case", ["partial", "changed", "fabricated", "stale", "revision", "dispute"]
)
def test_witnesses_cannot_bypass_complete_claim_or_freshness_guards(
    db, monkeypatch, case
):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    row = literal_note(db, two=True)
    now = datetime.now(UTC)
    production_prepare(db, "large", now=now)
    run = db.get(FactReviewRun, "large")
    changes = {}
    if case == "partial":
        changes["citations"] = [
            {"source_id": run.sources[0]["id"], "quote": run.sources[0]["text"]}
        ]
    if case == "changed":
        changes["verdict"] = "changed"
    if case == "fabricated":
        changes["citations"] = [
            {"source_id": run.sources[0]["id"], "quote": "fabricated source quote"}
        ]
    if case == "revision":
        row.revision += 1
        db.commit()
    if case == "dispute":
        db.add(Dispute(note_id=row.note_id, reason="pending correction", state="open"))
        db.commit()
    clock = now + timedelta(hours=1) if case == "stale" else now + timedelta(minutes=1)
    answer = witness_output(db, "large", **changes)
    if case == "fabricated":
        with pytest.raises(ExtractionOutputInvalid):
            review.apply(db, "large", answer, now=clock)
    else:
        result = review.apply(db, "large", answer, now=clock)
        assert result["outcome"] != "renewed"
        if case in {"partial", "changed"}:
            assert result["outcome"] == "unsupported"
            assert not db.exec(select(Dispute)).all()
    db.refresh(row)
    assert utc(row.review_after) == NOW - timedelta(days=10)


def test_unchanged_blocks_survive_restarts_without_spending_selection_allowance(
    db, monkeypatch
):
    from knowledge.models import FactReviewSourceBlock

    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    note(db, content="Behavior of " + LARGE_PATH)
    assert production_prepare(db, "blocked") == review.NO_REVIEW
    assert db.get(FactReviewSourceBlock, "fact").identity["files"][LARGE_PATH]
    engine = db.get_bind()
    db.commit()
    for index in range(4):
        with Session(engine) as restarted:
            calls = []
            assert (
                production_prepare(
                    restarted,
                    f"poll-{index}",
                    fetch=large_fetch(calls=calls),
                    now=NOW + timedelta(minutes=index + 1),
                )
                == review.NO_REVIEW
            )
            assert calls == []
            assert (
                review.throughput(restarted, now=NOW + timedelta(minutes=5))["attempts"]
                == 1
            )
    with Session(engine) as restarted:
        calls = []
        # Main and its directory tree moved, but the cited blob did not.
        assert (
            production_prepare(
                restarted,
                "unrelated",
                fetch=large_fetch(revision="b" * 40, calls=calls),
                now=NOW + timedelta(hours=7),
            )
            == review.NO_REVIEW
        )
        assert len(calls) == 2 and not any("/git/blobs/" in call for call in calls)
        assert (
            review.throughput(restarted, now=NOW + timedelta(hours=7))["attempts"] == 1
        )
    with Session(engine) as restarted:
        # Relevant source coverage can now fit completely. No outcome clearing.
        prompt = production_prepare(
            restarted,
            "changed-source",
            fetch=large_fetch(b"bounded complete source", revision="c" * 40),
            now=NOW + timedelta(hours=14),
        )
        assert "bounded complete source" in prompt
        assert len(restarted.exec(select(ReviewOutcome)).all()) == 1
        assert (
            review.throughput(restarted, now=NOW + timedelta(hours=14))["attempts"] == 2
        )
        assert (
            review._candidate(restarted, NOW + timedelta(hours=14)) is None
        )  # pending fence


def test_unresolved_identity_cannot_be_released_by_a_fresh_probe(db, monkeypatch):
    from knowledge import review_sources
    from knowledge.models import FactReviewSourceBlock
    from knowledge.review_source_blocks import probe

    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    # The claim's pin exhausts its request allowance before identity collection.
    # The independent probe has a fresh allowance and can resolve that same tree.
    monkeypatch.setattr(review_sources, "MAX_REQUESTS", 1)
    note(db, content="Behavior of " + LARGE_PATH)
    assert production_prepare(db, "blocked") == review.NO_REVIEW
    block = db.get(FactReviewSourceBlock, "fact")
    assert block.identity["files"] == {LARGE_PATH: None}
    db.commit()
    with Session(db.get_bind()) as restarted:
        for index in range(1, 4):
            now = NOW + timedelta(hours=7 * index)
            probe(restarted, now, fetch=large_fetch())
            assert not restarted.get(FactReviewSourceBlock, "fact").released
            assert (
                production_prepare(restarted, f"poll-{index}", now=now)
                == review.NO_REVIEW
            )
            assert len(restarted.exec(select(ReviewOutcome)).all()) == 1
            assert review.throughput(restarted, now=now)["attempts"] == 1


@pytest.mark.parametrize("change", ["claim", "policy", "hash"])
@pytest.mark.parametrize("unresolved", [False, True])
def test_changed_claim_or_coverage_policy_is_eligible_without_probe(
    db, monkeypatch, change, unresolved
):
    from knowledge import review_coverage, review_sources

    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    if unresolved:
        monkeypatch.setattr(review_sources, "MAX_REQUESTS", 1)
    row = note(db, content="Behavior of " + LARGE_PATH)
    production_prepare(db, "blocked")
    assert review._candidate(db, NOW) is None
    if change == "claim":
        row.revision += 1
    elif change == "hash":
        row.content_hash = "changed"
    else:
        monkeypatch.setattr(review_coverage, "VERSION", "literal-witness/v2")
    db.commit()
    assert review._candidate(db, NOW).note_id == row.note_id


def test_probe_failure_is_bounded_and_does_not_starve_valid_candidates(db, monkeypatch):
    from knowledge.models import FactReviewSourceBlock
    from knowledge.review_source_blocks import probe

    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    for index in range(4):
        note(db, f"blocked-{index}", content="Behavior of " + LARGE_PATH)
        production_prepare(db, f"blocked-run-{index}")
    now = NOW + timedelta(hours=7)
    calls = []
    probe(
        db,
        now,
        fetch=large_fetch(calls=calls, failure=EvidenceUnavailable("provider offline")),
    )
    assert len(calls) == 1
    engine = db.get_bind()
    db.commit()
    with Session(engine) as restarted:
        probe(
            restarted,
            now + timedelta(minutes=1),
            fetch=lambda _: pytest.fail("global probe cooldown"),
        )
        assert len(restarted.exec(select(FactReviewRun)).all()) == 4
        assert (
            sum(
                block.last_probe_at is not None
                for block in restarted.exec(select(FactReviewSourceBlock)).all()
            )
            == 2
        )
        assert not any(
            block.released
            for block in restarted.exec(select(FactReviewSourceBlock)).all()
        )
        note(restarted, "valid")
        assert review._candidate(restarted, now).note_id == "valid"


def test_transient_large_source_failures_keep_existing_backoff(db, monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    literal_note(db)
    production_prepare(
        db, "outage", fetch=large_fetch(failure=EvidenceUnavailable("provider offline"))
    )
    outcome = db.exec(select(ReviewOutcome)).one()
    assert outcome.status == "unavailable"
    assert review._candidate(db, NOW) is None
    assert (
        review._candidate(db, utc(outcome.next_attempt_at) + timedelta(seconds=1))
        is not None
    )


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("stage", ["/git/ref/", "/git/trees/", "/git/blobs/"])
@pytest.mark.parametrize("anomaly", ["json", "utf8", "encoding", "serialization"])
def test_response_envelope_anomalies_remain_retryable(
    db, monkeypatch, batched, stage, anomaly
):
    from knowledge import fact_review_batch as batch
    from knowledge.models import FactReviewSourceBlock

    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    literal_note(db)
    healthy = large_fetch()
    calls = []
    failing = True

    def transport(request):
        path = request.url.path
        calls.append(path)
        headers = {}
        if failing and stage in path:
            raw = {
                "json": b"not JSON",
                "utf8": b"\xff",
                "encoding": b"{}",
                "serialization": json.dumps(
                    {"x": "é" * 40_000}, ensure_ascii=False
                ).encode(),
            }[anomaly]
            if anomaly == "encoding":
                headers["Content-Encoding"] = "gzip"
        else:
            raw = json.dumps(healthy(path)).encode()
        return httpx.Response(200, stream=httpx.ByteStream(raw), headers=headers)

    monkeypatch.setattr(
        httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(transport))
    )
    prepare_review = batch.prepare if batched else review.prepare
    prepare_review(db, "outage", now=NOW)
    outcome = db.exec(select(ReviewOutcome)).one()
    assert outcome.status == "unavailable"
    assert utc(outcome.next_attempt_at) > NOW
    assert db.exec(select(FactReviewSourceBlock)).all() == []
    assert review._candidate(db, NOW) is None
    assert len(calls) == {"/git/ref/": 1, "/git/trees/": 2, "/git/blobs/": 3}[stage]
    retry_at = utc(outcome.next_attempt_at) + timedelta(seconds=1)
    assert review._candidate(db, retry_at) is not None
    failing = False
    prompt = prepare_review(db, "recovered", now=retry_at)
    assert "omitted_bytes" in prompt
    assert db.exec(select(FactReviewSourceBlock)).all() == []


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
            "payload": json.dumps({"mode": "fact-review", "review_protocol": 3}),
        }
        if state == "scheduled"
        else {}
    )
    assert after == expected
    assert not review.ensure_job(db)


def test_unavailable_cause_counts_are_redacted_and_distinguish_repeats(db):
    inputs = [
        ("one", "unsupported file representation", "file_representation"),
        ("one", "unsupported file representation", "file_representation"),
        ("two", "source collection budget exhausted", "collection_bound"),
        ("three", "GitHub returned 403", "source_access"),
        ("four", "GitHub returned 429", "source_rate_limit"),
        ("five", "GitHub returned 503", "source_server_error"),
        ("six", "PRIVATE_URL_TOKEN_ERROR", "other_or_unrecorded"),
        ("seven", None, "other_or_unrecorded"),
        ("eight", "incomplete check runs", "incomplete_checks"),
        ("nine", "invalid source file", "invalid_source_response"),
    ]
    rows = [
        FactReviewRun(
            invocation_key=f"cause-{i}",
            note_id=note,
            created_at=NOW,
            status="complete",
            result={"outcome": "unavailable", "reason": reason},
        )
        for i, (note, reason, _) in enumerate(inputs)
    ]
    rows.append(
        FactReviewRun(
            invocation_key="model",
            note_id="ten",
            created_at=NOW,
            status="complete",
            result={
                "outcome": "unavailable",
                "summary": "Source review: unavailable (invalid reviewer output)",
            },
        )
    )
    db.add_all(rows)
    db.commit()
    counts = review.throughput(db, now=NOW)["unavailable_by_cause"]
    assert counts["file_representation"] == {
        "attempts": 2,
        "distinct_facts": 1,
        "repeat_attempts": 1,
    }
    assert counts["other_or_unrecorded"] == {
        "attempts": 2,
        "distinct_facts": 2,
        "repeat_attempts": 0,
    }
    assert counts["invalid_reviewer_output"]["attempts"] == 1
    for _, _, cause in inputs[2:6] + inputs[8:]:
        assert counts[cause]["attempts"] == 1
    assert sum(v["attempts"] for v in counts.values()) == 11
    assert "PRIVATE_" not in json.dumps(counts)


def test_eligible_count_uses_worker_scope_pending_and_backoff_rules(db):
    note(db, "eligible")
    note(db, "private-scope", scope="repo:other/repo")
    note(db, "not-due", observed_at=NOW, review_after=NOW + timedelta(days=1))
    waiting = note(db, "waiting")
    unsupported = note(db, "unsupported")
    pending = note(db, "pending")
    record_outcome(
        db,
        note_id=waiting.note_id,
        revision=waiting.revision,
        status="unavailable",
        reason=review.PREFIX + "source collection budget exhausted",
        now=NOW,
    )
    record_outcome(
        db,
        note_id=unsupported.note_id,
        revision=unsupported.revision,
        status="unsupported",
        reason=review.PREFIX + "unsupported claim",
        now=NOW,
    )
    db.add(
        FactReviewRun(
            invocation_key="pending-count",
            note_id=pending.note_id,
            revision=pending.revision,
            created_at=NOW,
            status="prepared",
        )
    )
    db.commit()
    assert review.eligible_count(db, now=NOW) == 1
    assert review.eligible_count(db, now=NOW) == len(
        review._candidates(db, NOW, limit=100)
    )
    assert review.eligible_count(db, now=NOW + timedelta(minutes=6)) == 2
    assert db.get(FactReviewRun, "pending-count").status == "prepared"
