"""Unit tests for knowledge.gaps private helpers.

Covers:
  _read_stub_body          — reads first 4 KiB, missing file → None, error swallowed
  _remove_stub_if_present  — removes file, logs action, tolerates missing stub
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.gap_stubs import RESEARCHING_DIR
from knowledge.gaps import _read_stub_body, _remove_stub_if_present
from knowledge.models import Gap


# ---------------------------------------------------------------------------
# Session fixture (same in-memory SQLite pattern as gap_lifecycle_test.py)
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
# Shared helpers
# ---------------------------------------------------------------------------


def _make_gap(
    session: Session,
    *,
    term: str = "test-term",
    state: str = "in_review",
    gap_class: str = "internal",
    note_id: str | None = "test-term",
) -> Gap:
    gap = Gap(
        term=term,
        state=state,
        gap_class=gap_class,
        note_id=note_id,
        pipeline_version="test",
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)
    return gap


def _write_stub(vault_root: Path, slug: str, content: str = "stub body") -> Path:
    stub_dir = vault_root / RESEARCHING_DIR
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / f"{slug}.md"
    stub.write_text(content)
    return stub


# ---------------------------------------------------------------------------
# _read_stub_body
# ---------------------------------------------------------------------------


class TestReadStubBody:
    def test_returns_content_when_stub_exists(
        self, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session)
        _write_stub(tmp_path, "test-term", "stub content here")
        result = _read_stub_body(tmp_path, gap)
        assert result == "stub content here"

    def test_returns_none_when_stub_missing(
        self, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session)
        # No stub written — directory does not even exist.
        result = _read_stub_body(tmp_path, gap)
        assert result is None

    def test_returns_none_when_researching_dir_exists_but_stub_absent(
        self, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session)
        (tmp_path / RESEARCHING_DIR).mkdir()
        result = _read_stub_body(tmp_path, gap)
        assert result is None

    def test_truncates_at_4096_bytes(self, session: Session, tmp_path: Path) -> None:
        gap = _make_gap(session)
        # 5 000 ASCII bytes — must be capped at exactly 4 096.
        _write_stub(tmp_path, "test-term", "x" * 5000)
        result = _read_stub_body(tmp_path, gap)
        assert result is not None
        assert len(result.encode("utf-8")) == 4096

    def test_content_under_4096_bytes_returned_in_full(
        self, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session)
        content = "small stub content"
        _write_stub(tmp_path, "test-term", content)
        result = _read_stub_body(tmp_path, gap)
        assert result == content

    def test_uses_slugified_term_for_stub_path(
        self, session: Session, tmp_path: Path
    ) -> None:
        # "multi word" slugifies to "multi-word"; stub must live at that path.
        gap = _make_gap(session, term="multi-word", note_id="multi-word")
        _write_stub(tmp_path, "multi-word", "content for multi word gap")
        result = _read_stub_body(tmp_path, gap)
        assert result is not None
        assert "content for multi word gap" in result

    def test_oserror_returns_none_and_does_not_raise(
        self, session: Session, tmp_path: Path
    ) -> None:
        """An OSError during stub read is swallowed and returns None."""
        gap = _make_gap(session)
        _write_stub(tmp_path, "test-term", "content")

        with patch.object(Path, "open", side_effect=OSError("simulated read error")):
            result = _read_stub_body(tmp_path, gap)

        assert result is None

    def test_exact_4096_bytes_not_truncated(
        self, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session)
        content = "y" * 4096
        _write_stub(tmp_path, "test-term", content)
        result = _read_stub_body(tmp_path, gap)
        assert result is not None
        assert len(result.encode("utf-8")) == 4096


# ---------------------------------------------------------------------------
# _remove_stub_if_present
# ---------------------------------------------------------------------------


class TestRemoveStubIfPresent:
    def test_removes_stub_file_when_present(
        self, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session)
        stub = _write_stub(tmp_path, "test-term")
        assert stub.is_file()
        _remove_stub_if_present(tmp_path, gap, action="rejected")
        assert not stub.exists()

    def test_tolerates_missing_stub(self, session: Session, tmp_path: Path) -> None:
        gap = _make_gap(session)
        # No stub file — must not raise any exception.
        _remove_stub_if_present(tmp_path, gap, action="rejected")

    def test_tolerates_missing_researching_directory(
        self, session: Session, tmp_path: Path
    ) -> None:
        gap = _make_gap(session)
        # _researching dir never created — must not raise.
        _remove_stub_if_present(tmp_path, gap, action="answer")

    def test_logs_info_when_stub_removed(
        self, session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        gap = _make_gap(session)
        _write_stub(tmp_path, "test-term")
        with caplog.at_level(logging.INFO, logger="knowledge.gaps"):
            _remove_stub_if_present(tmp_path, gap, action="rejected")
        assert any("rejected" in r.message for r in caplog.records)

    def test_log_contains_gap_id(
        self, session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        gap = _make_gap(session)
        _write_stub(tmp_path, "test-term")
        with caplog.at_level(logging.INFO, logger="knowledge.gaps"):
            _remove_stub_if_present(tmp_path, gap, action="answer")
        log_text = " ".join(r.message for r in caplog.records)
        assert str(gap.id) in log_text

    def test_no_log_when_stub_absent(
        self, session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        gap = _make_gap(session)
        # No stub — no info log should be emitted (nothing happened).
        with caplog.at_level(logging.INFO, logger="knowledge.gaps"):
            _remove_stub_if_present(tmp_path, gap, action="rejected")
        assert not any("rejected" in r.message for r in caplog.records)

    def test_uses_slugified_term_for_path(
        self, session: Session, tmp_path: Path
    ) -> None:
        # Confirm the slug derivation: "multi-word-term" stays "multi-word-term".
        gap = _make_gap(session, term="multi-word-term", note_id="multi-word-term")
        stub = _write_stub(tmp_path, "multi-word-term")
        _remove_stub_if_present(tmp_path, gap, action="answer")
        assert not stub.exists()

    def test_only_removes_matching_stub(self, session: Session, tmp_path: Path) -> None:
        """The function must not remove unrelated stubs in the same directory."""
        gap = _make_gap(session, term="target-gap", note_id="target-gap")
        target_stub = _write_stub(tmp_path, "target-gap")
        other_stub = _write_stub(tmp_path, "other-gap")
        _remove_stub_if_present(tmp_path, gap, action="rejected")
        assert not target_stub.exists()
        assert other_stub.exists()
