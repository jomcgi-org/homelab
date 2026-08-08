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

migrate_raw_bucketing.py
  - _strip_frontmatter_keys: bad YAML returns original content
  - _strip_frontmatter_keys: non-dict YAML returns original content
  - _strip_frontmatter_keys: no frontmatter returns content unchanged
  - _strip_frontmatter_keys: stripping all keys returns just the body
  - _grandfather_raws: bad frontmatter logs warning and uses defaults
  - _grandfather_atoms: returns 0 when no atom notes in DB
  - _grandfather_atoms: inserts pre-migration sentinel for each atom
  - _grandfather_atoms: idempotent — skips atoms that already have a sentinel
  - _grandfather_atoms: handles fact and active note types
"""

from __future__ import annotations

import logging
from pathlib import Path

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


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


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
# migrate_raw_bucketing.py – _strip_frontmatter_keys: edge cases
# ---------------------------------------------------------------------------


class TestStripFrontmatterKeys:
    """Edge cases for _strip_frontmatter_keys in the migration helper."""

    def test_no_frontmatter_returns_content_unchanged(self):
        from knowledge.migrate_raw_bucketing import _strip_frontmatter_keys

        content = "Just plain body.\nNo frontmatter."
        result = _strip_frontmatter_keys(content, {"ttl"})
        assert result == content

    def test_unclosed_frontmatter_returns_content_unchanged(self):
        from knowledge.migrate_raw_bucketing import _strip_frontmatter_keys

        content = "---\ntitle: Test\n"  # no closing ---
        result = _strip_frontmatter_keys(content, {"ttl"})
        assert result == content

    def test_bad_yaml_returns_content_unchanged(self):
        from knowledge.migrate_raw_bucketing import _strip_frontmatter_keys

        content = "---\n: invalid: yaml:\n---\nBody."
        result = _strip_frontmatter_keys(content, {"ttl"})
        assert result == content

    def test_non_dict_yaml_returns_content_unchanged(self):
        from knowledge.migrate_raw_bucketing import _strip_frontmatter_keys

        content = "---\n- item1\n- item2\n---\nBody."
        result = _strip_frontmatter_keys(content, {"ttl"})
        assert result == content

    def test_stripping_all_keys_returns_body_only(self):
        """When all frontmatter keys are stripped, only the body is returned."""
        from knowledge.migrate_raw_bucketing import _strip_frontmatter_keys

        content = "---\nttl: 2026-01-01\n---\nBody text here.\n"
        result = _strip_frontmatter_keys(content, {"ttl"})
        # All keys stripped → meta dict is empty → body returned.
        assert result == "Body text here.\n"
        assert "ttl" not in result
        assert "---" not in result

    def test_preserves_non_stripped_keys(self):
        """Keys not in the strip-set are preserved in the output."""
        from knowledge.migrate_raw_bucketing import _strip_frontmatter_keys

        content = "---\ntitle: Keep Me\nttl: remove\n---\nBody.\n"
        result = _strip_frontmatter_keys(content, {"ttl"})
        assert "title: Keep Me" in result
        assert "ttl" not in result

    def test_stripping_nonexistent_key_is_a_noop(self):
        """Attempting to strip a key that doesn't exist in the frontmatter
        leaves the content structurally equivalent."""
        from knowledge.migrate_raw_bucketing import _strip_frontmatter_keys

        content = "---\ntitle: Test\n---\nBody.\n"
        result = _strip_frontmatter_keys(content, {"nonexistent-key"})
        assert "title: Test" in result
        assert "nonexistent-key" not in result


# ---------------------------------------------------------------------------
# migrate_raw_bucketing.py – _grandfather_raws: bad frontmatter warning
# ---------------------------------------------------------------------------


class TestGrandfatherRawsBadFrontmatter:
    """When _grandfather_raws encounters a file with unparseable frontmatter,
    it logs a warning and falls back to using the stem as the title."""

    def test_bad_frontmatter_logs_warning_and_uses_stem_as_title(
        self, tmp_path, session, caplog
    ):
        from knowledge.migrate_raw_bucketing import _grandfather_raws

        bad = tmp_path / "_deleted_with_ttl" / "inbox" / "bad-fm.md"
        # Frontmatter that will cause a parse error: unterminated list.
        _write(bad, "---\ntitle: [unterminated\n---\nBody text.")

        with caplog.at_level(
            logging.WARNING, logger="monolith.knowledge.migrate_raw_bucketing"
        ):
            count = _grandfather_raws(vault_root=tmp_path, session=session)
        session.commit()

        # The file was still processed (fallen back to stem for title).
        assert count == 1
        # Warning was logged.
        assert any("bad frontmatter" in r.message for r in caplog.records)
        # The raw_input should exist with the stem as title.
        rows = session.exec(select(RawInput)).all()
        assert len(rows) == 1
        assert rows[0].source == "grandfathered"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# migrate_raw_bucketing.py – _grandfather_atoms
# ---------------------------------------------------------------------------


class TestGrandfatherAtoms:
    """_grandfather_atoms creates pre-migration sentinel provenance rows for
    atom, fact, and active notes that do not already have one."""

    def test_returns_zero_when_no_atoms_in_db(self, session):
        """Returns 0 when the notes table has no atom/fact/active rows."""
        from knowledge.migrate_raw_bucketing import _grandfather_atoms

        result = _grandfather_atoms(session)
        assert result == 0

    def test_inserts_sentinel_for_each_atom_note(self, session):
        """Creates one pre-migration provenance row per atom note."""
        from knowledge.migrate_raw_bucketing import _grandfather_atoms

        for i in range(3):
            note = Note(
                note_id=f"atom-{i}",
                path=f"_processed/atom-{i}.md",
                title=f"Atom {i}",
                content_hash=f"h{i}",
                type="atom",
            )
            session.add(note)
        session.commit()

        result = _grandfather_atoms(session)
        assert result == 3

        sentinels = session.exec(
            select(AtomRawProvenance).where(
                AtomRawProvenance.gardener_version == "pre-migration"
            )
        ).all()
        assert len(sentinels) == 3
        # All sentinels have atom_fk set and raw_fk unset
        for s in sentinels:
            assert s.atom_fk is not None
            assert s.raw_fk is None

    def test_is_idempotent_for_atoms_with_existing_sentinel(self, session):
        """Atoms that already have a pre-migration sentinel are not re-processed."""
        from knowledge.migrate_raw_bucketing import _grandfather_atoms

        note = Note(
            note_id="already-done",
            path="_processed/already-done.md",
            title="Already Done",
            content_hash="h1",
            type="atom",
        )
        session.add(note)
        session.commit()

        # First call inserts the sentinel
        count1 = _grandfather_atoms(session)
        assert count1 == 1

        # Second call finds the existing sentinel and skips
        count2 = _grandfather_atoms(session)
        assert count2 == 0

        # Only one sentinel exists
        sentinels = session.exec(
            select(AtomRawProvenance).where(
                AtomRawProvenance.atom_fk == note.id,
                AtomRawProvenance.gardener_version == "pre-migration",
            )
        ).all()
        assert len(sentinels) == 1

    def test_handles_fact_and_active_note_types(self, session):
        """fact and active note types are included alongside atom notes."""
        from knowledge.migrate_raw_bucketing import _grandfather_atoms

        for note_type in ("fact", "active"):
            note = Note(
                note_id=f"typed-{note_type}",
                path=f"_processed/{note_type}.md",
                title=f"Typed {note_type}",
                content_hash=f"h-{note_type}",
                type=note_type,
            )
            session.add(note)
        session.commit()

        result = _grandfather_atoms(session)
        assert result == 2

        sentinels = session.exec(
            select(AtomRawProvenance).where(
                AtomRawProvenance.gardener_version == "pre-migration"
            )
        ).all()
        assert len(sentinels) == 2

    def test_ignores_raw_and_other_note_types(self, session):
        """Notes with type 'raw' or other non-atom types are not grandfathered."""
        from knowledge.migrate_raw_bucketing import _grandfather_atoms

        for note_type in ("raw", "note"):
            note = Note(
                note_id=f"skip-{note_type}",
                path=f"_processed/skip-{note_type}.md",
                title=f"Skip {note_type}",
                content_hash=f"h-skip-{note_type}",
                type=note_type,
            )
            session.add(note)
        session.commit()

        result = _grandfather_atoms(session)
        assert result == 0


# ---------------------------------------------------------------------------
# store.py – raws_needing_decomposition: exhausted retries are excluded
# ---------------------------------------------------------------------------


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
