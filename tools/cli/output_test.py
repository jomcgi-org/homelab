"""Tests for token-efficient output formatting."""

from unittest.mock import patch

import pytest  # noqa: F401

from tools.cli.output import compact_line, format_edges, search_line, write_to_tmpfile


class TestCompactLine:
    def test_basic_format(self):
        result = compact_line(42, "some/path.md", "obsidian", "bad JSON", 3)
        assert result == "[42] some/path.md (obsidian) — bad JSON [3 retries]"

    def test_no_error(self):
        result = compact_line(1, "path.md", "youtube", None, 0)
        assert result == "[1] path.md (youtube)"


class TestFormatEdges:
    def test_typed_edges(self):
        edges = [
            {"kind": "edge", "edge_type": "derives_from", "target_id": "note-a"},
            {"kind": "edge", "edge_type": "related", "target_id": "note-b"},
        ]
        result = format_edges(edges)
        assert result == "derives_from→note-a, related→note-b"

    def test_empty(self):
        assert format_edges([]) == ""


class TestWriteToTmpfile:
    def test_writes_content_and_returns_path(self, tmp_path):
        with patch("tools.cli.output.TMPDIR", tmp_path):
            path = write_to_tmpfile("my-note", "# Hello\nWorld")
            assert path.exists()
            assert path.read_text() == "# Hello\nWorld"
            assert "my-note" in path.name

    def test_overwrites_existing(self, tmp_path):
        with patch("tools.cli.output.TMPDIR", tmp_path):
            write_to_tmpfile("note", "v1")
            path = write_to_tmpfile("note", "v2")
            assert path.read_text() == "v2"


class TestSearchLine:
    def test_basic_format_no_edges(self):
        """Score, note_id, title, and type are all present in the output line."""
        result = search_line(0.95, "n1", "Attention Is All You Need", "paper", [])
        assert result == "[0.95] n1 — Attention Is All You Need (paper)"

    def test_score_formatted_to_two_decimal_places(self):
        """Score is always rendered with exactly two decimal places."""
        result = search_line(1.0, "n2", "Some Note", "concept", [])
        assert result.startswith("[1.00]")

    def test_with_typed_edges_appends_edge_line(self):
        """Typed edges are appended as an indented second line."""
        edges = [
            {"kind": "edge", "edge_type": "derives_from", "target_id": "note-a"},
            {"kind": "edge", "edge_type": "related", "target_id": "note-b"},
        ]
        result = search_line(0.80, "n3", "My Note", "note", edges)
        lines = result.splitlines()
        assert len(lines) == 2
        assert lines[0] == "[0.80] n3 — My Note (note)"
        assert "derives_from→note-a" in lines[1]
        assert "related→note-b" in lines[1]

    def test_link_kind_edges_excluded(self):
        """Edges with kind='link' (not 'edge') are filtered out and not shown."""
        edges = [
            {"kind": "link", "edge_type": None, "target_id": "other-note"},
        ]
        result = search_line(0.70, "n4", "Link Note", "note", edges)
        # No edge line appended for plain wikilinks
        assert "\n" not in result

    def test_very_long_title_renders_without_truncation(self):
        """A very long title is preserved verbatim — no truncation occurs."""
        long_title = "Very Long Title Word " * 50  # 1050 chars
        result = search_line(0.50, "n5", long_title, "note", [])
        assert long_title in result

    def test_unicode_in_note_id_and_title(self):
        """Unicode characters in note_id and title render correctly."""
        result = search_line(0.88, "ノート-001", "人工知能の未来", "concept", [])
        assert "ノート-001" in result
        assert "人工知能の未来" in result

    def test_zero_score(self):
        """Score of exactly 0.0 renders as [0.00]."""
        result = search_line(0.0, "n6", "Zero Score Note", "note", [])
        assert result.startswith("[0.00]")

    def test_empty_note_type_renders_empty_parens(self):
        """An empty string note type still renders with parentheses."""
        result = search_line(0.75, "n7", "Untyped Note", "", [])
        assert "()" in result


