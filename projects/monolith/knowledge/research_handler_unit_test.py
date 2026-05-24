"""Direct unit tests for _sweep_and_select_candidates and _process_one.

Exercises the private helpers in isolation from the top-level
research_gaps_handler, which is already covered end-to-end by
research_handler_test.py.

Scope:
  _sweep_and_select_candidates — selects eligible gaps, recovers stuck rows,
                                  respects RESEARCH_BATCH_SIZE limit.
  _process_one                  — async orchestration: research/personal/discard
                                  dispositions, infra-failure revert, note_id guard.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
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
    _process_one,
    _sweep_and_select_candidates,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="engine")
def engine_fixture():
    _engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(_engine)
        yield _engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture(name="session")
def session_fixture(engine):
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_gap(
    session: Session,
    *,
    term: str = "Test Gap",
    gap_class: str = "external",
    state: str = "classified",
    note_id: str | None = "test-gap",
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


def _research_result_with_claims() -> ResearchResult:
    note = ResearchNote(
        summary="Summary text.",
        claims=[
            Claim(
                text="A verified claim.",
                source_refs=("https://example.com/source",),
            )
        ],
    )
    return ResearchResult(
        disposition="research",
        reason="publicly researchable",
        note=note,
        raw_claims=(note.claims[0],),
        sources=(SourceEntry(tool="WebFetch", ref="https://example.com/source"),),
    )


def _personal_result() -> ResearchResult:
    return ResearchResult(
        disposition="personal",
        reason="appears only in private journal",
        sources=(SourceEntry(tool="Glob", ref="**/*.md"),),
    )


def _discard_result() -> ResearchResult:
    return ResearchResult(
        disposition="discard",
        reason="typo of a common word",
    )


# ---------------------------------------------------------------------------
# _sweep_and_select_candidates
# ---------------------------------------------------------------------------


class TestSweepAndSelectCandidates:
    def test_selects_classified_external_gaps(self, engine, session: Session) -> None:
        _make_gap(session, term="gap-a", note_id="gap-a")
        _make_gap(session, term="gap-b", note_id="gap-b")
        stuck_count, candidates = _sweep_and_select_candidates(engine)
        assert stuck_count == 0
        note_ids = [g.note_id for g in candidates]
        assert "gap-a" in note_ids
        assert "gap-b" in note_ids

    def test_excludes_non_external_gap_class(self, engine, session: Session) -> None:
        _make_gap(
            session, term="internal-gap", note_id="internal-gap", gap_class="internal"
        )
        _make_gap(session, term="hybrid-gap", note_id="hybrid-gap", gap_class="hybrid")
        _, candidates = _sweep_and_select_candidates(engine)
        assert candidates == []

    def test_excludes_non_classified_state(self, engine, session: Session) -> None:
        _make_gap(session, term="in-review", note_id="in-review", state="in_review")
        _make_gap(session, term="committed", note_id="committed", state="committed")
        _make_gap(session, term="parked", note_id="parked", state="parked")
        _, candidates = _sweep_and_select_candidates(engine)
        assert candidates == []

    def test_recovers_single_stuck_researching_row(
        self, engine, session: Session
    ) -> None:
        gap = _make_gap(
            session, term="stuck-gap", note_id="stuck-gap", state="researching"
        )
        stuck_count, candidates = _sweep_and_select_candidates(engine)
        assert stuck_count == 1
        # Recovered row should now appear as a candidate.
        assert any(g.note_id == "stuck-gap" for g in candidates)

    def test_recovers_multiple_stuck_rows(self, engine, session: Session) -> None:
        _make_gap(session, term="stuck-a", note_id="stuck-a", state="researching")
        _make_gap(session, term="stuck-b", note_id="stuck-b", state="researching")
        stuck_count, _ = _sweep_and_select_candidates(engine)
        assert stuck_count == 2

    def test_respects_research_batch_size_limit(self, engine, session: Session) -> None:
        for i in range(RESEARCH_BATCH_SIZE + 5):
            _make_gap(
                session,
                term=f"gap-{i}",
                note_id=f"gap-{i}",
                state="classified",
                gap_class="external",
            )
        _, candidates = _sweep_and_select_candidates(engine)
        assert len(candidates) == RESEARCH_BATCH_SIZE

    def test_returns_empty_list_with_no_gaps(self, engine) -> None:
        stuck_count, candidates = _sweep_and_select_candidates(engine)
        assert stuck_count == 0
        assert candidates == []

    def test_mixed_state_only_classified_external_selected(
        self, engine, session: Session
    ) -> None:
        _make_gap(
            session,
            term="eligible",
            note_id="eligible",
            state="classified",
            gap_class="external",
        )
        _make_gap(
            session,
            term="ineligible-internal",
            note_id="ii",
            state="classified",
            gap_class="internal",
        )
        _make_gap(
            session,
            term="ineligible-in-review",
            note_id="ir",
            state="in_review",
            gap_class="external",
        )
        _, candidates = _sweep_and_select_candidates(engine)
        assert len(candidates) == 1
        assert candidates[0].note_id == "eligible"

    def test_result_ordered_by_id(self, engine, session: Session) -> None:
        # Gaps are inserted in order; results must come back id-ascending.
        for i in range(3):
            _make_gap(session, term=f"ordered-{i}", note_id=f"ordered-{i}")
        _, candidates = _sweep_and_select_candidates(engine)
        ids = [g.id for g in candidates]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# _process_one
# ---------------------------------------------------------------------------


class TestProcessOne:
    @pytest.mark.asyncio
    async def test_research_disposition_writes_raw_and_finalizes_committed(
        self, engine, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session, state="researching")

        with (
            patch(
                "knowledge.research_handler.run_research",
                AsyncMock(return_value=_research_result_with_claims()),
            ),
            patch("knowledge.research_handler._finalize_gap_state") as finalize,
        ):
            await _process_one(engine=engine, gap=gap, vault_root=tmp_path)

        finalize.assert_called_once_with(engine, gap.id, state="committed")
        assert (tmp_path / "_inbox" / "research" / "test-gap.md").exists()

    @pytest.mark.asyncio
    async def test_infra_failure_reverts_to_classified_no_attempt_bump(
        self, engine, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session, state="researching")

        with (
            patch(
                "knowledge.research_handler.run_research",
                AsyncMock(side_effect=RuntimeError("agent timeout")),
            ),
            patch("knowledge.research_handler._finalize_gap_state") as finalize,
        ):
            await _process_one(engine=engine, gap=gap, vault_root=tmp_path)

        finalize.assert_called_once_with(engine, gap.id, state="classified")
        # No _inbox file written — infra failure must not produce output.
        assert not (tmp_path / "_inbox").exists()

    @pytest.mark.asyncio
    async def test_personal_disposition_flips_gap_class_to_internal(
        self, engine, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session, state="researching")
        stub_dir = tmp_path / "_researching"
        stub_dir.mkdir()
        stub = stub_dir / "test-gap.md"
        stub.write_text("---\nid: test-gap\n---\n")

        with (
            patch(
                "knowledge.research_handler.run_research",
                AsyncMock(return_value=_personal_result()),
            ),
            patch("knowledge.research_handler._finalize_gap_state") as finalize,
        ):
            await _process_one(engine=engine, gap=gap, vault_root=tmp_path)

        finalize.assert_called_once_with(
            engine, gap.id, state="classified", gap_class="internal"
        )
        # Stub must be deleted so the reconciler can't revert state.
        assert not stub.exists()

    @pytest.mark.asyncio
    async def test_personal_disposition_tolerates_missing_stub(
        self, engine, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session, state="researching")

        with (
            patch(
                "knowledge.research_handler.run_research",
                AsyncMock(return_value=_personal_result()),
            ),
            patch("knowledge.research_handler._finalize_gap_state") as finalize,
        ):
            # No stub on disk — must not raise.
            await _process_one(engine=engine, gap=gap, vault_root=tmp_path)

        finalize.assert_called_once_with(
            engine, gap.id, state="classified", gap_class="internal"
        )

    @pytest.mark.asyncio
    async def test_discard_disposition_parks_gap(
        self, engine, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session, state="researching")

        with (
            patch(
                "knowledge.research_handler.run_research",
                AsyncMock(return_value=_discard_result()),
            ),
            patch("knowledge.research_handler._finalize_gap_state") as finalize,
        ):
            await _process_one(engine=engine, gap=gap, vault_root=tmp_path)

        finalize.assert_called_once_with(
            engine, gap.id, state="parked", gap_class="parked"
        )

    @pytest.mark.asyncio
    async def test_note_id_none_raises_assertion_error(
        self, engine, session: Session, tmp_path: Path
    ) -> None:
        """A gap with note_id=None must fail loudly rather than produce None.md."""
        gap = _make_gap(session, state="researching", note_id=None)

        with patch(
            "knowledge.research_handler.run_research",
            AsyncMock(return_value=_research_result_with_claims()),
        ):
            with pytest.raises(AssertionError, match="no note_id"):
                await _process_one(engine=engine, gap=gap, vault_root=tmp_path)

    @pytest.mark.asyncio
    async def test_all_claims_dropped_quarantines_below_threshold(
        self, engine, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session, state="researching", research_attempts=0)
        all_dropped = ResearchResult(
            disposition="research",
            reason="publicly researchable",
            note=ResearchNote(summary="x", claims=[]),
            raw_claims=(
                Claim(text="hallucinated", source_refs=("https://hallucinated",)),
            ),
            sources=(SourceEntry(tool="WebSearch", ref="foo"),),
        )

        with (
            patch(
                "knowledge.research_handler.run_research",
                AsyncMock(return_value=all_dropped),
            ),
            patch("knowledge.research_handler._finalize_gap_state") as finalize,
        ):
            await _process_one(engine=engine, gap=gap, vault_root=tmp_path)

        # Below threshold (0+1 < RESEARCH_PARK_THRESHOLD=3) → stays classified.
        finalize.assert_called_once_with(
            engine, gap.id, state="classified", research_attempts=1
        )
        assert (tmp_path / "_failed_research" / "test-gap-1.md").exists()
