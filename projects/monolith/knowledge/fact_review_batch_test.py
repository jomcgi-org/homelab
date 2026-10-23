"""Batch amortization preserves per-fact coverage, revision and replay fences."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge import fact_review as review
from knowledge import fact_review_batch as batch
from knowledge.extraction import ExtractionOutputInvalid
from knowledge.freshness import STANDARD, utc
from knowledge.models import FactReviewBatch, FactReviewRun, Note, ReviewOutcome
from knowledge.review_batch_sources import BatchSources
from knowledge.review_sources import EvidenceUnsupported, collect_sources

NOW = datetime(2026, 10, 18, tzinfo=UTC)
SHA = "a" * 40
SOURCE = "if not inputs: raise ValueError('empty inputs')"


def coverage_fetch(files, *, calls=None, revision=SHA, unavailable=None):
    blobs = {}
    entries = []
    for path, raw in files.items():
        sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        blobs[sha] = (path, raw)
        entries.append(
            {
                "path": Path(path).name,
                "size": len(raw),
                "sha": sha,
                "type": "blob",
                "mode": "100644",
            }
        )

    def read(url):
        if calls is not None:
            calls.append(url)
        if "/git/ref/" in url:
            return {"object": {"sha": revision}}
        if "/git/trees/" in url:
            return {"truncated": False, "tree": entries}
        sha = url.rsplit("/", 1)[-1]
        path, raw = blobs[sha]
        if path == unavailable:
            raise httpx.ReadTimeout("source timeout")
        return {
            "sha": sha,
            "size": len(raw),
            "encoding": "base64",
            "content": base64.b64encode(raw).decode(),
        }

    return read


def streamed_coverage(
    monkeypatch, files, calls, *, pin_padding=0, tree_padding=0, blob_padding=0
):
    fetch = coverage_fetch(files, calls=calls)

    def transport(request):
        body = fetch(request.url.path)
        base = "https://api.github.com/repos/jomcgi-org/homelab"
        if "object" in body:
            body.update(
                ref="refs/heads/main",
                node_id="REF_kwDORcE7qq9yZWZzL2hlYWRzL21haW4",
                url=base + "/git/refs/heads/main",
            )
            body["object"].update(
                type="commit", url=base + "/git/commits/" + body["object"]["sha"]
            )
        if "tree" in body:
            body.update(sha="c" * 40, url=base + "/git/trees/" + "c" * 40)
            for entry in body["tree"]:
                entry["url"] = base + "/git/blobs/" + entry["sha"]
        if "content" in body:
            # GitHub base64 blobs wrap at 60 characters, including a final LF.
            encoded = body["content"]
            body["content"] = "".join(
                encoded[start : start + 60] + "\n"
                for start in range(0, len(encoded), 60)
            )
            body["url"] = base + "/git/blobs/" + body["sha"]
            body["node_id"] = "B_kwDORcE7qtoAK" + body["sha"]
        padding = (
            pin_padding
            if "object" in body
            else tree_padding
            if "tree" in body
            else blob_padding
        )
        raw = json.dumps(body).encode() + b" " * padding
        return httpx.Response(200, stream=httpx.ByteStream(raw))

    monkeypatch.setattr(
        httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(transport))
    )


@pytest.mark.parametrize(
    "count,size,pin_padding,tree_padding",
    [
        (4, 110_000, 0, 0),
        (3, 127_000, 0, 0),
        (3, 123_400, 0, 0),
        (3, 115_000, 30_000, 0),
        (3, 115_000, 0, 30_000),
    ],
)
@pytest.mark.parametrize("batched", [False, True])
def test_streamed_aggregate_blob_limit_precedes_reads_and_preserves_siblings(
    monkeypatch, count, size, pin_padding, tree_padding, batched
):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    files = {
        f"projects/example/large-{index}.py": bytes([65 + index]) * size
        for index in range(count)
    }
    claim = {"title": "Behavior of " + " and ".join(files), "content": None}
    files["projects/example/small.py"] = b"bounded complete source"
    calls = []
    streamed_coverage(
        monkeypatch, files, calls, pin_padding=pin_padding, tree_padding=tree_padding
    )
    with BatchSources() as reader:
        revision = reader.pin() if batched else None
        with pytest.raises(EvidenceUnsupported, match="byte bound") as error:
            if batched:
                reader.collect(claim, revision)
            else:
                collect_sources(claim)
        assert all(error.value.identity["files"].values())
        assert len(error.value.identity["files"]) == count
        assert not any("/git/blobs/" in path for path in calls)
        if batched:
            assert reader.stopped is None
            sources = reader.collect({"title": "projects/example/small.py"}, revision)
            assert sources[0]["text"] == "bounded complete source"
            assert sum("/git/blobs/" in path for path in calls) == 1


@pytest.mark.parametrize("blob_padding", [0, 2000])
def test_streamed_blob_envelope_bound_is_enforced_without_stopping_batch(
    monkeypatch, blob_padding
):
    from knowledge import review_coverage as coverage

    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    files = {
        f"projects/example/large-{index}.py": bytes([65 + index]) * 115_000
        for index in range(3)
    }
    clauses = [
        f"`{path}` contains the exact text `{raw[:8].decode()}`."
        for path, raw in files.items()
    ]
    claim = {"title": clauses[0], "content": "\n".join(clauses[1:])}
    calls = []
    streamed_coverage(monkeypatch, files, calls, blob_padding=blob_padding)
    with BatchSources() as reader:
        revision = reader.pin()
        if blob_padding:
            with pytest.raises(
                EvidenceUnsupported, match="response byte bound"
            ) as error:
                reader.collect(claim, revision)
            assert all(error.value.identity["files"].values())
            assert sum("/git/blobs/" in path for path in calls) == 1
        else:
            sources = reader.collect(claim, revision)
            assert len(sources) == 3
            assert sum("/git/blobs/" in path for path in calls) == 3
        assert reader.transfer_bytes <= coverage.MAX_TRANSFER_BYTES
        assert reader.stopped is None


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


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "failure", [401, 403, 429, 500, 502, 503, httpx.ConnectError, httpx.ConnectTimeout]
)
def test_streamed_batch_stops_after_provider_failure(db, monkeypatch, enabled, failure):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", str(enabled).lower())
    monkeypatch.setenv("KG_FACT_REVIEW_BATCH_SIZE", "32")
    notes(db, 32, paths=[f"projects/member-{i}/app.py" for i in range(32)])
    calls = []

    def transport(request):
        calls.append(request.url.path)
        if "/git/ref/" in request.url.path:
            return httpx.Response(
                200,
                stream=httpx.ByteStream(json.dumps({"object": {"sha": SHA}}).encode()),
            )
        if isinstance(failure, int):
            return httpx.Response(failure)
        raise failure("provider unavailable")

    monkeypatch.setattr(
        httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(transport))
    )
    assert prepare(db, fetch=None) == batch.SOURCE_UNAVAILABLE
    assert len(calls) == 2  # one pin and the first member, never the other 31
    parent = db.get(FactReviewBatch, "batch")
    assert parent.metrics["source_requests"] == 2
    assert parent.result["outcomes"] == {"unavailable": 32}
    assert all(
        utc(outcome.next_attempt_at) > NOW
        for outcome in db.exec(select(ReviewOutcome)).all()
    )


@pytest.mark.parametrize("failure", [404, httpx.ReadTimeout])
def test_enabled_streamed_batch_isolates_member_failure(monkeypatch, failure):
    from knowledge.review_sources import EvidenceUnavailable

    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    calls = []

    def transport(request):
        calls.append(request.url.path)
        if request.url.path == "/failed":
            if isinstance(failure, int):
                return httpx.Response(failure)
            raise failure("member timeout")
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok": true}'))

    with BatchSources() as reader:
        reader.client.close()
        reader.client = httpx.Client(transport=httpx.MockTransport(transport))
        with pytest.raises(EvidenceUnavailable):
            reader.read("/failed")
        assert reader.stopped is None
        assert reader.read("/healthy") == {"ok": True}
        assert calls == ["/failed", "/healthy"]


def test_prompt_budget_does_not_turn_temporary_deferral_into_unsupported(
    db, monkeypatch
):
    notes(db)
    monkeypatch.setattr(batch, "MAX_PROMPT_BYTES", 100)
    assert prepare(db) == batch.SOURCE_UNAVAILABLE
    assert {row.status for row in db.exec(select(ReviewOutcome)).all()} == {
        "unavailable"
    }


def test_staged_mixed_batch_shared_large_source_and_failure_isolation(db, monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    path = "projects/monolith/knowledge/extraction.py"
    files = {
        path: Path(__file__).with_name("extraction.py").read_bytes(),
        "projects/monolith/knowledge/small.py": SOURCE.encode(),
        "projects/monolith/knowledge/offline.py": b"unavailable source",
    }
    rows = notes(
        db,
        5,
        paths=[
            path,
            path,
            path,
            "projects/monolith/knowledge/offline.py",
            "projects/monolith/knowledge/small.py",
        ],
    )
    for row, literal in zip(rows[:2], ["RAW_BODY_CAP = 60_000", "DOC_DRIFT_CAP = 10"]):
        row.title = f"`{path}` contains the exact text `{literal}`."
        row.content = row.title
    db.commit()
    calls = []
    prompt = prepare(
        db,
        fetch=coverage_fetch(
            files, calls=calls, unavailable="projects/monolith/knowledge/offline.py"
        ),
    )
    parent = db.get(FactReviewBatch, "batch")
    runs = [db.get(FactReviewRun, key) for key in parent.member_keys]
    assert {run.note_id: run.result.get("outcome") for run in runs} == {
        "fact-0": None,
        "fact-1": None,
        "fact-2": "unsupported",
        "fact-3": "unavailable",
        "fact-4": None,
    }
    assert len(calls) == 5  # pin, shared directory, three distinct blobs
    assert parent.metrics["source_cache_hits"] >= 4
    assert len(prompt.encode()) <= batch.MAX_PROMPT_BYTES
    engine = db.get_bind()
    answer = {"reviews": []}
    for index, run in enumerate(runs, 1):
        if run.sources:
            answer["reviews"].append(
                {
                    "id": f"f{index}",
                    "claim_hash": review._digest(run.claim),
                    "verdict": "holds",
                    "rationale": "Every positive literal and the complete small source have exact assigned quotations.",
                    "citations": [
                        {"source_id": s["id"], "quote": s["text"]} for s in run.sources
                    ],
                }
            )
    db.commit()
    with Session(engine) as restarted:
        assert (
            prepare(restarted, fetch=lambda _: pytest.fail("replay refetched"))
            == prompt
        )
        result = batch.apply(
            restarted, "batch", json.dumps(answer), now=NOW + timedelta(minutes=1)
        )
        assert result["outcomes"] == {"renewed": 3, "unsupported": 1, "unavailable": 1}
        replay = batch.apply(restarted, "batch", "bad replay", now=NOW)
        assert replay["outcomes"] == result["outcomes"]
        assert len(restarted.exec(select(ReviewOutcome)).all()) == 5


def test_batch_rejects_cross_claim_witnesses_without_poisoning_sibling(db, monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    path = "projects/monolith/knowledge/extraction.py"
    rows = notes(db, 2, paths=[path])
    for row, literal in zip(rows, ["RAW_BODY_CAP = 60_000", "DOC_DRIFT_CAP = 10"]):
        row.title = f"`{path}` contains the exact text `{literal}`."
        row.content = None
    db.commit()
    prepare(
        db,
        fetch=coverage_fetch(
            {path: Path(__file__).with_name("extraction.py").read_bytes()}
        ),
    )
    parent = db.get(FactReviewBatch, "batch")
    first, second = [db.get(FactReviewRun, key) for key in parent.member_keys]
    assert first.sources[0]["id"] != second.sources[0]["id"]
    answer = {
        "reviews": [
            {
                "id": f"f{i}",
                "claim_hash": review._digest(run.claim),
                "verdict": "holds",
                "rationale": "The witness supports this literal presence clause in the source.",
                "citations": [
                    {
                        "source_id": second.sources[0]["id"],
                        "quote": second.sources[0]["text"],
                    }
                ],
            }
            for i, run in enumerate([first, second], 1)
        ]
    }
    result = batch.apply(
        db, "batch", json.dumps(answer), now=NOW + timedelta(minutes=1)
    )
    assert result["outcomes"] == {"unavailable": 1, "renewed": 1}


def test_batch_block_reconsideration_does_not_charge_unchanged_polls(db, monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    path = "projects/monolith/knowledge/extraction.py"
    notes(db, 3, paths=[path])
    raw = Path(__file__).with_name("extraction.py").read_bytes()
    assert prepare(db, fetch=coverage_fetch({path: raw})) == review.NO_MODEL
    engine = db.get_bind()
    db.commit()
    with Session(engine) as restarted:
        assert (
            prepare(
                restarted,
                "unchanged",
                fetch=lambda _: pytest.fail("unexpected network"),
                now=NOW + timedelta(minutes=1),
            )
            == review.NO_REVIEW
        )
        assert (
            review.throughput(restarted, now=NOW + timedelta(minutes=1))["attempts"]
            == 3
        )
        # Probe at most two candidates. One remains blocked without starving others.
        prompt = prepare(
            restarted,
            "changed",
            fetch=coverage_fetch({path: SOURCE.encode()}, revision="b" * 40),
            now=NOW + timedelta(hours=7),
        )
        assert SOURCE in prompt
        assert len(restarted.get(FactReviewBatch, "changed").member_keys) == 2
        assert (
            review.throughput(restarted, now=NOW + timedelta(hours=7))["attempts"] == 5
        )
        assert (
            prepare(
                restarted,
                "pending",
                fetch=lambda _: pytest.fail("pending/reprobe"),
                now=NOW + timedelta(hours=7, minutes=1),
            )
            == review.NO_REVIEW
        )


def test_staged_cache_counts_escaped_bytes_and_rejected_transfers(monkeypatch):
    from knowledge import review_batch_sources as cache
    from knowledge.review_sources import EvidenceUnavailable, EvidenceUnsupported

    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    monkeypatch.setattr(cache, "MAX_CACHE_BYTES", 300_000)
    value = {"x": "é" * 35_000}
    with BatchSources(fetch=lambda _: value) as reader:
        with pytest.raises(EvidenceUnsupported):
            reader.read("/oversized")
        assert reader.cache == {}
        with pytest.raises(EvidenceUnavailable, match="transport byte budget"):
            reader.read("/oversized-again")
        assert reader.requests == 2 and reader.stopped


def test_staged_batch_prompt_limit_preserves_backoff_and_valid_sibling(db, monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    notes(db, 2, paths=["projects/a.py", "projects/b.py"])
    monkeypatch.setattr(batch, "MAX_PROMPT_BYTES", 12_000)
    prompt = prepare(
        db,
        fetch=coverage_fetch(
            {"projects/a.py": SOURCE.encode(), "projects/b.py": b"x" * 20_000}
        ),
    )
    parent = db.get(FactReviewBatch, "batch")
    runs = [db.get(FactReviewRun, key) for key in parent.member_keys]
    assert runs[0].sources and runs[1].result["outcome"] == "unavailable"
    assert len(prompt.encode()) <= batch.MAX_PROMPT_BYTES


def test_known_failure_releases_with_backoff_but_unresolved_batch_stays_fenced(db):
    notes(db)
    prepare(db)
    assert review._candidate(db, NOW) is None
    batch.fail(db, "batch", "provider failed", now=NOW)
    assert review._candidate(db, NOW) is None
    assert review._candidate(db, NOW + timedelta(minutes=6)) is not None
    batch.fail(db, "batch", "repeat", now=NOW)
    assert len(db.exec(select(ReviewOutcome)).all()) == 3


def test_prestart_recovery_receipts_share_the_callers_transaction(db):
    notes(db)
    prepare(db)
    batch.fail(db, "batch", "proved no session was started", now=NOW, commit=False)
    assert db.get(FactReviewBatch, "batch").status == "complete"
    db.rollback()
    assert db.get(FactReviewBatch, "batch").status == "prepared"
    assert db.exec(select(ReviewOutcome)).all() == []
    assert all(row.status == "prepared" for row in db.exec(select(FactReviewRun)).all())
    batch.fail(db, "batch", "proved no session was started", now=NOW, commit=False)
    db.commit()
    assert db.get(FactReviewBatch, "batch").status == "complete"
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


def test_operator_abandonment_fences_old_writers_and_releases_only_unfinished_claims(
    db,
):
    notes(db, 2)
    keys = batch._reserve(db, "unknown-batch", NOW)
    completed = db.get(FactReviewRun, keys[0])
    completed.status, completed.completed_at, completed.result = (
        "complete",
        NOW,
        {"outcome": "supported"},
    )
    db.commit()
    db.refresh(completed)
    before = completed.model_dump()
    counts = batch.abandon_for_recomputation(db, "unknown-batch")
    db.commit()
    assert counts == {"abandoned_members": 1, "retained_completed_members": 1}
    assert db.get(FactReviewRun, keys[0]).model_dump() == before
    assert db.get(FactReviewRun, keys[1]).status == "abandoned"
    assert db.get(FactReviewRun, keys[1]).completed_at is None
    for operation in (
        lambda: batch.prepare(db, "unknown-batch", now=NOW),
        lambda: batch.apply(db, "unknown-batch", "bad", now=NOW),
        lambda: batch._finish(db, "unknown-batch", NOW),
        lambda: batch._failure(db, keys[1], "unavailable", "late failure", NOW),
        lambda: review.apply(db, keys[1], "bad", now=NOW),
        lambda: review.prepare(db, keys[1], now=NOW),
    ):
        with pytest.raises(ExtractionOutputInvalid, match="abandoned"):
            operation()
        db.rollback()
    batch.fail(db, "unknown-batch", "late failure", now=NOW)
    assert db.get(FactReviewRun, keys[0]).model_dump() == before
    assert db.get(FactReviewRun, keys[1]).status == "abandoned"
    new_keys = batch._reserve(db, "replacement", NOW)
    assert any(
        db.get(FactReviewRun, k).note_id == db.get(FactReviewRun, keys[1]).note_id
        for k in new_keys
    )
    assert db.get(FactReviewBatch, "unknown-batch").status == "abandoned"


def test_recovery_during_source_fetch_fences_late_single_writer(db):
    notes(db, 1)
    key = batch._reserve(db, "inflight", NOW)[0]

    def collector(claim):
        batch.abandon_for_recomputation(db, "inflight")
        db.commit()
        return [{"id": "late", "text": "late source"}], NOW

    with pytest.raises(ExtractionOutputInvalid, match="abandoned"):
        review.prepare(db, key, collector=collector, now=NOW)
    db.rollback()
    assert db.get(FactReviewRun, key).status == "abandoned"
    assert db.get(FactReviewRun, key).sources == []
    assert db.exec(select(ReviewOutcome)).first() is None
