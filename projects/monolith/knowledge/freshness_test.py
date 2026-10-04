"""Deterministic temporal policy, non-renewal, backfill and recall contracts."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge.freshness import (
    MAX_INTERVAL,
    STANDARD,
    VOLATILE,
    classify,
    current_predicate,
    deadline,
    metadata,
    preserve_deadline,
    state,
    utc,
)
from knowledge.freshness_backfill import backfill
from knowledge.frontmatter import ParsedFrontmatter, parse
from knowledge.models import Chunk, Note, RawInput
from knowledge.notes import _serialize_frontmatter
from knowledge.recall import (
    RECALL_HEADER,
    expire_recall,
    render_recall_block,
    render_related_notes,
)
from knowledge.store import KnowledgeStore

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'freshness.db'}").execution_options(
        schema_translate_map={
            table.schema: None for table in SQLModel.metadata.tables.values()
        }
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def note(name="fact", **fields):
    defaults = {
        "note_id": name,
        "path": f"{name}.md",
        "title": "Evidence about a function",
        "content_hash": "h",
        "content": "Stable implementation detail",
        "observed_at": NOW - timedelta(days=1),
        "verification_state": "verified",
        "scope": "repo:test/repo",
    }
    defaults.update(fields)
    return Note(**defaults)


@pytest.mark.parametrize(
    "title",
    [
        "PR #6804 is open and ready at head abc",
        "Issue #10 is closed",
        "Job 4411223 is running",
        "Workflow run 998877 checks passing",
        "Checks on de02262a35e2 are passing",
        "Operational acceptance for #6812 remains outstanding",
        "See https://github.com/jomcgi-org/homelab/pull/6821 merged",
    ],
)
def test_volatile_policy(title):
    policy = classify(title=title, content=None, now=NOW)
    assert policy == VOLATILE
    assert deadline(observed_at=NOW, policy=policy, now=NOW) == NOW + timedelta(
        hours=24
    )


@pytest.mark.parametrize(
    ("title", "content"),
    [
        (
            "Funding evidence uses the bug-fix task class window",
            "The window is chosen by task class.\n\n## Evidence\n\n"
            "- Independent review of PR #6214 at head d7ea299 found it open and red\n"
            "- job failed, workflow check complete",
        ),
        (
            "Admission discovery runs before existing-receipt short-circuit",
            "Discovery precedes the short-circuit.\n\n## Provenance\n\n"
            "This issue is complete; PR checks passed at SHA abc.",
        ),
        (
            "Sources are cited",
            "Durable claim.\n\n### Sources:\nPR 12 is merged, status closed.\n"
            "\n## Notes\nStable detail.",
        ),
    ],
)
def test_durable_claims_citing_pr_evidence_stay_standard(title, content):
    assert classify(title=title, content=content, now=NOW) == STANDARD


def test_volatile_claim_body_survives_evidence_exclusion():
    content = (
        "Stable claim.\n\n## Evidence\n\n- stable pointer\n\n"
        "## Status\nPR #6804 is open and ready."
    )
    assert classify(title="Durable title", content=content, now=NOW) == VOLATILE
    after_evidence = "Claim.\n\n## Evidence\n\n- x\n\n## Follow-up\nPR #12 is open."
    assert classify(title="Durable title", content=after_evidence, now=NOW) == VOLATILE


@pytest.mark.parametrize(
    "claim",
    [
        "Live pilot is still required before enabling.",
        "Still needs a live pilot.",
        "Operational validation is outstanding.",
        "Must verify after deploy.",
        "Blocked on Joe's approval.",
        "Waiting for the rollout.",
        "TODO: run the pilot.",
        "Follow-up: enable the CronWorkflow.",
        "Deployment has not been verified yet.",
    ],
)
def test_outstanding_operational_gate_is_volatile_without_an_instance(claim):
    assert classify(title=claim, content=None, now=NOW) == VOLATILE
    assert classify(title="Status", content=claim, now=NOW) == VOLATILE
    assert classify(title="Rule", content=f"## Evidence\n{claim}", now=NOW) == STANDARD


def test_maximum_uses_elapsed_utc_and_supplied_only_shortens():
    observed = NOW.astimezone(timezone(timedelta(hours=-7)))
    assert (
        classify(title="An implementation detail", content="function works", now=NOW)
        == STANDARD
    )
    for supplied in (None, NOW + timedelta(days=91), "bad-date"):
        assert (
            deadline(observed_at=observed, policy=STANDARD, supplied=supplied, now=NOW)
            == NOW + MAX_INTERVAL
        )
    assert deadline(
        observed_at=NOW, policy=STANDARD, now=NOW, supplied=NOW + timedelta(hours=1)
    ) == NOW + timedelta(hours=1)


@pytest.mark.parametrize(
    "observed", [None, "nonsense", "2026-99-99", NOW + timedelta(seconds=1)]
)
def test_missing_malformed_future_observations_are_unknown(observed):
    assert deadline(observed_at=observed, policy=STANDARD, now=NOW) is None
    assert (
        state(observed_at=observed, review_after=NOW + MAX_INTERVAL, now=NOW)
        == "unknown"
    )


def test_equality_is_due_independent_of_verification():
    assert state(observed_at=NOW - MAX_INTERVAL, review_after=NOW, now=NOW) == "due"
    assert (
        state(observed_at=NOW, review_after=NOW + timedelta(microseconds=1), now=NOW)
        == "current"
    )


def upsert(db, *, observed=NOW, extra=None, title="Stable detail"):
    KnowledgeStore(db, now=NOW).upsert_note(
        note_id="stored",
        path="stored.md",
        title=title,
        content_hash="h",
        content="Evidence",
        metadata=ParsedFrontmatter(
            observed_at=observed, verification_state="verified", extra=extra or {}
        ),
        chunks=[],
        vectors=[],
        links=[],
    )
    return db.exec(select(Note).where(Note.note_id == "stored")).one()


@pytest.mark.parametrize(
    "operation", ["ingestion", "reindex", "retelling", "frontmatter", "failed-check"]
)
def test_ordinary_writes_cannot_renew_or_replace_original_observation(
    session, operation
):
    original = NOW - timedelta(days=80)
    stored = upsert(session, observed=original)
    due = utc(stored.review_after)
    if operation == "frontmatter":
        parsed, _ = parse(
            _serialize_frontmatter(
                ParsedFrontmatter(
                    observed_at=NOW,
                    extra={
                        "review_after": (NOW + MAX_INTERVAL).isoformat(),
                        "last_reviewed_at": NOW.isoformat(),
                    },
                ),
                "body",
            )
        )
        extra = parsed.extra
    else:
        extra = {
            "review_after": (NOW + MAX_INTERVAL).isoformat(),
            "last_reviewed_at": NOW.isoformat(),
        }
    stored = upsert(session, observed=NOW, extra=extra)
    assert utc(stored.observed_at) == original
    assert utc(stored.review_after) == due
    assert stored.last_reviewed_at is None


def test_unknown_retelling_does_not_gain_lease_and_volatile_rewrite_only_shortens(
    session,
):
    upsert(session, observed=None)
    stored = upsert(session, observed=NOW)
    assert stored.review_after is None
    session.delete(stored)
    session.commit()
    stored = upsert(session, observed=NOW - timedelta(days=2))
    stored = upsert(session, title="PR #12 is open")
    assert utc(stored.review_after) == NOW - timedelta(days=1)
    assert stored.review_policy == VOLATILE
    stored = upsert(session, title="Stable detail")
    assert stored.review_policy == VOLATILE
    assert utc(stored.review_after) == NOW - timedelta(days=1)


def test_database_check_rejects_extended_or_baseless_deadline(session):
    for fields in (
        {"review_after": NOW + timedelta(days=90)},
        {"observed_at": None, "review_after": NOW},
    ):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add_all([note(**fields)])
            session.flush()


def test_sql_predicate_excludes_null_equal_and_future_preserving_history(session):
    rows = [
        note("current", review_after=NOW + timedelta(days=1)),
        note("equal", review_after=NOW),
        note("unknown"),
        note(
            "future",
            observed_at=NOW + timedelta(days=1),
            review_after=NOW + timedelta(days=2),
        ),
    ]
    session.add_all(rows)
    session.commit()
    found = session.exec(select(Note).where(current_predicate(now=NOW)).limit(10)).all()
    assert [row.note_id for row in found] == ["current"]
    assert len(session.exec(select(Note).limit(10)).all()) == 4


def test_backfill_dry_run_counts_replay_pagination_and_preserved_fields(session):
    rows = [
        note("a-current"),
        note("b-old", observed_at=NOW - timedelta(days=100)),
        note("c-volatile", title="PR #12 is open", observed_at=NOW - timedelta(days=2)),
        note("d-unknown", observed_at=None, verification_state="disputed"),
        note("e-future", observed_at=NOW + timedelta(days=1)),
        note("f-short", review_after=NOW, review_policy=STANDARD),
    ]
    session.add_all(rows)
    session.commit()
    before = [
        (row.id, row.note_id, row.observed_at, row.verification_state, row.content_hash)
        for row in rows
    ]
    result = backfill(session, now=NOW)
    assert result["policies"] == {STANDARD: 5, VOLATILE: 1}
    assert result["freshness"] == {"current": 1, "due": 3, "unknown": 2}
    assert rows[0].review_policy is None
    page = backfill(session, now=NOW, apply=True, batch_size=2, max_batches=1)
    assert page["next_after"] == "b-old"
    rest = backfill(session, now=NOW, apply=True, after=page["next_after"])
    assert rest["count"] == 4
    deadlines = [row.review_after for row in rows]
    backfill(session, now=NOW + timedelta(days=1), apply=True)
    assert [row.review_after for row in rows] == deadlines
    assert [
        (row.id, row.note_id, row.observed_at, row.verification_state, row.content_hash)
        for row in rows
    ] == before
    assert backfill(session, now=NOW, apply=True, pending_only=True)["count"] == 0


def test_backfill_operational_error_is_explicit_and_atomic(session, monkeypatch):
    session.add_all([note("a"), note("b")])
    session.commit()
    original = session.exec
    calls = 0

    def broken(query):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OperationalError("SELECT", {}, RuntimeError("unavailable"))
        return original(query)

    monkeypatch.setattr(session, "exec", broken)
    with pytest.raises(OperationalError):
        backfill(session, now=NOW, apply=True, batch_size=1)
    monkeypatch.setattr(session, "exec", original)
    assert all(
        row.review_policy is None for row in session.exec(select(Note).limit(2)).all()
    )


DURABLE_RULES = [
    "Required CI must succeed on the exact review head",
    "Factory delivery requires a successful implementation run naming the PR",
    "Advisory semgrep failure can fail the combined GitHub status despite "
    "required CI success",
    "Changing the node workflow identity strands in-flight factory work during deploys",
    "Repository acceptance does not substitute for live drained-loss validation",
    "A merged PR is closed, and an open issue with a failing check blocks the queue",
    "Live validation requires an operational gate and the rollout is pending",
]


@pytest.mark.parametrize("claim", DURABLE_RULES)
def test_durable_rule_phrasing_without_a_concrete_instance_stays_standard(claim):
    assert classify(title=claim, content=None, now=NOW) == STANDARD
    assert classify(title="Rule", content=claim, now=NOW) == STANDARD


@pytest.mark.parametrize(
    "claim",
    [
        "PR #6821 is open",
        "Issue 6812 is closed",
        "The head is de02262a35e2 and green",
        "Run 1234567 failed",
    ],
)
def test_concrete_instance_asserted_in_a_current_state_is_volatile(claim):
    assert classify(title=claim, content=None, now=NOW) == VOLATILE


def test_instance_and_state_must_share_a_sentence_and_instance_must_be_concrete():
    assert (
        classify(
            title="Rule",
            content="PR #12 introduced the helper.\nThe queue is open to anyone.",
            now=NOW,
        )
        == STANDARD
    )
    # A word made of hex letters, or bare digits, is not a SHA.
    assert (
        classify(title="Rule", content="The face is decaded and open", now=NOW)
        == STANDARD
    )
    assert classify(title="Rule", content="Port 12345678 is open", now=NOW) == STANDARD


def recall_item(snippet="evidence", **fields):
    return {"note_id": "fact", "title": "t", "snippet": snippet, **fields}


def test_recall_dates_and_persisted_block_expire_at_equality():
    row = note(title="PR #12 is open")
    preserve_deadline(row, now=NOW)
    item = {
        "note_id": "fact",
        "title": row.title,
        "snippet": "evidence",
        **metadata(row, now=NOW),
    }
    assert "observed 2026-10-02" in render_related_notes([item])[0]
    assert (
        "new authoritative observation required before action"
        in render_related_notes([item])[0]
    )
    block = render_recall_block([item], expires=NOW)
    text = "base\n\n" + block
    assert expire_recall(text, now=NOW - timedelta(microseconds=1)) == text
    assert expire_recall(text, now=NOW) == "base"
    assert expire_recall(block, now=NOW) is None
    unparseable = block.replace(NOW.isoformat(), "nonsense")
    assert expire_recall(unparseable, now=NOW) is None


def test_expire_recall_ignores_a_snippet_that_quotes_the_header():
    quoting = recall_item(RECALL_HEADER + "quoted header, no marker\n" + RECALL_HEADER)
    block = render_recall_block([quoting, recall_item("tail")], expires=NOW)
    text = "task\n\n" + block
    assert expire_recall(text, now=NOW - timedelta(seconds=1)) == text
    assert expire_recall(text, now=NOW) == "task"


def test_expire_recall_ignores_a_snippet_that_quotes_a_whole_block():
    inner = render_recall_block([recall_item()], expires=NOW - timedelta(days=9))
    block = render_recall_block([recall_item(inner)], expires=NOW)
    text = "task\n\n" + block
    assert expire_recall(text, now=NOW - timedelta(seconds=1)) == text
    assert expire_recall(text, now=NOW) == "task"


def test_expire_recall_keeps_task_text_that_quotes_an_expired_block():
    task = "Now do the real task: fix bug X."
    quoted = render_recall_block([recall_item()], expires=NOW - timedelta(days=1))
    for text in (
        f"Review this prior prompt:\n\n{quoted}\n\n{task}",
        f"{quoted}\n\n{task}",
        f"Review {RECALL_HEADER}RECALL_EXPIRES {NOW.isoformat()}\n{task}",
        f"{RECALL_HEADER}item...",
    ):
        assert expire_recall(text, now=NOW) == text
    appended = f"Review this prior prompt:\n\n{quoted}\n\n{task}\n\n" + (
        render_recall_block([recall_item()], expires=NOW)
    )
    assert expire_recall(appended, now=NOW) == (
        f"Review this prior prompt:\n\n{quoted}\n\n{task}"
    )


@pytest.mark.parametrize("caller", ["api", "recall", "extraction"])
def test_current_context_callers_exclude_due_during_hydration(
    session, monkeypatch, caller
):
    from knowledge import api, extraction, recall, store as store_module

    current = note("current", review_after=NOW + timedelta(seconds=1))
    due = note("due", review_after=NOW)
    session.add_all([current, due])
    session.flush()
    chunks = [
        Chunk(
            note_fk=row.id,
            chunk_index=0,
            section_header="",
            chunk_text=row.note_id + " evidence " * 30,
            embedding=[0.1] * 1024,
        )
        for row in (current, due)
    ]
    session.add_all(chunks)
    session.commit()
    # A rank result can become due before hydration, so never trust it alone.
    monkeypatch.setattr(
        store_module,
        "_rank_search_chunks",
        lambda *args, **kwargs: [
            (current.id, chunks[0].id, 0.99),
            (due.id, chunks[1].id, 0.99),
        ],
    )
    store = KnowledgeStore(session, now=NOW)
    monkeypatch.setattr(store_module, "KnowledgeStore", lambda db: store)
    monkeypatch.setattr(api, "KnowledgeStore", lambda db: store)
    monkeypatch.setenv("KNOWLEDGE_DEFAULT_REPO_SCOPE", "repo:test/repo")
    vector = [0.1] * 1024
    if caller == "api":
        results = api.search_notes(session, vector, scope_filter="repo:test/repo")
        assert [row["note_id"] for row in results] == ["current"]
    elif caller == "recall":
        results = recall.search_related(session, vector, limit=5)
        assert [row["note_id"] for row in results] == ["current"]
    else:
        monkeypatch.setattr(extraction.raw_store, "fetch_raw", lambda key: "Raw body")
        monkeypatch.setattr(
            extraction,
            "EmbeddingClient",
            lambda: type("Embedding", (), {"embed": AsyncMock(return_value=vector)})(),
        )
        prompt = extraction.build_extraction_prompt(
            session,
            RawInput(raw_id="raw", path="raw.md", content_hash="h", source="test"),
        )
        assert "- [current]" in prompt
        assert "- [due]" not in prompt
    history = store.search_notes_with_context(vector, include_history=True)
    assert {row["note_id"] for row in history} == {"current", "due"}
    store._now = NOW + timedelta(seconds=1)
    assert store.search_notes_with_context(vector) == []
