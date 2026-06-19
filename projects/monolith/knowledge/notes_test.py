"""Unit tests for knowledge.notes helper functions.

DB-only after ADR 006 (Obsidian decommissioned): note bodies live in the
Postgres ``content`` column, there is no ``/vault`` on disk, and the
trash-file / frontmatter-rewrite helpers are gone. Covers:

  _read_note_snippet     — line/byte cap, missing content
  _note_to_review_dict   — serialization shape and snippet inclusion
  _serialize_frontmatter — round-trip YAML fidelity and key ordering
  resolve_note_body      — passthrough of the Postgres ``content`` body
"""

from __future__ import annotations

from datetime import datetime, timezone

from knowledge import frontmatter
from knowledge.models import Note
from knowledge.notes import (
    _note_to_review_dict,
    _read_note_snippet,
    _serialize_frontmatter,
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


# ---------------------------------------------------------------------------
# _serialize_frontmatter — unchanged by ADR 006.
# ---------------------------------------------------------------------------


class TestSerializeFrontmatter:
    def test_round_trip_preserves_promoted_fields(self) -> None:
        raw = (
            "---\n"
            "id: round-trip\n"
            "title: Round Trip Note\n"
            "type: atom\n"
            "visibility: public\n"
            "tags:\n- foo\n- bar\n"
            "---\n\nbody text\n"
        )
        parsed, body = frontmatter.parse(raw)
        reserialized = _serialize_frontmatter(parsed, body)
        reparsed, rebodied = frontmatter.parse(reserialized)

        assert reparsed.note_id == "round-trip"
        assert reparsed.title == "Round Trip Note"
        assert reparsed.type == "atom"
        assert reparsed.visibility == "public"
        assert reparsed.tags == ["foo", "bar"]
        assert "body text" in rebodied

    def test_none_fields_absent_from_output(self) -> None:
        raw = "---\nid: minimal\ntitle: Minimal\n---\n\nbody\n"
        parsed, body = frontmatter.parse(raw)
        result = _serialize_frontmatter(parsed, body)
        # Unset promoted fields must not appear as "null" entries.
        assert "visibility:" not in result
        assert "status:" not in result
        assert "source:" not in result

    def test_extra_fields_preserved_at_end(self) -> None:
        raw = (
            "---\n"
            "id: extra-test\n"
            "title: Extra Test\n"
            "custom_field: my_value\n"
            "---\n\nbody\n"
        )
        parsed, body = frontmatter.parse(raw)
        result = _serialize_frontmatter(parsed, body)
        assert "custom_field" in result
        assert "my_value" in result

    def test_visibility_none_removes_key(self) -> None:
        raw = "---\nid: clear-vis\ntitle: Clear\nvisibility: public\n---\n\nbody\n"
        parsed, body = frontmatter.parse(raw)
        parsed.visibility = None
        result = _serialize_frontmatter(parsed, body)
        assert "visibility:" not in result

    def test_output_wrapped_in_frontmatter_delimiters(self) -> None:
        raw = "---\nid: fence\ntitle: Fence\n---\n\nbody\n"
        parsed, body = frontmatter.parse(raw)
        result = _serialize_frontmatter(parsed, body)
        assert result.startswith("---\n")
        # Must have the closing delimiter followed by blank line + body.
        assert "---\n\n" in result

    def test_body_preserved_verbatim(self) -> None:
        body_text = "line one\nline two\n\nParagraph two.\n"
        raw = f"---\nid: body-test\ntitle: Body Test\n---\n\n{body_text}"
        parsed, body = frontmatter.parse(raw)
        result = _serialize_frontmatter(parsed, body)
        assert "line one" in result
        assert "Paragraph two." in result

    def test_promoted_key_ordering_id_before_title(self) -> None:
        raw = "---\nid: order-check\ntitle: Order Check\ntype: atom\n---\n\nbody\n"
        parsed, body = frontmatter.parse(raw)
        result = _serialize_frontmatter(parsed, body)
        id_pos = result.index("id:")
        title_pos = result.index("title:")
        type_pos = result.index("type:")
        assert id_pos < title_pos < type_pos
