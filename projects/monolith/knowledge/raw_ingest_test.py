"""Tests for raw ingest Phase A (move) and Phase B (reconcile)."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.models import Note, RawInput
from knowledge.raw_ingest import (
    ReconcileRawStats,
    move_phase,
    reconcile_raw_phase,
)


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class TestMovePhase:
    def test_moves_vault_root_md_into_raw_tree(self, tmp_path):
        _write(tmp_path / "inbox" / "note.md", "---\ntitle: Note\n---\nBody.")
        now = datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc)
        stats = move_phase(vault_root=tmp_path, now=now)
        assert stats.moved == 1
        assert stats.deduped == 0
        assert not (tmp_path / "inbox" / "note.md").exists()
        date_dir = tmp_path / "_raw" / "2026" / "04" / "09"
        targets = list(date_dir.glob("*.md"))
        assert len(targets) == 1
        assert targets[0].read_text(encoding="utf-8").startswith("---")

    def test_skips_files_already_under_managed_dirs(self, tmp_path):
        _write(tmp_path / "_raw" / "2026" / "04" / "09" / "abc-note.md", "x")
        _write(tmp_path / "_processed" / "atoms" / "a.md", "y")
        stats = move_phase(
            vault_root=tmp_path,
            now=datetime(2026, 4, 9, tzinfo=timezone.utc),
        )
        assert stats.moved == 0

    def test_dedup_deletes_source_when_target_exists(self, tmp_path):
        content = "---\ntitle: Dup\n---\nSame body."
        _write(tmp_path / "inbox" / "a.md", content)
        _write(tmp_path / "inbox" / "b.md", content)  # identical content -> same raw_id
        now = datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc)
        stats = move_phase(vault_root=tmp_path, now=now)
        # First gets moved, second is deduped (source deleted).
        assert stats.moved == 1
        assert stats.deduped == 1
        remaining = list((tmp_path / "inbox").glob("*.md"))
        assert remaining == []

    def test_ignores_dotfiles_and_dot_dirs(self, tmp_path):
        _write(tmp_path / ".obsidian" / "config.md", "x")
        _write(tmp_path / "inbox" / ".hidden.md", "y")
        _write(tmp_path / "inbox" / "visible.md", "---\ntitle: V\n---\nB")
        stats = move_phase(
            vault_root=tmp_path,
            now=datetime(2026, 4, 9, tzinfo=timezone.utc),
        )
        assert stats.moved == 1


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


class TestReconcileRawPhase:
    def test_inserts_raw_input_and_mirror_note_row(self, tmp_path, session):
        raw_file = tmp_path / "_raw" / "2026" / "04" / "09" / "abc1-my-note.md"
        raw_file.parent.mkdir(parents=True)
        raw_file.write_text(
            "---\ntitle: My Note\nsource: vault-drop\n---\nBody.",
            encoding="utf-8",
        )

        stats = reconcile_raw_phase(vault_root=tmp_path, session=session)
        session.commit()

        assert stats.inserted == 1
        assert stats.skipped == 0

        rows = session.exec(select(RawInput)).all()
        assert len(rows) == 1
        assert rows[0].path == "_raw/2026/04/09/abc1-my-note.md"
        assert rows[0].source == "vault-drop"
        assert rows[0].created_at is not None

        notes = session.exec(
            select(Note).where(Note.type == "raw", Note.deleted_at.is_(None))
        ).all()
        assert len(notes) == 1
        assert notes[0].note_id == rows[0].raw_id

    def test_is_idempotent(self, tmp_path, session):
        raw_file = tmp_path / "_raw" / "2026" / "04" / "09" / "abc1-my-note.md"
        raw_file.parent.mkdir(parents=True)
        raw_file.write_text("---\ntitle: N\n---\nBody.", encoding="utf-8")

        reconcile_raw_phase(vault_root=tmp_path, session=session)
        session.commit()
        stats = reconcile_raw_phase(vault_root=tmp_path, session=session)
        session.commit()

        assert stats.inserted == 0
        assert stats.skipped == 1
        assert len(session.exec(select(RawInput)).all()) == 1

    def test_inserts_raw_input_when_note_already_exists(self, tmp_path, session):
        """RawInput must be recorded even if a Note with the same note_id
        already exists (e.g. from the decomposition pipeline)."""
        content = "---\ntitle: Pre-existing\n---\nBody."
        raw_file = tmp_path / "_raw" / "2026" / "04" / "09" / "abc1-pre.md"
        raw_file.parent.mkdir(parents=True)
        raw_file.write_text(content, encoding="utf-8")

        from knowledge.raw_paths import compute_raw_id

        note_id = compute_raw_id(content)
        existing_note = Note(
            note_id=note_id,
            path="_processed/atoms/pre.md",
            title="Pre-existing",
            content_hash=note_id,
            type="atom",
            source="decomposition",
            indexed_at=datetime.now(timezone.utc),
        )
        session.add(existing_note)
        session.commit()

        stats = reconcile_raw_phase(vault_root=tmp_path, session=session)
        session.commit()

        assert stats.inserted == 1
        rows = session.exec(select(RawInput)).all()
        assert len(rows) == 1
        assert rows[0].raw_id == note_id

    def test_missing_raw_dir_is_noop(self, tmp_path, session):
        stats = reconcile_raw_phase(vault_root=tmp_path, session=session)
        assert stats.inserted == 0
        assert stats.skipped == 0

    def test_infer_source_grandfathered(self, tmp_path, session):
        """Files under _raw/grandfathered/ get source='grandfathered' when
        no frontmatter source is present."""
        raw_file = tmp_path / "_raw" / "grandfathered" / "abcd1234-old-note.md"
        raw_file.parent.mkdir(parents=True)
        raw_file.write_text(
            "---\ntitle: Old Note\n---\nBody.",
            encoding="utf-8",
        )

        stats = reconcile_raw_phase(vault_root=tmp_path, session=session)
        session.commit()

        assert stats.inserted == 1
        rows = session.exec(select(RawInput)).all()
        assert len(rows) == 1
        assert rows[0].source == "grandfathered"

    def test_raw_input_insert_failure_on_duplicate_raw_id(self, tmp_path, session):
        """When a RawInput with the same raw_id already exists (different path),
        the DB insert fails gracefully and inserted==0."""
        content = "---\ntitle: Collision\n---\nBody."
        raw_file = tmp_path / "_raw" / "2026" / "04" / "09" / "abc1-collision.md"
        raw_file.parent.mkdir(parents=True)
        raw_file.write_text(content, encoding="utf-8")

        from knowledge.raw_paths import compute_raw_id

        raw_id = compute_raw_id(content)
        # Pre-insert a RawInput that shares raw_id but has a different path
        # so the path-based idempotency check does NOT skip it, but the DB
        # unique constraint on raw_id causes the insert to fail.
        existing = RawInput(
            raw_id=raw_id,
            path="_raw/2026/04/08/abc1-other.md",
            source="vault-drop",
            content=content,
            content_hash=raw_id,
            created_at=datetime.now(timezone.utc),
        )
        session.add(existing)
        session.commit()

        stats = reconcile_raw_phase(vault_root=tmp_path, session=session)
        session.commit()

        assert stats.inserted == 0
        # Only the original pre-inserted row should exist.
        rows = session.exec(select(RawInput)).all()
        assert len(rows) == 1
        assert rows[0].path == "_raw/2026/04/08/abc1-other.md"


def test_infer_source_research_subdir_returns_research():
    """A raw under _inbox/research/ is sourced as 'research'."""
    from knowledge.raw_ingest import _infer_source

    assert _infer_source(None, ("_inbox", "research", "merkle-tree.md")) == "research"


def test_infer_source_research_does_not_override_explicit_meta_source():
    """Explicit frontmatter meta_source still wins over directory inference."""
    from knowledge.raw_ingest import _infer_source

    assert _infer_source("manual", ("_inbox", "research", "x.md")) == "manual"


def test_excluded_top_level_contains_discord_to_match_vault_export_intent():
    """Drift detector: _discord/ must be excluded from move_phase.

    chat/vault_export.py writes user + channel summaries to
    _discord/<channel_id>/... with an explicit "NOT for KG ingest"
    docstring. If _discord/ is removed from _EXCLUDED_TOP_LEVEL,
    move_phase will atomically rename those files into _raw/ and the
    gardener will turn each one into a permanent dated atom — exactly
    the flood the exclusion was added to stop (2026-05-28 audit
    deleted ~258 atoms produced this way).
    """
    from knowledge.raw_ingest import _EXCLUDED_TOP_LEVEL

    assert "_discord" in _EXCLUDED_TOP_LEVEL, (
        "_discord/ must be excluded from move_phase to match "
        "chat/vault_export.py's intent (see module docstring there)"
    )
