"""Tests for knowledge note() CLI command — empty/absent content branch.

knowledge_cmd.py note() at lines 98-101:

    content = data.get("content", "")
    if content:
        path = write_to_tmpfile(note_id, content)
        typer.echo(f"Content: {path}")

When the API returns a note with no content (empty string or key absent),
the ``if content:`` block is skipped — no tmpfile is written and no
"Content: ..." line is printed.  This branch is untested in the existing
knowledge_test.py suite.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from tools.cli.main import app


@pytest.fixture
def runner():
    return CliRunner()


def _make_client_returning(data: dict):
    """Build a client() context-manager replacement that returns *data* for any GET."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = data
    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp

    @contextmanager
    def _ctx():
        yield mock_client

    def _factory():
        return _ctx()

    return _factory


# Minimal note metadata without content.
_NOTE_NO_CONTENT = {
    "note_id": "n1",
    "title": "Attention Is All You Need",
    "path": "papers/attention.md",
    "type": "paper",
    "tags": ["ml"],
    "edges": [],
    # "content" key is absent
}

_NOTE_EMPTY_CONTENT = {
    **_NOTE_NO_CONTENT,
    "content": "",
}


class TestNoteEmptyContentBranch:
    """note() skips tmpfile writing when content is absent or empty."""

    def test_absent_content_does_not_print_content_line(self, runner):
        """When the API response has no 'content' key, 'Content:' is not printed."""
        with patch(
            "tools.cli.api.client",
            _make_client_returning(_NOTE_NO_CONTENT),
        ):
            result = runner.invoke(app, ["knowledge", "note", "n1"])

        assert result.exit_code == 0
        assert "Content:" not in result.output

    def test_absent_content_still_prints_title_and_type(self, runner):
        """Title and type are still printed even when content is absent."""
        with patch(
            "tools.cli.api.client",
            _make_client_returning(_NOTE_NO_CONTENT),
        ):
            result = runner.invoke(app, ["knowledge", "note", "n1"])

        assert result.exit_code == 0
        assert "Attention Is All You Need" in result.output
        assert "paper" in result.output

    def test_empty_string_content_does_not_print_content_line(self, runner):
        """When content is an empty string, 'Content:' is not printed."""
        with patch(
            "tools.cli.api.client",
            _make_client_returning(_NOTE_EMPTY_CONTENT),
        ):
            result = runner.invoke(app, ["knowledge", "note", "n1"])

        assert result.exit_code == 0
        assert "Content:" not in result.output

    def test_empty_string_content_does_not_write_tmpfile(self, runner, tmp_path):
        """No file is written to TMPDIR when content is empty."""
        with (
            patch(
                "tools.cli.api.client",
                _make_client_returning(_NOTE_EMPTY_CONTENT),
            ),
            patch("tools.cli.output.TMPDIR", tmp_path / "notes"),
        ):
            result = runner.invoke(app, ["knowledge", "note", "n1"])

        assert result.exit_code == 0
        # TMPDIR should be empty — write_to_tmpfile was never called.
        notes_dir = tmp_path / "notes"
        if notes_dir.exists():
            assert list(notes_dir.iterdir()) == []

    def test_empty_string_content_exits_zero(self, runner):
        """note() with empty content is not an error — exit code is 0."""
        with patch(
            "tools.cli.api.client",
            _make_client_returning(_NOTE_EMPTY_CONTENT),
        ):
            result = runner.invoke(app, ["knowledge", "note", "n1"])

        assert result.exit_code == 0

    def test_non_empty_content_does_print_content_line(self, runner, tmp_path):
        """Sanity: when content is non-empty, 'Content:' IS printed (no regression)."""
        note_with_content = {
            **_NOTE_NO_CONTENT,
            "content": "# Attention\n\nSelf-attention mechanism.",
        }
        with (
            patch(
                "tools.cli.api.client",
                _make_client_returning(note_with_content),
            ),
            patch("tools.cli.output.TMPDIR", tmp_path / "notes"),
        ):
            result = runner.invoke(app, ["knowledge", "note", "n1"])

        assert result.exit_code == 0
        assert "Content:" in result.output
