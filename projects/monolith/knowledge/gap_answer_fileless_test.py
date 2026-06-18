"""Unit tests for the fileless ``answer_gap`` path (Task 2).

``answer_gap`` no longer writes ``_processed/<slug>.md``: it routes through the
shared fileless index helper ``knowledge.mcp._index_atom`` (the same core
``create_atom`` uses) and sets the gap committed in Postgres. These tests mock
``_index_atom`` so they stay pure unit tests (no embeddings, no network),
mirroring how ``mcp_test.py::TestCreateAtom`` mocks ``index_note_from_raw``.

Uses the in-memory SQLite + schema-strip fixture pattern shared with the
other gap tests (SQLite does not enforce the Postgres CHECK constraints).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.gaps import GAPS_PIPELINE_VERSION, answer_gap
from knowledge.models import Gap


@pytest.fixture(name="session")
def session_fixture():
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
    state: str = "in_review",
    gap_class: str | None = "internal",
) -> int:
    gap = Gap(
        term=term,
        context="networking note",
        gap_class=gap_class,
        state=state,
        pipeline_version=GAPS_PIPELINE_VERSION,
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)
    return gap.id


@pytest.mark.asyncio
async def test_answer_in_review_gap_creates_atom_and_commits(session):
    gap_id = _make_gap(session, term="Linkerd mTLS", gap_class="internal")

    index = AsyncMock(return_value="linkerd-mtls")
    with patch("knowledge.mcp._index_atom", index):
        result = await answer_gap(
            session,
            gap_id,
            "Linkerd enables mTLS via per-pod sidecar proxies on port 4143.",
        )

    # _index_atom drove the fileless atom build with personal/private framing.
    index.assert_awaited_once()
    kwargs = index.call_args.kwargs
    assert kwargs["title"] == "Linkerd mTLS"
    assert kwargs["type"] == "atom"
    assert kwargs["visibility"] == "private"
    assert kwargs["source_tier"] == "personal"
    assert "Linkerd enables mTLS" in kwargs["body"]

    # Result shape dropped the on-disk path.
    assert result == {"gap_id": gap_id, "note_id": "linkerd-mtls"}
    assert "path" not in result

    gap = session.get(Gap, gap_id)
    assert gap.state == "committed"
    assert gap.human_verified is True
    assert gap.note_id == "linkerd-mtls"
    assert gap.resolved_at is not None
    assert gap.answer.startswith("Linkerd enables mTLS")


@pytest.mark.asyncio
async def test_answer_with_frontmatter_terminator_raises(session):
    gap_id = _make_gap(session, term="some-term")

    index = AsyncMock()
    with patch("knowledge.mcp._index_atom", index):
        with pytest.raises(ValueError, match="frontmatter terminator"):
            await answer_gap(session, gap_id, "foo\n---\nbar")

    index.assert_not_awaited()
    gap = session.get(Gap, gap_id)
    assert gap.state == "in_review"
    assert gap.answer is None
    assert gap.resolved_at is None


@pytest.mark.asyncio
async def test_tombstone_answer_rejects_without_atom(session):
    gap_id = _make_gap(session, term="not-worth-it")

    index = AsyncMock()
    with patch("knowledge.mcp._index_atom", index):
        result = await answer_gap(
            session,
            gap_id,
            "Tombstone - vault convention, not worth a content atom",
        )

    # No atom created on the tombstone branch.
    index.assert_not_awaited()
    assert result["state"] == "rejected"

    gap = session.get(Gap, gap_id)
    assert gap.state == "rejected"
    assert gap.human_verified is True
    assert gap.resolved_at is not None
    assert gap.note_id is None


@pytest.mark.asyncio
async def test_answer_non_in_review_gap_raises(session):
    gap_id = _make_gap(
        session, term="still-discovered", state="discovered", gap_class=None
    )

    index = AsyncMock()
    with patch("knowledge.mcp._index_atom", index):
        with pytest.raises(ValueError, match="expected 'in_review'"):
            await answer_gap(session, gap_id, "some answer")

    index.assert_not_awaited()
    gap = session.get(Gap, gap_id)
    assert gap.state == "discovered"
