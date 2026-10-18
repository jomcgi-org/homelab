"""Extra coverage tests for KnowledgeStore — error paths and edge cases."""

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.frontmatter import ParsedFrontmatter
from knowledge.links import Link
from knowledge.models import Chunk, Note, NoteLink
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
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


@pytest.fixture
def store(session):
    return KnowledgeStore(session=session)


def _meta(**kw):
    return ParsedFrontmatter(**kw)


def _chunks(n):
    return [
        {"index": i, "section_header": f"H{i}", "text": f"chunk {i}"} for i in range(n)
    ]


def _vecs(n):
    return [[float(i)] * 1024 for i in range(n)]


def _upsert(
    store,
    *,
    note_id="a-id",
    path="a.md",
    content_hash="h1",
    title="A",
    metadata=None,
    n_chunks=1,
    links=None,
):
    metadata = metadata or _meta(title=title)
    store.upsert_note(
        note_id=note_id,
        path=path,
        content_hash=content_hash,
        title=title,
        metadata=metadata,
        chunks=_chunks(n_chunks),
        vectors=_vecs(n_chunks),
        links=links or [],
    )


# ---------------------------------------------------------------------------
# delete_note — absent path
# ---------------------------------------------------------------------------


class TestDeleteNoteExtra:
    def test_delete_absent_path_is_noop(self, store, session):
        """delete_note on a path that doesn't exist logs info but does not raise."""
        # DB is empty — no note at "does-not-exist.md"
        store.delete_note("does-not-exist.md")

        # No error should have been raised; DB is still empty
        assert list(session.scalars(select(Note))) == []

    def test_delete_absent_path_logs_info(self, store, caplog):
        """delete_note for absent path emits an info-level log message."""
        import logging

        with caplog.at_level(logging.INFO, logger="knowledge.store"):
            store.delete_note("ghost.md")

        assert any("ghost.md" in r.message for r in caplog.records)

    def test_delete_note_only_removes_target(self, store, session):
        """delete_note removes only the targeted note, leaving siblings intact."""
        _upsert(store, note_id="a", path="a.md", content_hash="h1", title="A")
        _upsert(store, note_id="b", path="b.md", content_hash="h2", title="B")

        store.delete_note("a.md")

        remaining = list(session.scalars(select(Note)))
        assert len(remaining) == 1
        assert remaining[0].path == "b.md"


# ---------------------------------------------------------------------------
# upsert_note — zip mismatch (strict=True)
# ---------------------------------------------------------------------------


class TestUpsertZipMismatch:
    def test_more_chunks_than_vectors_raises_value_error(self, store, session):
        """zip(chunks, vectors, strict=True) raises ValueError if lengths differ.

        chunks=2, vectors=1 → ValueError is raised during chunk insertion.
        """
        with pytest.raises(ValueError):
            store.upsert_note(
                note_id="x",
                path="x.md",
                content_hash="hx",
                title="X",
                metadata=_meta(title="X"),
                chunks=_chunks(2),
                vectors=_vecs(1),  # shorter than chunks
                links=[],
            )

    def test_more_vectors_than_chunks_raises_value_error(self, store, session):
        """zip(chunks, vectors, strict=True) raises ValueError if vectors exceed chunks."""
        with pytest.raises(ValueError):
            store.upsert_note(
                note_id="y",
                path="y.md",
                content_hash="hy",
                title="Y",
                metadata=_meta(title="Y"),
                chunks=_chunks(1),
                vectors=_vecs(3),  # longer than chunks
                links=[],
            )

    def test_zip_mismatch_on_reupsert_preserves_original(self, store, session):
        """If a re-upsert fails due to zip mismatch, the original note is preserved.

        The SAVEPOINT in upsert_note should roll back the cascaded deletes and
        failed insert, leaving the original row intact.
        """
        _upsert(
            store,
            path="a.md",
            content_hash="h1",
            title="Original",
            metadata=_meta(title="Original"),
            n_chunks=1,
        )

        with pytest.raises(ValueError):
            store.upsert_note(
                note_id="a-id",
                path="a.md",
                content_hash="h2",
                title="Replaced",
                metadata=_meta(title="Replaced"),
                chunks=_chunks(2),
                vectors=_vecs(1),  # mismatch — will fail
                links=[],
            )

        session.rollback()

        notes = list(session.scalars(select(Note)))
        assert len(notes) == 1
        assert notes[0].title == "Original"
        assert notes[0].content_hash == "h1"
