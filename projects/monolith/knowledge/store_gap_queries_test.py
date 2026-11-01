"""Tests for KnowledgeStore.list_gaps()."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.models import Gap, Note
from knowledge.store import KnowledgeStore


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


def _make_note(session: Session, note_id: str = "n") -> Note:
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


def _add_gap(
    session: Session,
    *,
    term: str,
    state: str = "discovered",
    gap_class: str | None = None,
    created_at: datetime | None = None,
) -> Gap:
    gap = Gap(
        term=term,
        context="",
        gap_class=gap_class,
        state=state,
        pipeline_version="gaps@v1",
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)
    return gap


def test_list_gaps_returns_most_recent_first(session):
    _make_note(session)
    now = datetime.now(timezone.utc)
    _add_gap(session, term="old", created_at=now - timedelta(hours=1))
    _add_gap(session, term="new", created_at=now)

    store = KnowledgeStore(session)
    rows = store.list_gaps()

    assert [r["term"] for r in rows] == ["new", "old"]
    assert rows[0]["pipeline_version"] == "gaps@v1"


def test_list_gaps_filters_by_state(session):
    _make_note(session)
    _add_gap(session, term="a", state="discovered")
    _add_gap(
        session,
        term="b",
        state="in_review",
        gap_class="internal",
    )
    _add_gap(
        session,
        term="c",
        state="committed",
        gap_class="internal",
    )

    store = KnowledgeStore(session)
    rows = store.list_gaps(states=["in_review", "committed"])

    assert sorted(r["term"] for r in rows) == ["b", "c"]


def test_list_gaps_filters_by_class(session):
    _make_note(session)
    _add_gap(
        session,
        term="int",
        state="in_review",
        gap_class="internal",
    )
    _add_gap(
        session,
        term="ext",
        state="classified",
        gap_class="external",
    )
    _add_gap(
        session,
        term="park",
        state="classified",
        gap_class="parked",
    )

    store = KnowledgeStore(session)
    rows = store.list_gaps(classes=["internal", "external"])

    assert sorted(r["term"] for r in rows) == ["ext", "int"]


def test_list_gaps_respects_limit(session):
    _make_note(session)
    now = datetime.now(timezone.utc)
    for i in range(5):
        _add_gap(
            session,
            term=f"g-{i}",
            created_at=now + timedelta(seconds=i),
        )

    store = KnowledgeStore(session)
    rows = store.list_gaps(limit=3)

    assert len(rows) == 3
    # Most-recent first.
    assert [r["term"] for r in rows] == ["g-4", "g-3", "g-2"]
