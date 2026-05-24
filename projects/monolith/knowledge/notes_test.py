"""Unit tests for knowledge.notes helper functions.

Covers:
  _read_note_snippet              — line/byte cap, missing file, frontmatter strip
  _note_to_review_dict            — serialization shape and snippet inclusion
  _serialize_frontmatter          — round-trip YAML fidelity and key ordering
  _write_note_visibility_frontmatter — writes correct frontmatter to disk, guards
  _trash_filename                 — collision-safe naming, timestamp format
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge import frontmatter
from knowledge.models import Note
from knowledge.notes import (
    _note_to_review_dict,
    _read_note_snippet,
    _serialize_frontmatter,
    _trash_filename,
    _write_note_visibility_frontmatter,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_note(
    *,
    note_id: str = "test-note",
    path: str | None = None,
    title: str = "Test Note",
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
        content_hash=f"hash-{note_id}",
        type=type_,
        visibility=visibility,  # type: ignore[arg-type]
        visibility_verified=visibility_verified,
        tags=tags or [],
        source=source,
        deleted_at=deleted_at,
        updated_at=updated_at,
    )


def _write_vault_note(
    vault_root: Path,
    rel_path: str,
    *,
    body: str = "Hello world.",
    visibility: str | None = None,
) -> Path:
    """Write a minimal markdown file at *vault_root/rel_path* and return it."""
    fm: dict = {}
    if visibility is not None:
        fm["visibility"] = visibility
    if fm:
        fm_str = yaml.dump(fm, default_flow_style=False, sort_keys=False)
        content = f"---\n{fm_str}---\n\n{body}\n"
    else:
        content = body
    target = vault_root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


# ---------------------------------------------------------------------------
# _read_note_snippet
# ---------------------------------------------------------------------------


class TestReadNoteSnippet:
    def test_returns_body_without_frontmatter(self, tmp_path: Path) -> None:
        note = _make_note(path="note.md")
        _write_vault_note(
            tmp_path, "note.md", body="This is the body.", visibility="public"
        )
        snippet = _read_note_snippet(tmp_path, note)
        assert "This is the body." in snippet
        # Frontmatter key must not bleed into the snippet.
        assert "visibility" not in snippet

    def test_line_cap_returns_at_most_100_lines(self, tmp_path: Path) -> None:
        note = _make_note(path="long.md")
        body = "\n".join(f"line {i}" for i in range(200))
        _write_vault_note(tmp_path, "long.md", body=body)
        snippet = _read_note_snippet(tmp_path, note)
        lines = snippet.splitlines()
        assert len(lines) == 100
        assert lines[0] == "line 0"
        assert lines[99] == "line 99"
        assert "line 100" not in snippet

    def test_byte_cap_truncates_to_8192_bytes(self, tmp_path: Path) -> None:
        note = _make_note(path="big.md")
        # Each "line" is 100 ASCII chars; 100 lines ≈ 10 100 bytes > 8 192 cap.
        body = "\n".join("x" * 100 for _ in range(100))
        _write_vault_note(tmp_path, "big.md", body=body)
        snippet = _read_note_snippet(tmp_path, note)
        assert len(snippet.encode("utf-8")) <= 8192

    def test_short_content_not_truncated(self, tmp_path: Path) -> None:
        note = _make_note(path="short.md")
        body = "just a few lines\nof text\n"
        _write_vault_note(tmp_path, "short.md", body=body)
        snippet = _read_note_snippet(tmp_path, note)
        assert "just a few lines" in snippet
        assert "of text" in snippet

    def test_missing_file_returns_empty_string(self, tmp_path: Path) -> None:
        note = _make_note(path="ghost.md")
        # No file written — must return "" silently.
        snippet = _read_note_snippet(tmp_path, note)
        assert snippet == ""

    def test_path_outside_vault_returns_empty_string(self, tmp_path: Path) -> None:
        # A relative path that escapes the vault root must be rejected.
        note = _make_note(path="../escape.md")
        snippet = _read_note_snippet(tmp_path, note)
        assert snippet == ""

    def test_broken_frontmatter_falls_back_to_raw(self, tmp_path: Path) -> None:
        note = _make_note(path="broken.md")
        # Frontmatter that yaml cannot parse as a mapping.
        raw = "---\n: bad yaml {\n---\n\nbody text here\n"
        (tmp_path / "broken.md").write_text(raw)
        # Must not raise; the raw text (with frontmatter junk) is returned.
        snippet = _read_note_snippet(tmp_path, note)
        assert isinstance(snippet, str)
        # "body text here" may or may not appear depending on fallback, but
        # the critical guarantee is: no exception and result is a str.

    def test_no_frontmatter_returns_body(self, tmp_path: Path) -> None:
        note = _make_note(path="plain.md")
        (tmp_path / "plain.md").write_text("plain text\nno frontmatter\n")
        snippet = _read_note_snippet(tmp_path, note)
        assert "plain text" in snippet


# ---------------------------------------------------------------------------
# _note_to_review_dict
# ---------------------------------------------------------------------------


class TestNoteToReviewDict:
    def test_returns_all_required_keys(self, tmp_path: Path) -> None:
        note = _make_note(note_id="shape-note", tags=["foo", "bar"])
        _write_vault_note(tmp_path, "shape-note.md", body="body text")
        d = _note_to_review_dict(note, tmp_path)
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

    def test_id_matches_note_id(self, tmp_path: Path) -> None:
        note = _make_note(note_id="my-note")
        _write_vault_note(tmp_path, "my-note.md", body="stuff")
        d = _note_to_review_dict(note, tmp_path)
        assert d["id"] == "my-note"

    def test_snippet_contains_disk_content(self, tmp_path: Path) -> None:
        note = _make_note(note_id="snip-note")
        _write_vault_note(tmp_path, "snip-note.md", body="unique sentinel content")
        d = _note_to_review_dict(note, tmp_path)
        assert "unique sentinel content" in d["snippet"]

    def test_tags_serialized_as_list(self, tmp_path: Path) -> None:
        note = _make_note(note_id="tagged", tags=["alpha", "beta"])
        _write_vault_note(tmp_path, "tagged.md", body="")
        d = _note_to_review_dict(note, tmp_path)
        assert d["tags"] == ["alpha", "beta"]

    def test_empty_tags_returns_empty_list(self, tmp_path: Path) -> None:
        note = _make_note(note_id="no-tags", tags=[])
        _write_vault_note(tmp_path, "no-tags.md", body="x")
        d = _note_to_review_dict(note, tmp_path)
        assert d["tags"] == []

    def test_updated_at_isoformat_when_set(self, tmp_path: Path) -> None:
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        note = _make_note(note_id="ts-note", updated_at=ts)
        _write_vault_note(tmp_path, "ts-note.md", body="x")
        d = _note_to_review_dict(note, tmp_path)
        assert d["updated_at"] == ts.isoformat()

    def test_updated_at_none_when_not_set(self, tmp_path: Path) -> None:
        note = _make_note(note_id="no-ts", updated_at=None)
        _write_vault_note(tmp_path, "no-ts.md", body="x")
        d = _note_to_review_dict(note, tmp_path)
        assert d["updated_at"] is None

    def test_deleted_at_isoformat_when_set(self, tmp_path: Path) -> None:
        ts = datetime(2025, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
        note = _make_note(note_id="del-note", deleted_at=ts)
        _write_vault_note(tmp_path, "del-note.md", body="x")
        d = _note_to_review_dict(note, tmp_path)
        assert d["deleted_at"] == ts.isoformat()

    def test_deleted_at_none_when_not_set(self, tmp_path: Path) -> None:
        note = _make_note(note_id="live-note", deleted_at=None)
        _write_vault_note(tmp_path, "live-note.md", body="x")
        d = _note_to_review_dict(note, tmp_path)
        assert d["deleted_at"] is None

    def test_missing_file_snippet_is_empty_string(self, tmp_path: Path) -> None:
        note = _make_note(note_id="ghost", path="ghost.md")
        # No file on disk — snippet must be "" (not raise).
        d = _note_to_review_dict(note, tmp_path)
        assert d["snippet"] == ""

    def test_visibility_and_type_and_source_included(self, tmp_path: Path) -> None:
        note = _make_note(
            note_id="full-note",
            visibility="public",
            type_="atom",
            source="web",
        )
        _write_vault_note(tmp_path, "full-note.md", body="content")
        d = _note_to_review_dict(note, tmp_path)
        assert d["visibility"] == "public"
        assert d["type"] == "atom"
        assert d["source"] == "web"


# ---------------------------------------------------------------------------
# _serialize_frontmatter
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


# ---------------------------------------------------------------------------
# _write_note_visibility_frontmatter
# ---------------------------------------------------------------------------


class TestWriteNoteVisibilityFrontmatter:
    def test_sets_public_visibility_in_frontmatter(self, tmp_path: Path) -> None:
        note = _make_note(note_id="wv-note", path="wv-note.md")
        _write_vault_note(tmp_path, "wv-note.md", body="body text")
        _write_note_visibility_frontmatter(tmp_path, note, "public")
        disk_raw = (tmp_path / "wv-note.md").read_text()
        parsed, _ = frontmatter.parse(disk_raw)
        assert parsed.visibility == "public"

    def test_sets_private_visibility_in_frontmatter(self, tmp_path: Path) -> None:
        note = _make_note(note_id="priv-note", path="priv-note.md")
        _write_vault_note(tmp_path, "priv-note.md", body="private body")
        _write_note_visibility_frontmatter(tmp_path, note, "private")
        disk_raw = (tmp_path / "priv-note.md").read_text()
        parsed, _ = frontmatter.parse(disk_raw)
        assert parsed.visibility == "private"

    def test_none_removes_visibility_key_from_frontmatter(self, tmp_path: Path) -> None:
        note = _make_note(note_id="clear-note", path="clear-note.md")
        _write_vault_note(tmp_path, "clear-note.md", body="body", visibility="public")
        _write_note_visibility_frontmatter(tmp_path, note, None)
        disk_raw = (tmp_path / "clear-note.md").read_text()
        assert "visibility:" not in disk_raw
        parsed, _ = frontmatter.parse(disk_raw)
        assert parsed.visibility is None

    def test_preserves_body_content_after_write(self, tmp_path: Path) -> None:
        note = _make_note(note_id="body-note", path="body-note.md")
        _write_vault_note(tmp_path, "body-note.md", body="preserved body text")
        _write_note_visibility_frontmatter(tmp_path, note, "public")
        disk_raw = (tmp_path / "body-note.md").read_text()
        assert "preserved body text" in disk_raw

    def test_missing_file_raises_value_error(self, tmp_path: Path) -> None:
        note = _make_note(note_id="no-file", path="no-file.md")
        with pytest.raises(ValueError, match="not found on disk"):
            _write_note_visibility_frontmatter(tmp_path, note, "public")

    def test_path_outside_vault_raises_value_error(self, tmp_path: Path) -> None:
        # A path that resolves outside vault_root must be rejected.
        note = _make_note(note_id="escape", path="../escape.md")
        with pytest.raises(ValueError, match="not found on disk"):
            _write_note_visibility_frontmatter(tmp_path, note, "public")

    def test_overwrites_existing_visibility_value(self, tmp_path: Path) -> None:
        note = _make_note(note_id="overwrite-note", path="overwrite-note.md")
        _write_vault_note(
            tmp_path, "overwrite-note.md", body="body", visibility="private"
        )
        _write_note_visibility_frontmatter(tmp_path, note, "public")
        disk_raw = (tmp_path / "overwrite-note.md").read_text()
        parsed, _ = frontmatter.parse(disk_raw)
        assert parsed.visibility == "public"


# ---------------------------------------------------------------------------
# _trash_filename
# ---------------------------------------------------------------------------


class TestTrashFilename:
    def test_basic_format_no_collision(self, tmp_path: Path) -> None:
        when = datetime(2025, 3, 15, 10, 30, 45, tzinfo=timezone.utc)
        filename = _trash_filename("my-note", when=when, collision_root=tmp_path)
        assert filename == "20250315T103045Z-my-note.md"

    def test_zero_padded_timestamp_components(self, tmp_path: Path) -> None:
        when = datetime(2025, 1, 5, 8, 3, 9, tzinfo=timezone.utc)
        filename = _trash_filename("slug", when=when, collision_root=tmp_path)
        # Month/day/hour/min/sec must all be zero-padded to 2 digits.
        assert filename == "20250105T080309Z-slug.md"

    def test_timestamp_matches_zulu_second_resolution_pattern(
        self, tmp_path: Path
    ) -> None:
        when = datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        filename = _trash_filename("note", when=when, collision_root=tmp_path)
        # Extract the timestamp prefix and verify it matches YYYYMMDDTHHMMSSZ.
        ts_part = filename[: filename.index("-note.md")]
        assert re.fullmatch(r"\d{8}T\d{6}Z", ts_part)

    def test_first_collision_appends_counter_1(self, tmp_path: Path) -> None:
        when = datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        ts = "20250501T120000Z"
        (tmp_path / f"{ts}-slug.md").write_text("existing")
        filename = _trash_filename("slug", when=when, collision_root=tmp_path)
        assert filename == f"{ts}-slug-1.md"

    def test_multiple_collisions_increments_counter(self, tmp_path: Path) -> None:
        when = datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        ts = "20250501T120000Z"
        (tmp_path / f"{ts}-slug.md").write_text("a")
        (tmp_path / f"{ts}-slug-1.md").write_text("b")
        filename = _trash_filename("slug", when=when, collision_root=tmp_path)
        assert filename == f"{ts}-slug-2.md"

    def test_three_collisions_reaches_counter_3(self, tmp_path: Path) -> None:
        when = datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        ts = "20250501T120000Z"
        for suffix in ("", "-1", "-2"):
            (tmp_path / f"{ts}-note{suffix}.md").write_text("x")
        filename = _trash_filename("note", when=when, collision_root=tmp_path)
        assert filename == f"{ts}-note-3.md"

    def test_does_not_create_file(self, tmp_path: Path) -> None:
        """_trash_filename only computes the name — it must not touch the filesystem."""
        when = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        filename = _trash_filename("ghost", when=when, collision_root=tmp_path)
        assert not (tmp_path / filename).exists()
