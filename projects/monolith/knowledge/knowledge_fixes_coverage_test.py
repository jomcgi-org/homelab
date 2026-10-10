"""Coverage tests for code paths added by the 13+ knowledge fix commits.

Targets gaps not exercised by existing test files:

store.py
  - upsert_note: note_id fallback lookup when path changes mid-cycle
    (commit 57b1049: handle note_id collision when path changes during upsert)

store.py – raws_needing_decomposition (tiering lifted from the retired
in-pod gardener; the decomposition itself now runs as a remote claude.ai
routine, see ADR 006 Phase 4c)
  - exhausted retries (retry_count >= _MAX_RETRIES) are excluded
  - retriable failures (retry_count < _MAX_RETRIES) are included
  - a successful current-version provenance row wins over a failed row

"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.models import AtomRawProvenance, Note, RawInput


# ---------------------------------------------------------------------------
# Shared SQLite session fixture
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
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


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


# ---------------------------------------------------------------------------
# store.py – raws_needing_decomposition: exhausted retries are excluded
# ---------------------------------------------------------------------------


def test_raws_needing_decomposition_excludes_lane_owned_sources(session):
    from knowledge.store import KnowledgeStore

    raws = [
        RawInput(
            raw_id=f"source-{source}",
            path=f"_raw/source-{source}.md",
            source=source,
            content="body",
            content_hash=f"hash-{source}",
        )
        for source in ("distress", "agent-report", "capture")
    ]
    session.add_all(raws)
    session.commit()

    result = KnowledgeStore(session).raws_needing_decomposition()

    assert [raw.source for raw in result] == ["capture"]


class TestRawsNeedingDecompositionExhaustedRetries:
    """A raw with retry_count >= MAX_GARDENER_RETRIES must NOT appear in
    raws_needing_decomposition() — it belongs in the dead letter queue."""

    def test_exhausted_raw_is_excluded(self, tmp_path, session):
        """Raw with retry_count == _MAX_RETRIES is excluded."""
        from knowledge.gardener import GARDENER_VERSION, MAX_GARDENER_RETRIES
        from knowledge.store import KnowledgeStore

        raw = RawInput(
            raw_id="exhausted-raw",
            path="_raw/2026/04/10/abc1-exhausted.md",
            source="vault-drop",
            content="body",
            content_hash="h1",
        )
        session.add(raw)
        session.commit()
        session.refresh(raw)

        prov = AtomRawProvenance(
            raw_fk=raw.id,
            derived_note_id="failed",
            gardener_version=GARDENER_VERSION,
            error="too many retries",
            retry_count=MAX_GARDENER_RETRIES,
        )
        session.add(prov)
        session.commit()

        result = KnowledgeStore(session).raws_needing_decomposition()

        ids = [r.id for r in result]
        assert raw.id not in ids

    def test_over_limit_raw_is_excluded(self, tmp_path, session):
        """Raw with retry_count > _MAX_RETRIES is also excluded."""
        from knowledge.gardener import GARDENER_VERSION, MAX_GARDENER_RETRIES
        from knowledge.store import KnowledgeStore

        raw = RawInput(
            raw_id="over-limit-raw",
            path="_raw/2026/04/10/abc2-over.md",
            source="vault-drop",
            content="body",
            content_hash="h2",
        )
        session.add(raw)
        session.commit()
        session.refresh(raw)

        prov = AtomRawProvenance(
            raw_fk=raw.id,
            derived_note_id="failed",
            gardener_version=GARDENER_VERSION,
            error="over limit",
            retry_count=MAX_GARDENER_RETRIES + 5,
        )
        session.add(prov)
        session.commit()

        result = KnowledgeStore(session).raws_needing_decomposition()

        ids = [r.id for r in result]
        assert raw.id not in ids

    def test_under_limit_raw_is_included(self, tmp_path, session):
        """Raw with retry_count < _MAX_RETRIES IS included (retriable tier)."""
        from knowledge.gardener import GARDENER_VERSION, MAX_GARDENER_RETRIES
        from knowledge.store import KnowledgeStore

        raw = RawInput(
            raw_id="retriable-raw",
            path="_raw/2026/04/10/abc3-retriable.md",
            source="vault-drop",
            content="body",
            content_hash="h3",
        )
        session.add(raw)
        session.commit()
        session.refresh(raw)

        prov = AtomRawProvenance(
            raw_fk=raw.id,
            derived_note_id="failed",
            gardener_version=GARDENER_VERSION,
            error="transient error",
            retry_count=MAX_GARDENER_RETRIES - 1,
        )
        session.add(prov)
        session.commit()

        result = KnowledgeStore(session).raws_needing_decomposition()

        ids = [r.id for r in result]
        assert raw.id in ids


# ---------------------------------------------------------------------------
# store.py – raws_needing_decomposition: successful provenance wins over failed
# ---------------------------------------------------------------------------


class TestRawsNeedingDecompositionSuccessfulProvenanceWins:
    """When a raw has BOTH a 'failed' provenance row AND a successful
    current-version provenance row, the successful one wins — the raw must
    NOT appear in raws_needing_decomposition()."""

    def test_successful_provenance_excludes_raw_despite_failed_row(
        self, tmp_path, session
    ):
        """Raw with both a 'failed' row and a current-version success row is
        excluded from decomposition (success wins)."""
        from knowledge.gardener import GARDENER_VERSION
        from knowledge.store import KnowledgeStore

        raw = RawInput(
            raw_id="mixed-prov-raw",
            path="_raw/2026/04/10/abc1-mixed.md",
            source="vault-drop",
            content="body",
            content_hash="h1",
        )
        session.add(raw)
        session.commit()
        session.refresh(raw)

        # A failed provenance row — under the retry limit so it would normally
        # be retriable.
        failed_prov = AtomRawProvenance(
            raw_fk=raw.id,
            derived_note_id="failed",
            gardener_version=GARDENER_VERSION,
            error="transient error",
            retry_count=1,
        )
        session.add(failed_prov)

        # A successful current-version provenance row.
        success_prov = AtomRawProvenance(
            raw_fk=raw.id,
            derived_note_id="my-derived-note",
            gardener_version=GARDENER_VERSION,
        )
        session.add(success_prov)
        session.commit()

        result = KnowledgeStore(session).raws_needing_decomposition()

        ids = [r.id for r in result]
        assert raw.id not in ids

    def test_only_failed_row_without_success_is_retriable(self, tmp_path, session):
        """Control: same raw with only a failed row (no success) IS returned
        when retry_count is below the limit."""
        from knowledge.gardener import GARDENER_VERSION
        from knowledge.store import KnowledgeStore

        raw = RawInput(
            raw_id="only-failed-raw",
            path="_raw/2026/04/10/abc2-only-failed.md",
            source="vault-drop",
            content="body",
            content_hash="h2",
        )
        session.add(raw)
        session.commit()
        session.refresh(raw)

        failed_prov = AtomRawProvenance(
            raw_fk=raw.id,
            derived_note_id="failed",
            gardener_version=GARDENER_VERSION,
            error="transient error",
            retry_count=1,
        )
        session.add(failed_prov)
        session.commit()

        result = KnowledgeStore(session).raws_needing_decomposition()

        ids = [r.id for r in result]
        assert raw.id in ids
