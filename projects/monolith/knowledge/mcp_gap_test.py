"""Tests for the gap lifecycle MCP tools.

Uses a real in-memory SQLite engine (patched in for ``get_engine``) so the
tools run their real ``Session(get_engine())`` path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import yaml
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.gaps import GAPS_PIPELINE_VERSION
from knowledge.mcp import (
    answer_gap,
    approve_research_gap,
    get_review_queue,
    list_gaps,
)
from knowledge.models import Gap, Note


@pytest.fixture(name="engine")
def engine_fixture():
    engine = create_engine(
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
        SQLModel.metadata.create_all(engine)
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture(name="session")
def session_fixture(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture(name="patched_engine")
def patched_engine_fixture(engine):
    """Patch knowledge.mcp.get_engine so tools open sessions on our in-memory DB."""
    with patch("knowledge.mcp.get_engine", return_value=engine):
        yield engine


def _make_source_note(session: Session, note_id: str = "src") -> Note:
    note = Note(
        note_id=note_id,
        path=f"_processed/{note_id}.md",
        title=note_id,
        content_hash=f"hash-{note_id}",
        type="atom",
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def _make_gap(
    session: Session,
    *,
    term: str,
    source_fk: int,
    state: str = "in_review",
    gap_class: str | None = "internal",
    created_at: datetime | None = None,
) -> Gap:
    gap = Gap(
        term=term,
        context="",
        gap_class=gap_class,
        state=state,
        pipeline_version=GAPS_PIPELINE_VERSION,
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)
    return gap


class TestListGapsTool:
    @pytest.mark.asyncio
    async def test_returns_all_gaps_by_default(self, session, patched_engine):
        src = _make_source_note(session)
        _make_gap(
            session, term="a", source_fk=src.id, state="discovered", gap_class=None
        )
        _make_gap(session, term="b", source_fk=src.id, state="in_review")

        result = await list_gaps()
        terms = sorted(g["term"] for g in result["gaps"])
        assert terms == ["a", "b"]

    @pytest.mark.asyncio
    async def test_filters_by_state_and_class(self, session, patched_engine):
        src = _make_source_note(session)
        _make_gap(
            session,
            term="ext",
            source_fk=src.id,
            state="classified",
            gap_class="external",
        )
        _make_gap(
            session,
            term="intr",
            source_fk=src.id,
            state="in_review",
            gap_class="internal",
        )
        _make_gap(
            session, term="hyb", source_fk=src.id, state="in_review", gap_class="hybrid"
        )

        result = await list_gaps(state="in_review", gap_class="internal,hybrid")
        terms = sorted(g["term"] for g in result["gaps"])
        assert terms == ["hyb", "intr"]

    @pytest.mark.asyncio
    async def test_limit_forwarded(self, session, patched_engine):
        src = _make_source_note(session)
        for i in range(5):
            _make_gap(
                session,
                term=f"t{i}",
                source_fk=src.id,
                state="discovered",
                gap_class=None,
            )

        result = await list_gaps(limit=2)
        assert len(result["gaps"]) == 2

    @pytest.mark.asyncio
    async def test_list_gaps_strips_whitespace_in_state(self, session, patched_engine):
        """MCP state CSV must match HTTP behavior — trim whitespace between segments.

        Regression: the old ``state.split(",")`` passed ``" classified"`` as a
        literal filter value, silently dropping every ``classified`` gap when an
        LLM wrote a natural space after the comma.
        """
        src = _make_source_note(session)
        _make_gap(
            session,
            term="intr",
            source_fk=src.id,
            state="in_review",
            gap_class="internal",
        )
        _make_gap(
            session,
            term="cls",
            source_fk=src.id,
            state="classified",
            gap_class="external",
        )

        result = await list_gaps(state="in_review, classified")
        terms = sorted(g["term"] for g in result["gaps"])
        assert terms == ["cls", "intr"]

    @pytest.mark.asyncio
    async def test_list_gaps_clamps_limit_upper_bound(self, session, patched_engine):
        """Oversized MCP limit is clamped to the HTTP max (defense in depth)."""
        src = _make_source_note(session)
        for i in range(3):
            _make_gap(
                session,
                term=f"t{i}",
                source_fk=src.id,
                state="discovered",
                gap_class=None,
            )

        result = await list_gaps(limit=1_000_000)
        assert len(result["gaps"]) == 3


class TestReviewQueueTool:
    @pytest.mark.asyncio
    async def test_returns_only_internal_hybrid_in_review(
        self, session, patched_engine
    ):
        src = _make_source_note(session)
        now = datetime.now(timezone.utc)
        _make_gap(
            session,
            term="first-internal",
            source_fk=src.id,
            state="in_review",
            gap_class="internal",
            created_at=now - timedelta(seconds=30),
        )
        _make_gap(
            session,
            term="second-hybrid",
            source_fk=src.id,
            state="in_review",
            gap_class="hybrid",
            created_at=now - timedelta(seconds=20),
        )
        _make_gap(
            session,
            term="external",
            source_fk=src.id,
            state="classified",
            gap_class="external",
            created_at=now - timedelta(seconds=10),
        )

        result = await get_review_queue()
        terms = [g["term"] for g in result["gaps"]]
        assert terms == ["first-internal", "second-hybrid"]

    @pytest.mark.asyncio
    async def test_empty_queue(self, patched_engine):
        result = await get_review_queue()
        assert result == {"gaps": []}


class TestAnswerGapTool:
    @pytest.mark.asyncio
    async def test_happy_path(self, session, patched_engine, tmp_path, monkeypatch):
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        src = _make_source_note(session)
        gap = _make_gap(
            session,
            term="Linkerd mTLS",
            source_fk=src.id,
            state="in_review",
            gap_class="internal",
        )

        result = await answer_gap(gap.id, "Linkerd uses per-pod sidecars on 4143.")

        assert result["gap_id"] == gap.id
        assert result["note_id"] == "linkerd-mtls"
        assert result["path"] == "_processed/linkerd-mtls.md"

        written = (tmp_path / result["path"]).read_text()
        _, fm_block, body = written.split("---\n", 2)
        fm = yaml.safe_load(fm_block)
        assert fm["id"] == "linkerd-mtls"
        assert fm["source_tier"] == "personal"
        assert "Linkerd uses per-pod sidecars" in body

        session.expire_all()
        reloaded = session.get(Gap, gap.id)
        assert reloaded.state == "committed"

    @pytest.mark.asyncio
    async def test_unknown_id_returns_error_dict(
        self, patched_engine, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        result = await answer_gap(9999, "some answer")
        assert "error" in result
        assert "Gap not found" in result["error"]

    @pytest.mark.asyncio
    async def test_wrong_state_returns_error_dict(
        self, session, patched_engine, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        src = _make_source_note(session)
        gap = _make_gap(
            session,
            term="still-discovered",
            source_fk=src.id,
            state="discovered",
            gap_class=None,
        )

        result = await answer_gap(gap.id, "x")
        assert "error" in result
        assert "expected 'in_review'" in result["error"]

    @pytest.mark.asyncio
    async def test_frontmatter_terminator_returns_error_dict(
        self, session, patched_engine, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        src = _make_source_note(session)
        gap = _make_gap(
            session,
            term="injectable",
            source_fk=src.id,
            state="in_review",
            gap_class="internal",
        )

        result = await answer_gap(gap.id, "foo\n---\nbar")
        assert "error" in result
        assert "frontmatter terminator" in result["error"]


class TestApproveResearchGapTool:
    @pytest.mark.asyncio
    async def test_happy_path(self, session, patched_engine):
        """approve_research_gap flips in_review external -> classified."""
        src = _make_source_note(session)
        gap = _make_gap(
            session,
            term="merkle-tree",
            source_fk=src.id,
            state="in_review",
            gap_class="external",
        )

        result = await approve_research_gap(gap.id)

        assert "error" not in result
        assert result["state"] == "classified"

        session.expire_all()
        reloaded = session.get(Gap, gap.id)
        assert reloaded.state == "classified"
        assert reloaded.human_verified is True

    @pytest.mark.asyncio
    async def test_unknown_id_returns_error_dict(self, patched_engine):
        result = await approve_research_gap(9999)
        assert "error" in result
        assert "Gap not found" in result["error"]

    @pytest.mark.asyncio
    async def test_wrong_class_returns_error_dict(self, session, patched_engine):
        """Internal/hybrid gaps cannot be approved; tool returns error dict."""
        src = _make_source_note(session)
        gap = _make_gap(
            session,
            term="my-therapist",
            source_fk=src.id,
            state="in_review",
            gap_class="internal",
        )

        result = await approve_research_gap(gap.id)
        assert "error" in result
        assert "expected 'external'" in result["error"]
