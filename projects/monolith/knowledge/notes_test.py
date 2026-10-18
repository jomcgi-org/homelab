"""Unit tests for knowledge.notes helper functions.

DB-only after ADR 006 (Obsidian decommissioned): note bodies live in the
Postgres ``content`` column, there is no ``/vault`` on disk, and the
trash-file / frontmatter-rewrite helpers are gone. Covers:

  _read_note_snippet     — line/byte cap, missing content
  _note_to_review_dict   — serialization shape and snippet inclusion
  resolve_note_body      — passthrough of the Postgres ``content`` body
"""

from __future__ import annotations

from datetime import datetime, timezone

from knowledge.models import Note
from knowledge.notes import (
    _note_to_review_dict,
    _read_note_snippet,
    resolve_note_body,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_note(
    *,
    note_id: str = "test-note",
    path: str | None = None,
    title: str = "Test Note",
    content: str | None = None,
    visibility: str | None = None,
    visibility_verified: bool = False,
    tags: list[str] | None = None,
    type_: str | None = "atom",
    source: str | None = None,
    deleted_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> Note:
    """Return an *unsaved* Note for testing pure helpers that don't need a DB."""
    return Note(
        note_id=note_id,
        path=path or f"{note_id}.md",
        title=title,
        content=content,
        content_hash=f"hash-{note_id}",
        type=type_,
        visibility=visibility,  # type: ignore[arg-type]
        visibility_verified=visibility_verified,
        tags=tags or [],
        source=source,
        deleted_at=deleted_at,
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# _read_note_snippet — reads the authoritative Postgres ``content`` column.
# ---------------------------------------------------------------------------


class TestReadNoteSnippet:
    def test_returns_body_from_content(self) -> None:
        note = _make_note(content="This is the body.")
        snippet = _read_note_snippet(note)
        assert "This is the body." in snippet

    def test_line_cap_returns_at_most_100_lines(self) -> None:
        body = "\n".join(f"line {i}" for i in range(200))
        note = _make_note(content=body)
        snippet = _read_note_snippet(note)
        lines = snippet.splitlines()
        assert len(lines) == 100
        assert lines[0] == "line 0"
        assert lines[99] == "line 99"
        assert "line 100" not in snippet

    def test_byte_cap_truncates_to_8192_bytes(self) -> None:
        # Each "line" is 100 ASCII chars; 100 lines ≈ 10 100 bytes > 8 192 cap.
        body = "\n".join("x" * 100 for _ in range(100))
        note = _make_note(content=body)
        snippet = _read_note_snippet(note)
        assert len(snippet.encode("utf-8")) <= 8192

    def test_short_content_not_truncated(self) -> None:
        note = _make_note(content="just a few lines\nof text\n")
        snippet = _read_note_snippet(note)
        assert "just a few lines" in snippet
        assert "of text" in snippet

    def test_missing_content_returns_empty_string(self) -> None:
        # content IS NULL (partial row) must return "" silently, never raise.
        note = _make_note(content=None)
        snippet = _read_note_snippet(note)
        assert snippet == ""

    def test_leading_whitespace_stripped(self) -> None:
        note = _make_note(content="\n\n  Body straight from Postgres.")
        snippet = _read_note_snippet(note)
        assert snippet.startswith("Body straight from Postgres.")


# ---------------------------------------------------------------------------
# resolve_note_body — Postgres ``content`` is authoritative; pure passthrough.
# ---------------------------------------------------------------------------


class TestResolveNoteBody:
    def test_returns_content_when_present(self) -> None:
        assert resolve_note_body("from postgres") == "from postgres"

    def test_returns_none_when_content_none(self) -> None:
        assert resolve_note_body(None) is None

    def test_empty_string_content_is_served(self) -> None:
        # An empty body is a valid value, returned as-is.
        assert resolve_note_body("") == ""


# ---------------------------------------------------------------------------
# _note_to_review_dict — reads the snippet from ``content``.
# ---------------------------------------------------------------------------


class TestNoteToReviewDict:
    def test_returns_all_required_keys(self) -> None:
        note = _make_note(
            note_id="shape-note", content="body text", tags=["foo", "bar"]
        )
        d = _note_to_review_dict(note)
        assert set(d.keys()) >= {
            "id",
            "title",
            "snippet",
            "visibility",
            "visibility_verified",
            "updated_at",
            "tags",
            "type",
            "source",
            "deleted_at",
        }

    def test_id_matches_note_id(self) -> None:
        note = _make_note(note_id="my-note", content="stuff")
        d = _note_to_review_dict(note)
        assert d["id"] == "my-note"

    def test_snippet_contains_content(self) -> None:
        note = _make_note(note_id="snip-note", content="unique sentinel content")
        d = _note_to_review_dict(note)
        assert "unique sentinel content" in d["snippet"]

    def test_tags_serialized_as_list(self) -> None:
        note = _make_note(note_id="tagged", content="x", tags=["alpha", "beta"])
        d = _note_to_review_dict(note)
        assert d["tags"] == ["alpha", "beta"]

    def test_empty_tags_returns_empty_list(self) -> None:
        note = _make_note(note_id="no-tags", content="x", tags=[])
        d = _note_to_review_dict(note)
        assert d["tags"] == []

    def test_updated_at_isoformat_when_set(self) -> None:
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        note = _make_note(note_id="ts-note", content="x", updated_at=ts)
        d = _note_to_review_dict(note)
        assert d["updated_at"] == ts.isoformat()

    def test_updated_at_none_when_not_set(self) -> None:
        note = _make_note(note_id="no-ts", content="x", updated_at=None)
        d = _note_to_review_dict(note)
        assert d["updated_at"] is None

    def test_deleted_at_isoformat_when_set(self) -> None:
        ts = datetime(2025, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
        note = _make_note(note_id="del-note", content="x", deleted_at=ts)
        d = _note_to_review_dict(note)
        assert d["deleted_at"] == ts.isoformat()

    def test_deleted_at_none_when_not_set(self) -> None:
        note = _make_note(note_id="live-note", content="x", deleted_at=None)
        d = _note_to_review_dict(note)
        assert d["deleted_at"] is None

    def test_missing_content_snippet_is_empty_string(self) -> None:
        note = _make_note(note_id="ghost", content=None)
        d = _note_to_review_dict(note)
        assert d["snippet"] == ""

    def test_visibility_and_type_and_source_included(self) -> None:
        note = _make_note(
            note_id="full-note",
            content="content",
            visibility="public",
            type_="atom",
            source="web",
        )
        d = _note_to_review_dict(note)
        assert d["visibility"] == "public"
        assert d["type"] == "atom"
        assert d["source"] == "web"
