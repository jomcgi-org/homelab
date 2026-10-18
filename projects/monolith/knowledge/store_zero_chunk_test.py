"""Tests for zero-chunk upsert in knowledge/store.py.

When ``chunks=[]`` and ``vectors=[]`` are passed to ``upsert_note``, the
``zip(chunks, vectors, strict=True)`` loop does not execute.  This is a valid
input (a note that produces no text chunks — e.g. an empty body after
frontmatter is stripped).  The Note row must still be created; no Chunk rows
should appear.

This path was identified as untested in the coverage review.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.frontmatter import ParsedFrontmatter
from knowledge.models import Chunk, Note, NoteLink
from knowledge.store import KnowledgeStore


# ---------------------------------------------------------------------------
# Fixtures (mirrors the pattern in store_extra_test.py)
# ---------------------------------------------------------------------------


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
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


@pytest.fixture
def store(session):
    return KnowledgeStore(session=session)


def _meta(**kw) -> ParsedFrontmatter:
    return ParsedFrontmatter(**kw)


# ---------------------------------------------------------------------------
# Zero-chunk upsert
# ---------------------------------------------------------------------------


class TestUpsertNoteZeroChunks:
    """Verify that upsert_note with empty chunks/vectors creates a Note row
    but no Chunk rows."""

    def test_note_row_is_created_with_zero_chunks(self, store, session):
        """A Note row exists after upsert with empty chunks/vectors."""
        store.upsert_note(
            note_id="empty-note",
            path="empty.md",
            content_hash="hh1",
            title="Empty Note",
            metadata=_meta(title="Empty Note"),
            chunks=[],
            vectors=[],
            links=[],
        )

        notes = list(session.scalars(select(Note)))
        assert len(notes) == 1
        assert notes[0].note_id == "empty-note"
        assert notes[0].path == "empty.md"
        assert notes[0].title == "Empty Note"

    def test_no_chunk_rows_created_with_zero_chunks(self, store, session):
        """No Chunk rows are persisted when chunks=[] is passed."""
        store.upsert_note(
            note_id="empty-note-2",
            path="empty2.md",
            content_hash="hh2",
            title="Empty Note 2",
            metadata=_meta(title="Empty Note 2"),
            chunks=[],
            vectors=[],
            links=[],
        )

        chunks = list(session.scalars(select(Chunk)))
        assert chunks == []

    def test_zero_chunks_with_links_still_creates_note_links(self, store, session):
        """Even with zero chunks, NoteLink rows are still inserted for wikilinks."""
        from knowledge.links import Link

        store.upsert_note(
            note_id="linked-empty",
            path="linked-empty.md",
            content_hash="hh3",
            title="Linked Empty",
            metadata=_meta(title="Linked Empty"),
            chunks=[],
            vectors=[],
            links=[Link(target="other-note", display="Other Note")],
        )

        note = session.exec(select(Note).where(Note.path == "linked-empty.md")).first()
        assert note is not None

        links = list(
            session.scalars(select(NoteLink).where(NoteLink.src_note_fk == note.id))
        )
        assert len(links) == 1
        assert links[0].target_id == "other-note"
        assert links[0].kind == "link"

    def test_reupsert_with_zero_chunks_replaces_existing_chunks(self, store, session):
        """Re-upserting a note with zero chunks removes previously stored Chunk rows."""
        # First upsert: 2 chunks
        store.upsert_note(
            note_id="note-x",
            path="note_x.md",
            content_hash="old-hash",
            title="Note X",
            metadata=_meta(title="Note X"),
            chunks=[
                {"index": 0, "section_header": "", "text": "chunk 0"},
                {"index": 1, "section_header": "", "text": "chunk 1"},
            ],
            vectors=[[0.1] * 1024, [0.2] * 1024],
            links=[],
        )

        assert len(list(session.scalars(select(Chunk)))) == 2

        # Second upsert: zero chunks
        store.upsert_note(
            note_id="note-x",
            path="note_x.md",
            content_hash="new-hash",
            title="Note X Updated",
            metadata=_meta(title="Note X Updated"),
            chunks=[],
            vectors=[],
            links=[],
        )

        # All old Chunk rows must have been deleted by the cascade
        assert list(session.scalars(select(Chunk))) == []

        # Note row updated
        note = session.exec(select(Note).where(Note.path == "note_x.md")).first()
        assert note is not None
        assert note.content_hash == "new-hash"
