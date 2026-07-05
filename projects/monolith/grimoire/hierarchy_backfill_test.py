"""Tests for the grimoire section_hierarchy backfill sync core.

Covers ``grimoire.jobs._apply_hierarchy_updates``: given a
``{chunk_ref: section_hierarchy}`` map and pre-seeded chunks, it must update
section_hierarchy ONLY for matching chunk_refs, leave non-matching rows
untouched, insert nothing, and touch no other column (content/section_path/seq).
Uses the SQLite ``create_all`` fixture (no migrations), mirroring ingest_test.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from grimoire import jobs
from grimoire.models import KnowledgeChunk


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # The models declare a Postgres schema; strip it so SQLite create_all works,
    # then restore (mirrors grimoire/ingest_test.py).
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


def _seed(session: Session, rows: list[dict]) -> None:
    session.add_all(
        KnowledgeChunk(
            book_id=r["book_id"],
            chunk_ref=r["chunk_ref"],
            content=r["content"],
            section_path=r.get("section_path"),
            section_hierarchy=r.get("section_hierarchy"),
            seq=r.get("seq", 0),
        )
        for r in rows
    )
    session.commit()


def _by_ref(session: Session, book_id: str) -> dict[str, KnowledgeChunk]:
    return {
        c.chunk_ref: c
        for c in session.execute(
            select(KnowledgeChunk).where(KnowledgeChunk.book_id == book_id)
        )
        .scalars()
        .all()
    }


def test_updates_only_matching_chunk_refs(session: Session):
    _seed(
        session,
        [
            {"book_id": "phb", "chunk_ref": "a", "content": "A", "seq": 0},
            {"book_id": "phb", "chunk_ref": "b", "content": "B", "seq": 1},
            {"book_id": "phb", "chunk_ref": "c", "content": "C", "seq": 2},
        ],
    )

    # Map covers a and b; c is absent (its boundary shifted) and stays NULL.
    matched, updated = jobs._apply_hierarchy_updates(
        session,
        "phb",
        {"a": "Chapter 1 > Section > A", "b": "Chapter 1 > Section > B"},
    )
    session.commit()

    assert (matched, updated) == (2, 2)
    rows = _by_ref(session, "phb")
    assert rows["a"].section_hierarchy == "Chapter 1 > Section > A"
    assert rows["b"].section_hierarchy == "Chapter 1 > Section > B"
    assert rows["c"].section_hierarchy is None


def test_leaves_other_columns_untouched(session: Session):
    _seed(
        session,
        [
            {
                "book_id": "phb",
                "chunk_ref": "a",
                "content": "original content",
                "section_path": "Chapter 1/A",
                "seq": 7,
            },
        ],
    )

    jobs._apply_hierarchy_updates(session, "phb", {"a": "Chapter 1 > A"})
    session.commit()

    row = _by_ref(session, "phb")["a"]
    assert row.section_hierarchy == "Chapter 1 > A"
    # Only section_hierarchy changed; everything else is exactly as seeded.
    assert row.content == "original content"
    assert row.section_path == "Chapter 1/A"
    assert row.seq == 7


def test_never_inserts_unmatched_refs(session: Session):
    _seed(
        session,
        [{"book_id": "phb", "chunk_ref": "a", "content": "A", "seq": 0}],
    )

    # Map references chunk_refs that do not exist for this book: nothing inserted.
    matched, updated = jobs._apply_hierarchy_updates(
        session, "phb", {"ghost1": "X", "ghost2": "Y"}
    )
    session.commit()

    assert (matched, updated) == (0, 0)
    all_rows = session.execute(select(KnowledgeChunk)).scalars().all()
    assert {r.chunk_ref for r in all_rows} == {"a"}


def test_scopes_to_book_id(session: Session):
    _seed(
        session,
        [
            {"book_id": "phb", "chunk_ref": "shared", "content": "P", "seq": 0},
            {"book_id": "dmg", "chunk_ref": "shared", "content": "D", "seq": 0},
        ],
    )

    jobs._apply_hierarchy_updates(session, "phb", {"shared": "PHB hierarchy"})
    session.commit()

    assert _by_ref(session, "phb")["shared"].section_hierarchy == "PHB hierarchy"
    # A same-named chunk_ref in another book is not touched.
    assert _by_ref(session, "dmg")["shared"].section_hierarchy is None


def test_idempotent_second_run_updates_nothing(session: Session):
    _seed(
        session,
        [{"book_id": "phb", "chunk_ref": "a", "content": "A", "seq": 0}],
    )
    mapping = {"a": "Chapter 1 > A"}

    first_matched, first_updated = jobs._apply_hierarchy_updates(
        session, "phb", mapping
    )
    session.commit()
    second_matched, second_updated = jobs._apply_hierarchy_updates(
        session, "phb", mapping
    )
    session.commit()

    assert (first_matched, first_updated) == (1, 1)
    # Already at the target value (IS DISTINCT FROM): matched again, updated none.
    assert (second_matched, second_updated) == (1, 0)


def test_can_clear_to_null_when_raw_has_no_hierarchy(session: Session):
    _seed(
        session,
        [
            {
                "book_id": "phb",
                "chunk_ref": "a",
                "content": "A",
                "section_hierarchy": "stale value",
                "seq": 0,
            }
        ],
    )

    matched, updated = jobs._apply_hierarchy_updates(session, "phb", {"a": None})
    session.commit()

    assert (matched, updated) == (1, 1)
    assert _by_ref(session, "phb")["a"].section_hierarchy is None


def test_empty_map_is_noop(session: Session):
    _seed(
        session,
        [{"book_id": "phb", "chunk_ref": "a", "content": "A", "seq": 0}],
    )

    assert jobs._apply_hierarchy_updates(session, "phb", {}) == (0, 0)
