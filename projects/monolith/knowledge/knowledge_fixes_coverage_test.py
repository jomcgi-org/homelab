"""Coverage tests for code paths added by the 13+ knowledge fix commits.

Targets gaps not exercised by existing test files:

store.py
  - upsert_note: note_id fallback lookup when path changes mid-cycle
    (commit 57b1049: handle note_id collision when path changes during upsert)

"""

from __future__ import annotations

import pytest
from sqlmodel import select

from knowledge.models import Note
from shared.testing.sqlite import sqlite_session


# ---------------------------------------------------------------------------
# Shared SQLite session fixture
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    with sqlite_session() as session:
        yield session


# ---------------------------------------------------------------------------
# store.py – upsert_note: note_id collision when path changes
# (commit 57b1049: handle note_id collision when path changes during upsert)
# ---------------------------------------------------------------------------


class TestUpsertNoteNoteIdFallback:
    """When a note's path changes between runs, upsert_note must find the
    existing row by note_id (the stable identity) rather than by path.

    Without the fix the path lookup returns None AND the note_id lookup also
    returns None — the INSERT fails with a UNIQUE violation on note_id.
    The fix adds a second lookup by note_id when the path lookup misses.
    """

    def test_upsert_replaces_note_when_path_changes(self, session):
        """Re-upserting the same note_id at a new path replaces the old row."""
        from knowledge.frontmatter import ParsedFrontmatter
        from knowledge.store import KnowledgeStore

        store = KnowledgeStore(session=session)
        meta = ParsedFrontmatter()
        chunks = [{"index": 0, "section_header": "", "text": "Body."}]
        vectors = [[0.1] * 1024]

        # First upsert at the original path.
        store.upsert_note(
            note_id="stable-id",
            path="_processed/old-path.md",
            content_hash="h1",
            title="Old Title",
            metadata=meta,
            chunks=chunks,
            vectors=vectors,
            links=[],
        )

        old_notes = session.exec(select(Note)).all()
        assert len(old_notes) == 1

        # Re-upsert the same note_id at a different path (e.g. gardener moved
        # the file from the vault root into _processed/).
        store.upsert_note(
            note_id="stable-id",
            path="_processed/new-path.md",
            content_hash="h2",
            title="New Title",
            metadata=meta,
            chunks=chunks,
            vectors=vectors,
            links=[],
        )

        notes = session.exec(select(Note)).all()
        # Exactly one row – the old one was replaced, not duplicated.
        assert len(notes) == 1
        assert notes[0].note_id == "stable-id"
        assert notes[0].path == "_processed/new-path.md"
        assert notes[0].title == "New Title"
        assert notes[0].content_hash == "h2"

    def test_upsert_clears_old_chunks_on_path_change(self, session):
        """Chunks from the old path are deleted when the note is re-upserted at
        a new path (cascade delete via note_id fallback lookup)."""
        from knowledge.models import Chunk
        from knowledge.frontmatter import ParsedFrontmatter
        from knowledge.store import KnowledgeStore

        store = KnowledgeStore(session=session)
        meta = ParsedFrontmatter()

        store.upsert_note(
            note_id="chunk-test",
            path="_processed/orig.md",
            content_hash="h1",
            title="T",
            metadata=meta,
            chunks=[
                {"index": 0, "section_header": "S0", "text": "chunk0"},
                {"index": 1, "section_header": "S1", "text": "chunk1"},
            ],
            vectors=[[0.1] * 1024, [0.2] * 1024],
            links=[],
        )

        # Confirm two chunks were stored.
        assert len(session.exec(select(Chunk)).all()) == 2

        # Re-upsert at a new path with a single chunk.
        store.upsert_note(
            note_id="chunk-test",
            path="_processed/moved.md",
            content_hash="h2",
            title="T2",
            metadata=meta,
            chunks=[{"index": 0, "section_header": "", "text": "new chunk"}],
            vectors=[[0.3] * 1024],
            links=[],
        )

        # Old chunks deleted; only the new one remains.
        chunks = session.exec(select(Chunk)).all()
        assert len(chunks) == 1
        assert chunks[0].chunk_text == "new chunk"
