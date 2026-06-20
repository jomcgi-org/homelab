"""Unit tests for knowledge.public_models -- PublicNote, PublicNoteLink, PublicChunk.

The models map to public_api Postgres views. In tests we use the
SQLite + schema-strip pattern (same as all other monolith model tests) so
SQLModel.metadata.create_all() materialises them as plain tables that tests
can seed directly. The view derivation and role-level permissions are covered
by the real-Postgres BDD test (knowledge/tests/bdd_public_test.py).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.public_models import PublicChunk, PublicNote, PublicNoteLink


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with all schemas stripped for SQLite compat."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas: dict[str, str] = {}
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


# ---------------------------------------------------------------------------
# PublicNote
# ---------------------------------------------------------------------------


class TestPublicNote:
    def test_instantiate_with_required_fields(self):
        note = PublicNote(
            note_id="n-001",
            title="Test Atom",
            indexed_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            path="atoms/test.md",
        )
        assert note.note_id == "n-001"
        assert note.title == "Test Atom"
        assert note.path == "atoms/test.md"

    def test_optional_fields_default_to_none(self):
        note = PublicNote(
            note_id="n-002",
            title="Fact",
            indexed_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            path="facts/f.md",
        )
        assert note.type is None
        assert note.content is None
        assert note.layout_x is None
        assert note.layout_y is None

    def test_tags_default_to_empty_list(self):
        note = PublicNote(
            note_id="n-003",
            title="No tags",
            indexed_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            path="atoms/none.md",
        )
        assert isinstance(note.tags, list)
        assert len(note.tags) == 0

    def test_aliases_default_to_empty_list(self):
        note = PublicNote(
            note_id="n-004",
            title="No aliases",
            indexed_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            path="atoms/none.md",
        )
        assert isinstance(note.aliases, list)

    def test_persist_and_retrieve(self, session):
        note = PublicNote(
            note_id="n-db-01",
            title="Persisted Note",
            type="atom",
            content="Some body text",
            indexed_at=datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc),
            path="atoms/p.md",
            layout_x=1.5,
            layout_y=2.5,
        )
        session.add(note)
        session.commit()

        retrieved = session.exec(
            select(PublicNote).where(PublicNote.note_id == "n-db-01")
        ).one()
        assert retrieved.title == "Persisted Note"
        assert retrieved.type == "atom"
        assert retrieved.content == "Some body text"
        assert retrieved.layout_x == pytest.approx(1.5)
        assert retrieved.layout_y == pytest.approx(2.5)

    def test_query_by_type(self, session):
        session.add_all(
            [
                PublicNote(
                    note_id="a1",
                    title="Atom",
                    type="atom",
                    indexed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    path="a.md",
                ),
                PublicNote(
                    note_id="g1",
                    title="Gap",
                    type="gap",
                    indexed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    path="g.md",
                ),
            ]
        )
        session.commit()

        atoms = session.exec(select(PublicNote).where(PublicNote.type == "atom")).all()
        assert len(atoms) == 1
        assert atoms[0].note_id == "a1"


# ---------------------------------------------------------------------------
# PublicNoteLink
# ---------------------------------------------------------------------------


class TestPublicNoteLink:
    def test_instantiate_with_required_fields(self):
        link = PublicNoteLink(
            id=1,
            source="n-001",
            target="n-002",
            kind="link",
        )
        assert link.source == "n-001"
        assert link.target == "n-002"
        assert link.kind == "link"
        assert link.edge_type is None

    def test_edge_type_optional(self):
        link = PublicNoteLink(
            id=2,
            source="n-001",
            target="n-003",
            kind="edge",
            edge_type="refines",
        )
        assert link.edge_type == "refines"

    def test_persist_and_retrieve(self, session):
        link = PublicNoteLink(
            id=10,
            source="src-note",
            target="tgt-note",
            kind="link",
        )
        session.add(link)
        session.commit()

        retrieved = session.exec(
            select(PublicNoteLink).where(PublicNoteLink.id == 10)
        ).one()
        assert retrieved.source == "src-note"
        assert retrieved.target == "tgt-note"
        assert retrieved.kind == "link"

    def test_multiple_links_same_source(self, session):
        session.add_all(
            [
                PublicNoteLink(id=20, source="hub", target="spoke-1", kind="link"),
                PublicNoteLink(id=21, source="hub", target="spoke-2", kind="link"),
            ]
        )
        session.commit()

        links = session.exec(
            select(PublicNoteLink).where(PublicNoteLink.source == "hub")
        ).all()
        assert len(links) == 2
        targets = {l.target for l in links}
        assert targets == {"spoke-1", "spoke-2"}


# ---------------------------------------------------------------------------
# PublicChunk (structural checks only -- embedding column needs pgvector in prod)
# ---------------------------------------------------------------------------


class TestPublicChunk:
    def test_instantiate_with_required_fields(self):
        chunk = PublicChunk(
            note_id="n-001",
            chunk_index=0,
            title="Test Note",
            chunk_text="Some chunk content",
        )
        assert chunk.note_id == "n-001"
        assert chunk.chunk_index == 0
        assert chunk.chunk_text == "Some chunk content"

    def test_section_header_defaults_to_empty_string(self):
        chunk = PublicChunk(
            note_id="n-002",
            chunk_index=0,
            title="Test",
            chunk_text="body",
        )
        assert chunk.section_header == ""

    def test_section_header_can_be_set(self):
        chunk = PublicChunk(
            note_id="n-003",
            chunk_index=1,
            title="Test",
            section_header="## Methods",
            chunk_text="body under methods",
        )
        assert chunk.section_header == "## Methods"
