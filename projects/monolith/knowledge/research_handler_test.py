"""Tests for the research-gaps scheduled-job handler.

Mocks ``run_research`` directly (the single boundary). Exercises:
  - happy path (research disposition with surviving claims)
  - infra failure (RuntimeError from run_research)
  - personal disposition (gap_class flip, stub removed)
  - discard disposition (gap parked, stub removed)
  - all-claims-dropped quarantine below threshold (stub kept for retry)
  - park-at-threshold after repeated quarantines (stub removed)
  - race-lost (state already changed before lock)
  - stuck-row recovery sweep
  - privacy guard (non-external gap skipped after lock)

Stub-removal assertions guard against the reconcile-revert loop where
a stub left at ``status: classified`` causes the reconciler to overwrite
the handler's terminal-state DB write on its next cycle, so the same
gap gets researched indefinitely.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.models import Gap
from knowledge.research_agent import (
    Claim,
    ResearchNote,
    ResearchResult,
    SourceEntry,
)
from knowledge.research_handler import (
    RESEARCH_BATCH_SIZE,
    RESEARCH_CONCURRENCY,
    RESEARCH_PARK_THRESHOLD,
    research_gaps_handler,
)


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Strip the ``knowledge`` schema for the in-memory SQLite engine
    # (SQLite has no schemas). Restored after the test so other modules
    # importing the metadata still see the production-shaped tables.
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _make_gap(
    session: Session,
    *,
    term: str = "Linkerd mTLS",
    gap_class: str | None = "external",
    state: str = "classified",
    note_id: str | None = "linkerd-mtls",
    research_attempts: int = 0,
) -> Gap:
    gap = Gap(
        term=term,
        gap_class=gap_class,
        state=state,
        note_id=note_id,
        research_attempts=research_attempts,
        pipeline_version="test",
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)
    return gap


def _write_stub(vault_root: Path, slug: str) -> Path:
    """Write a minimal classified gap stub at _researching/<slug>.md."""
    stub_dir = vault_root / "_researching"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / f"{slug}.md"
    stub.write_text(
        "---\n"
        f"id: {slug}\n"
        f"title: {slug}\n"
        "type: gap\n"
        "status: classified\n"
        "gap_class: external\n"
        "---\n"
    )
    return stub


def _research_result_with_claims() -> ResearchResult:
    note = ResearchNote(
        summary="Linkerd uses mTLS for service-to-service auth.",
        claims=[
            Claim(
                text="Linkerd terminates mTLS at the proxy.",
                source_refs=("https://linkerd.io/2.13/features/mtls/",),
            )
        ],
    )
    return ResearchResult(
        disposition="research",
        reason="publicly-researchable concept",
        note=note,
        raw_claims=(note.claims[0],),
        sources=(
            SourceEntry(tool="WebFetch", ref="https://linkerd.io/2.13/features/mtls/"),
        ),
    )


def _research_result_all_dropped() -> ResearchResult:
    return ResearchResult(
        disposition="research",
        reason="publicly-researchable concept",
        note=ResearchNote(summary="x", claims=[]),
        raw_claims=(
            Claim(text="claim Sonnet emitted", source_refs=("https://hallucinated",)),
        ),
        sources=(SourceEntry(tool="WebSearch", ref="foo"),),
    )


@pytest.mark.asyncio
async def test_research_disposition_writes_raw_and_marks_committed(
    session: Session, tmp_path: Path
) -> None:
    gap = _make_gap(session)

    with patch(
        "knowledge.research_handler.run_research",
        AsyncMock(return_value=_research_result_with_claims()),
    ):
        await research_gaps_handler(session=session, vault_root=tmp_path)

    session.refresh(gap)
    assert gap.state == "committed"
    assert gap.research_attempts == 0
    assert (tmp_path / "_inbox" / "research" / "linkerd-mtls.md").exists()
    assert not (tmp_path / "_failed_research").exists()


@pytest.mark.asyncio
async def test_personal_disposition_flips_gap_class_and_removes_stub(
    session: Session, tmp_path: Path
) -> None:
    gap = _make_gap(session)
    stub = _write_stub(tmp_path, "linkerd-mtls")

    personal = ResearchResult(
        disposition="personal",
        reason="term appears only in journal-style notes",
        sources=(SourceEntry(tool="Glob", ref="glob:**/*.md"),),
    )

    with patch(
        "knowledge.research_handler.run_research",
        AsyncMock(return_value=personal),
    ):
        await research_gaps_handler(session=session, vault_root=tmp_path)

    session.refresh(gap)
    assert gap.gap_class == "internal"
    assert gap.state == "classified"
    assert gap.research_attempts == 0  # not bumped
    assert not stub.exists()  # reconciler can no longer revert the DB row
    assert not (tmp_path / "_inbox").exists()
    assert not (tmp_path / "_failed_research").exists()


@pytest.mark.asyncio
async def test_personal_disposition_tolerates_missing_stub(
    session: Session, tmp_path: Path
) -> None:
    """Stub deletion is idempotent: a missing stub is not an error."""
    gap = _make_gap(session)
    # Deliberately do NOT write a stub.

    personal = ResearchResult(
        disposition="personal",
        reason="term appears only in journal-style notes",
        sources=(SourceEntry(tool="Glob", ref="glob:**/*.md"),),
    )

    with patch(
        "knowledge.research_handler.run_research",
        AsyncMock(return_value=personal),
    ):
        await research_gaps_handler(session=session, vault_root=tmp_path)

    session.refresh(gap)
    assert gap.gap_class == "internal"
    assert gap.state == "classified"


@pytest.mark.asyncio
async def test_discard_disposition_parks_gap_and_marks_stub_discardable(
    session: Session, tmp_path: Path
) -> None:
    """Discards plug into the Phase A discardable-rewrite path.

    The stub must stay in place with ``triaged: discardable`` so the next
    gardener tick sees it and (eventually) rewrites source wikilinks.
    Status and gap_class are set to ``parked`` so the reconciler projects
    matching values onto the Gap row instead of reverting it.
    """
    gap = _make_gap(session)
    stub = _write_stub(tmp_path, "linkerd-mtls")

    discard = ResearchResult(
        disposition="discard",
        reason="looks like a typo of `food`",
    )

    with patch(
        "knowledge.research_handler.run_research",
        AsyncMock(return_value=discard),
    ):
        await research_gaps_handler(session=session, vault_root=tmp_path)

    session.refresh(gap)
    assert gap.gap_class == "parked"
    assert gap.state == "parked"
    assert gap.research_attempts == 0  # not bumped
    assert stub.exists(), "stub must remain so the gardener can pick up the marker"

    import yaml as _yaml

    parts = stub.read_text().split("---\n", 2)
    fm = _yaml.safe_load(parts[1])
    assert fm["triaged"] == "discardable"
    assert fm["status"] == "parked"
    assert fm["gap_class"] == "parked"

    assert not (tmp_path / "_inbox").exists()
    assert not (tmp_path / "_failed_research").exists()


@pytest.mark.asyncio
async def test_discard_disposition_tolerates_missing_stub(
    session: Session, tmp_path: Path
) -> None:
    """Marker write is idempotent: missing stub is not an error."""
    gap = _make_gap(session)
    # Deliberately do NOT write a stub.

    discard = ResearchResult(
        disposition="discard",
        reason="not worth tracking",
    )

    with patch(
        "knowledge.research_handler.run_research",
        AsyncMock(return_value=discard),
    ):
        await research_gaps_handler(session=session, vault_root=tmp_path)

    session.refresh(gap)
    assert gap.state == "parked"
    assert gap.gap_class == "parked"
    assert not (tmp_path / "_researching" / "linkerd-mtls.md").exists()


@pytest.mark.asyncio
async def test_discard_marker_is_idempotent_on_already_marked_stub(
    session: Session, tmp_path: Path
) -> None:
    """Re-discarding an already-marked stub doesn't churn the file's mtime."""
    gap = _make_gap(session)
    stub = _write_stub(tmp_path, "linkerd-mtls")

    # Pre-mark the stub as if a previous discard had already run.
    import yaml as _yaml

    parts = stub.read_text().split("---\n", 2)
    fm = _yaml.safe_load(parts[1])
    fm["triaged"] = "discardable"
    fm["status"] = "parked"
    fm["gap_class"] = "parked"
    stub.write_text(f"---\n{_yaml.dump(fm, sort_keys=False)}---\n{parts[2]}")
    pre_mtime = stub.stat().st_mtime_ns

    discard = ResearchResult(
        disposition="discard",
        reason="duplicate gap",
    )

    with patch(
        "knowledge.research_handler.run_research",
        AsyncMock(return_value=discard),
    ):
        await research_gaps_handler(session=session, vault_root=tmp_path)

    assert stub.stat().st_mtime_ns == pre_mtime, (
        "already-marked stub must not be rewritten"
    )


@pytest.mark.asyncio
async def test_all_claims_dropped_quarantines_and_keeps_stub_below_threshold(
    session: Session, tmp_path: Path
) -> None:
    """Below threshold the stub stays so the next tick retries the gap."""
    gap = _make_gap(session)
    stub = _write_stub(tmp_path, "linkerd-mtls")

    with patch(
        "knowledge.research_handler.run_research",
        AsyncMock(return_value=_research_result_all_dropped()),
    ):
        await research_gaps_handler(session=session, vault_root=tmp_path)

    session.refresh(gap)
    assert gap.research_attempts == 1
    assert gap.state == "classified"  # below threshold
    assert stub.exists()  # retained for the next research tick
    assert (tmp_path / "_failed_research" / "linkerd-mtls-1.md").exists()
    assert not (tmp_path / "_inbox" / "research" / "linkerd-mtls.md").exists()


@pytest.mark.asyncio
async def test_quarantine_at_threshold_parks_gap_and_removes_stub(
    session: Session, tmp_path: Path
) -> None:
    gap = _make_gap(session, research_attempts=RESEARCH_PARK_THRESHOLD - 1)
    stub = _write_stub(tmp_path, "linkerd-mtls")

    with patch(
        "knowledge.research_handler.run_research",
        AsyncMock(return_value=_research_result_all_dropped()),
    ):
        await research_gaps_handler(session=session, vault_root=tmp_path)

    session.refresh(gap)
    assert gap.research_attempts == RESEARCH_PARK_THRESHOLD
    assert gap.state == "parked"
    assert not stub.exists()


@pytest.mark.asyncio
async def test_runtime_error_reverts_state_no_attempt_bump(
    session: Session, tmp_path: Path
) -> None:
    gap = _make_gap(session)

    with patch(
        "knowledge.research_handler.run_research",
        AsyncMock(side_effect=RuntimeError("claude exit 1: boom")),
    ):
        await research_gaps_handler(session=session, vault_root=tmp_path)

    session.refresh(gap)
    assert gap.state == "classified"
    assert gap.research_attempts == 0  # infra failure does NOT burn an attempt
    assert not (tmp_path / "_inbox").exists()
    assert not (tmp_path / "_failed_research").exists()


@pytest.mark.asyncio
async def test_stuck_researching_rows_swept_to_classified(
    session: Session, tmp_path: Path
) -> None:
    """Rows left in 'researching' from a previous crashed tick get
    recovered to 'classified' before the SELECT picks candidates."""
    gap = _make_gap(session, state="researching")

    with patch(
        "knowledge.research_handler.run_research",
        AsyncMock(return_value=_research_result_with_claims()),
    ):
        await research_gaps_handler(session=session, vault_root=tmp_path)

    session.refresh(gap)
    # Recovered to 'classified', then picked up, then committed.
    assert gap.state == "committed"


@pytest.mark.asyncio
async def test_race_lost_skips_gap(session: Session, tmp_path: Path) -> None:
    """If the SELECT returns no candidates because state was already
    moved out of 'classified' before the handler ran, run_research is
    never invoked."""
    gap = _make_gap(session)

    runner = AsyncMock(return_value=_research_result_with_claims())
    with patch("knowledge.research_handler.run_research", runner):
        # Move the gap out of 'classified' BEFORE the handler runs, so
        # the SELECT in the handler returns nothing. The recovery sweep
        # only catches 'researching'; 'in_review' is left alone.
        gap.state = "in_review"
        session.add(gap)
        session.commit()
        await research_gaps_handler(session=session, vault_root=tmp_path)

    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_external_gap_skipped_by_privacy_guard(
    session: Session, tmp_path: Path
) -> None:
    """Even if a 'classified' gap with non-external gap_class somehow
    reaches the loop (e.g. a future SELECT bug), the per-gap
    re-assertion skips it before any Sonnet call."""
    gap = _make_gap(session, gap_class="external", state="classified")

    runner = AsyncMock(return_value=_research_result_with_claims())
    with patch("knowledge.research_handler.run_research", runner):
        # Mutate gap_class out from under the SELECT, so the per-gap
        # re-assertion catches it.
        gap.gap_class = "internal"
        session.add(gap)
        session.commit()
        await research_gaps_handler(session=session, vault_root=tmp_path)

    runner.assert_not_awaited()
    session.refresh(gap)
    assert gap.gap_class == "internal"  # unchanged by handler


@pytest.mark.asyncio
async def test_no_candidates_logs_and_returns(session: Session, tmp_path: Path) -> None:
    runner = AsyncMock(return_value=_research_result_with_claims())
    with patch("knowledge.research_handler.run_research", runner):
        await research_gaps_handler(session=session, vault_root=tmp_path)
    runner.assert_not_awaited()


def test_concurrency_constants_consistent() -> None:
    """Batch size should not exceed concurrency, or the semaphore queues
    work that we could just have gathered. Equal is the intended shape."""
    assert RESEARCH_BATCH_SIZE <= RESEARCH_CONCURRENCY


@pytest.mark.asyncio
async def test_full_batch_processed_concurrently(
    session: Session, tmp_path: Path
) -> None:
    """A tick with N gaps fans them all out via asyncio.gather and each
    one reaches a terminal disposition.

    Uses an asyncio.Event to verify concurrency: the mocked run_research
    blocks until N parallel callers have entered, then releases all of
    them. If the handler ran sequentially, only one caller would ever
    enter at a time and the test would deadlock.

    The to_thread DB helpers (_lock_and_fetch_gap, _finalize_gap_state)
    are mocked because each fresh session would race for the StaticPool's
    single in-memory SQLite connection; under N concurrent worker
    threads this hits sqlite3.InterfaceError. Production (Postgres) has
    a real connection pool so each fresh session gets its own
    connection. End-to-end DB state is covered by the other tests in
    this file.
    """
    n = 5
    gaps = [_make_gap(session, term=f"term-{i}", note_id=f"slug-{i}") for i in range(n)]
    gap_by_id = {g.id: g for g in gaps}

    entered = asyncio.Event()
    in_flight = 0

    async def mocked_run_research(*, term: str, vault_root: Path) -> ResearchResult:
        nonlocal in_flight
        in_flight += 1
        if in_flight >= n:
            entered.set()
        # Fail fast if the handler is sequential -- a 1s wait would
        # multiply by N if serialized, but completes once if parallel.
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        return _research_result_with_claims()

    runner = AsyncMock(side_effect=mocked_run_research)
    with (
        patch("knowledge.research_handler.run_research", runner),
        patch(
            "knowledge.research_handler._lock_and_fetch_gap",
            lambda _engine, gap_id: gap_by_id[gap_id],
        ),
        patch("knowledge.research_handler._finalize_gap_state") as finalize,
    ):
        await research_gaps_handler(session=session, vault_root=tmp_path)
    assert runner.await_count == n
    # Every gap reached a terminal _finalize_gap_state call.
    assert finalize.call_count == n
    # Every gap wrote its raw to a distinct slug -- no collision under
    # parallel writes.
    for i in range(n):
        assert (tmp_path / "_inbox" / "research" / f"slug-{i}.md").exists()
