"""Tests for knowledge.drift_detector."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.drift_detector import (
    DriftCase,
    DriftStats,
    detect_visibility_drift,
)
from knowledge.models import Note


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


def _make_note(
    note_id: str,
    path: str,
    visibility: str | None = None,
) -> Note:
    return Note(
        note_id=note_id,
        path=path,
        title=note_id,
        type="atom",
        content_hash="dummyhash",
        visibility=visibility,
        indexed_at=datetime.now(timezone.utc),
    )


def _write_atom(
    vault_root: Path,
    note_id: str,
    visibility: str | None,
) -> None:
    file_path = vault_root / "_processed" / f"{note_id}.md"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = [f"id: {note_id}", f"title: {note_id}", "type: atom"]
    if visibility is not None:
        fm_lines.append(f"visibility: {visibility}")
    file_path.write_text(
        "---\n" + "\n".join(fm_lines) + "\n---\n\nbody for " + note_id + "\n"
    )


def test_no_drift_when_all_match(session, tmp_path):
    """Both DB and file have the same visibility -> no drift cases."""
    session.add(_make_note("a", "_processed/a.md", "public"))
    session.add(_make_note("b", "_processed/b.md", "private"))
    session.add(_make_note("c", "_processed/c.md", None))
    session.commit()

    _write_atom(tmp_path, "a", "public")
    _write_atom(tmp_path, "b", "private")
    _write_atom(tmp_path, "c", None)

    stats = detect_visibility_drift(session, tmp_path)

    assert stats.checked == 3
    assert stats.drift_count == 0
    assert stats.drift_cases == ()


def test_drift_when_db_null_file_set(session, tmp_path):
    """DB has NULL, file has visibility set -> drift case."""
    session.add(_make_note("a", "_processed/a.md", None))
    session.commit()
    _write_atom(tmp_path, "a", "public")

    stats = detect_visibility_drift(session, tmp_path)

    assert stats.checked == 1
    assert stats.drift_count == 1
    assert len(stats.drift_cases) == 1
    case = stats.drift_cases[0]
    assert case.note_id == "a"
    assert case.db_visibility is None
    assert case.file_visibility == "public"


def test_drift_when_db_set_file_null(session, tmp_path):
    """DB has visibility, file has none -> drift case.

    This is the exact failure mode the MCP edit_note bug produced:
    file gets rewritten without visibility, DB still carries the old
    value until reconciler propagates the null. Detector should catch
    the gap.
    """
    session.add(_make_note("a", "_processed/a.md", "public"))
    session.commit()
    _write_atom(tmp_path, "a", None)

    stats = detect_visibility_drift(session, tmp_path)

    assert stats.drift_count == 1
    case = stats.drift_cases[0]
    assert case.db_visibility == "public"
    assert case.file_visibility is None


def test_drift_when_different_values(session, tmp_path):
    """DB and file disagree on the value -> drift case."""
    session.add(_make_note("a", "_processed/a.md", "public"))
    session.commit()
    _write_atom(tmp_path, "a", "private")

    stats = detect_visibility_drift(session, tmp_path)

    assert stats.drift_count == 1
    case = stats.drift_cases[0]
    assert case.db_visibility == "public"
    assert case.file_visibility == "private"


def test_missing_files_counted(session, tmp_path):
    """Note row exists but the file is gone -> missing_files counter."""
    session.add(_make_note("a", "_processed/ghost.md", "public"))
    session.commit()

    stats = detect_visibility_drift(session, tmp_path)

    assert stats.checked == 0
    assert stats.missing_files == 1
    assert stats.drift_count == 0


def test_parse_failure_counted(session, tmp_path):
    """File present but unparseable frontmatter -> parse_failures counter."""
    session.add(_make_note("a", "_processed/a.md", "public"))
    session.commit()

    bad_file = tmp_path / "_processed" / "a.md"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_text("---\nthis: is: not: valid: yaml: : :\n---\nbody\n")

    stats = detect_visibility_drift(session, tmp_path)

    # Either parses with empty visibility (drift) or fails (parse_failures).
    # Both outcomes are acceptable -- the contract is that we do not
    # crash. Total signal across the two buckets covers the case.
    assert stats.parse_failures + stats.drift_count >= 1


def test_drift_cases_capped_at_sample_limit(session, tmp_path):
    """Even with 200 drift cases the sample tuple stays bounded.

    Protects against memory/log blow-up if every note in the vault drifts
    at once (e.g. a future schema migration sets visibility to NULL on a
    large slice).
    """
    notes = []
    for i in range(150):
        nid = f"n{i:03d}"
        notes.append(_make_note(nid, f"_processed/{nid}.md", "public"))
        _write_atom(tmp_path, nid, "private")
    session.add_all(notes)
    session.commit()

    stats = detect_visibility_drift(session, tmp_path)

    assert stats.drift_count == 150
    assert len(stats.drift_cases) == 100


def test_deleted_notes_skipped(session, tmp_path):
    """Soft-deleted notes are not scanned even if their files still exist."""
    deleted = _make_note("a", "_processed/a.md", "public")
    deleted.deleted_at = datetime.now(timezone.utc)
    session.add(deleted)
    session.commit()
    _write_atom(tmp_path, "a", "private")

    stats = detect_visibility_drift(session, tmp_path)

    assert stats.checked == 0
    assert stats.drift_count == 0