class TestCompactLineBoundary:
    def test_very_long_path_renders_without_truncation(self):
        """A very long path is preserved verbatim in the output."""
        long_path = "a/b/" * 200 + "note.md"  # ~803 chars
        result = compact_line(1, long_path, "obsidian", None, 0)
        assert long_path in result

    def test_unicode_in_path_source_and_error(self):
        """Unicode characters in path, source, and error all render correctly."""
        result = compact_line(
            99,
            "日本語/ノート.md",
            "黒曜石",
            "エラー: JSON解析失敗",
            2,
        )
        assert "日本語/ノート.md" in result
        assert "黒曜石" in result
        assert "エラー: JSON解析失敗" in result

    def test_empty_string_error_treated_as_no_error(self):
        """Empty string error (falsy) produces the no-error branch — no '—' separator."""
        result = compact_line(1, "path.md", "src", "", 0)
        assert "—" not in result
        assert "retries" not in result

    def test_zero_id(self):
        """id=0 is a valid integer boundary and renders correctly."""
        result = compact_line(0, "path.md", "src", None, 0)
        assert result.startswith("[0]")

    def test_large_retry_count(self):
        """Very large retry_count renders as a number without truncation."""
        result = compact_line(1, "path.md", "src", "boom", 9999)
        assert "[9999 retries]" in result


class TestFormatEdgesBoundary:
    def test_missing_kind_key_excluded(self):
        """An edge dict without a 'kind' key at all is excluded (get returns None)."""
        edges = [{"edge_type": "related", "target_id": "note-x"}]
        result = format_edges(edges)
        assert result == ""

    def test_none_kind_excluded(self):
        """An edge with kind=None is excluded (None != 'edge')."""
        edges = [{"kind": None, "edge_type": "related", "target_id": "note-x"}]
        result = format_edges(edges)
        assert result == ""

    def test_none_edge_type_renders_as_string_none(self):
        """kind='edge' with edge_type=None renders via str() as 'None→target'."""
        edges = [{"kind": "edge", "edge_type": None, "target_id": "note-x"}]
        result = format_edges(edges)
        assert "None→note-x" in result

    def test_unicode_edge_type_and_target(self):
        """Unicode edge_type and target_id are preserved in the compact representation."""
        edges = [{"kind": "edge", "edge_type": "関連", "target_id": "ノート-1"}]
        result = format_edges(edges)
        assert "関連→ノート-1" in result

    def test_mixed_edge_and_non_edge_kinds(self):
        """Only 'edge' kind entries appear; other kinds are silently dropped."""
        edges = [
            {"kind": "edge", "edge_type": "derives_from", "target_id": "note-a"},
            {"kind": "link", "edge_type": None, "target_id": "note-b"},
            {"kind": "unknown", "edge_type": "foo", "target_id": "note-c"},
        ]
        result = format_edges(edges)
        assert "derives_from→note-a" in result
        assert "note-b" not in result
        assert "note-c" not in result


class TestWriteToTmpfileBoundary:
    def test_unicode_content_round_trips_correctly(self, tmp_path):
        """Unicode content (CJK, emoji) is written and read back identically."""
        content = "# こんにちは 🌍\n\n世界のデータ"
        with patch("tools.cli.output.TMPDIR", tmp_path):
            path = write_to_tmpfile("unicode-note", content)
        assert path.read_text() == content

    def test_empty_content_creates_empty_file(self, tmp_path):
        """Writing an empty string creates an empty file without error."""
        with patch("tools.cli.output.TMPDIR", tmp_path):
            path = write_to_tmpfile("empty-note", "")
        assert path.exists()
        assert path.read_text() == ""

    def test_very_large_content(self, tmp_path):
        """Very large content (>1 MB) is written and read back correctly."""
        content = "x" * (1024 * 1024)  # 1 MB
        with patch("tools.cli.output.TMPDIR", tmp_path):
            path = write_to_tmpfile("big-note", content)
        assert len(path.read_text()) == 1024 * 1024
