"""Tests for lenient frontmatter parsing."""

from datetime import datetime, timezone

import pytest

from knowledge.frontmatter import (
    FrontmatterError,
    ParsedFrontmatter,
    _sanitize_yaml_block,
    parse,
)


class TestParse:
    def test_no_frontmatter_returns_empty_metadata_and_full_body(self):
        meta, body = parse("Just a body.")
        assert meta == ParsedFrontmatter()
        assert body == "Just a body."

    def test_well_formed_frontmatter(self):
        raw = (
            "---\n"
            "title: Attention Is All You Need\n"
            "type: paper\n"
            "tags: [ml, attention]\n"
            "created: 2017-06-12\n"
            "---\n"
            "Body text."
        )
        meta, body = parse(raw)
        assert meta.title == "Attention Is All You Need"
        assert meta.type == "paper"
        assert meta.tags == ["ml", "attention"]
        assert meta.created == datetime(2017, 6, 12, tzinfo=timezone.utc)
        assert body == "Body text."

    def test_tags_as_comma_string(self):
        raw = "---\ntags: ml, attention,  transformers\n---\nx"
        meta, _ = parse(raw)
        assert meta.tags == ["ml", "attention", "transformers"]

    def test_tags_as_space_string(self):
        raw = "---\ntags: ml attention transformers\n---\nx"
        meta, _ = parse(raw)
        assert meta.tags == ["ml", "attention", "transformers"]

    def test_aliases_same_rules_as_tags(self):
        raw = "---\naliases: [Foo, Bar]\n---\nx"
        meta, _ = parse(raw)
        assert meta.aliases == ["Foo", "Bar"]

    def test_invalid_yaml_raises_frontmatter_error(self):
        raw = "---\ntitle: [unterminated\n---\nBody text."
        with pytest.raises(FrontmatterError):
            parse(raw)

    def test_non_mapping_yaml_raises_frontmatter_error(self):
        raw = "---\n- just\n- a\n- list\n---\nBody text."
        with pytest.raises(FrontmatterError):
            parse(raw)

    def test_invalid_date_yields_none(self):
        raw = "---\ncreated: not-a-date\n---\nx"
        meta, _ = parse(raw)
        assert meta.created is None

    def test_unknown_keys_land_in_extra(self):
        raw = "---\ntitle: T\nauthor: Karpathy\nyear: 2017\n---\nx"
        meta, _ = parse(raw)
        assert meta.title == "T"
        assert meta.extra == {"author": "Karpathy", "year": 2017}

    def test_promoted_keys_never_appear_in_extra(self):
        raw = "---\ntype: paper\nstatus: published\nauthor: K\n---\nx"
        meta, _ = parse(raw)
        assert "type" not in meta.extra
        assert "status" not in meta.extra
        assert meta.extra == {"author": "K"}

    def test_delimiter_not_at_top_is_not_frontmatter(self):
        raw = "Body first.\n\n---\ntitle: T\n---\nMore body."
        meta, body = parse(raw)
        assert meta == ParsedFrontmatter()
        assert body == raw

    def test_id_captured_verbatim(self):
        raw = "---\nid: attention-is-all-you-need\n---\nx"
        meta, _ = parse(raw)
        assert meta.note_id == "attention-is-all-you-need"

    def test_id_missing_returns_none(self):
        raw = "---\ntitle: T\n---\nx"
        meta, _ = parse(raw)
        assert meta.note_id is None

    def test_edges_block_parsed(self):
        raw = (
            "---\n"
            "edges:\n"
            "  refines: [parent-note]\n"
            "  related: [a, b]\n"
            "  contradicts: []\n"
            "---\n"
            "x"
        )
        meta, _ = parse(raw)
        assert meta.edges == {
            "refines": ["parent-note"],
            "related": ["a", "b"],
            # Empty lists are dropped at the parser level.
        }

    def test_edges_unknown_key_dropped_with_warning(self, caplog):
        raw = "---\nedges:\n  refutes: [x]\n  refines: [y]\n---\nz"
        meta, _ = parse(raw)
        assert meta.edges == {"refines": ["y"]}
        assert any("unknown edge type" in r.message for r in caplog.records)

    def test_edges_block_missing_returns_empty_dict(self):
        raw = "---\ntitle: T\n---\nx"
        meta, _ = parse(raw)
        assert meta.edges == {}

    def test_crlf_frontmatter_parses_correctly(self):
        raw = "---\r\ntitle: CRLF Note\r\ntags: [a, b]\r\n---\r\nBody with CRLF.\r\n"
        meta, body = parse(raw)
        assert meta.title == "CRLF Note"
        assert meta.tags == ["a", "b"]
        assert body == "Body with CRLF.\r\n"

    def test_up_key_is_no_longer_recognized(self):
        # `up:` was removed in favor of `edges.refines`. A one-shot vault
        # migration script (out of scope) rewrites legacy `up:` keys.
        # Until then, the parser ignores `up:` and lets it land in `extra`.
        raw = "---\nup: '[[Index]]'\n---\nx"
        meta, _ = parse(raw)
        assert meta.extra == {"up": "[[Index]]"}

    def test_title_with_unquoted_colon_parses(self):
        raw = "---\ntitle: Atomic Note: One Concept Per Note Principle\ntype: atom\n---\nBody."
        meta, body = parse(raw)
        assert meta.title == "Atomic Note: One Concept Per Note Principle"
        assert meta.type == "atom"
        assert body == "Body."

    def test_title_with_multiple_colons_parses(self):
        raw = "---\ntitle: Note Type Taxonomy: atom / fact / active\n---\nBody."
        meta, _ = parse(raw)
        assert meta.title == "Note Type Taxonomy: atom / fact / active"

    def test_already_quoted_title_with_colon_unchanged(self):
        raw = '---\ntitle: "Already: Quoted"\n---\nBody.'
        meta, _ = parse(raw)
        assert meta.title == "Already: Quoted"

    def test_single_quoted_title_with_colon_unchanged(self):
        raw = "---\ntitle: 'Single: Quoted'\n---\nBody."
        meta, _ = parse(raw)
        assert meta.title == "Single: Quoted"

    def test_colon_without_space_not_affected(self):
        raw = "---\ntitle: ratio 3:1 works\n---\nx"
        meta, _ = parse(raw)
        assert meta.title == "ratio 3:1 works"

    def test_visibility_public_parsed(self):
        raw = "---\nid: a\ntitle: A\nvisibility: public\n---\nbody"
        meta, _ = parse(raw)
        assert meta.visibility == "public"

    def test_visibility_private_parsed(self):
        raw = "---\nid: a\ntitle: A\nvisibility: private\n---\nbody"
        meta, _ = parse(raw)
        assert meta.visibility == "private"

    def test_visibility_missing_is_none(self):
        raw = "---\nid: a\ntitle: A\n---\nbody"
        meta, _ = parse(raw)
        assert meta.visibility is None

    def test_visibility_empty_string_is_none(self):
        raw = "---\nid: a\ntitle: A\nvisibility:\n---\nbody"
        meta, _ = parse(raw)
        assert meta.visibility is None

    def test_visibility_unknown_value_warns_and_nulls(self, caplog):
        """Bad classifier output must not break ingest — warn, treat as null."""
        raw = "---\nid: a\ntitle: A\nvisibility: yellow\n---\nbody"
        with caplog.at_level("WARNING", logger="monolith.knowledge.frontmatter"):
            meta, _ = parse(raw)
        assert meta.visibility is None
        assert any("visibility" in r.message.lower() for r in caplog.records)

    def test_nested_edges_not_broken_by_sanitizer(self):
        raw = (
            "---\n"
            "title: Test: With Colon\n"
            "edges:\n"
            "  refines: [parent]\n"
            "  related: [a, b]\n"
            "---\n"
            "Body."
        )
        meta, body = parse(raw)
        assert meta.title == "Test: With Colon"
        assert meta.edges == {"refines": ["parent"], "related": ["a", "b"]}


class TestSanitizeYamlBlock:
    def test_quotes_value_with_embedded_colon_space(self):
        block = "title: Foo: Bar Baz"
        assert _sanitize_yaml_block(block) == 'title: "Foo: Bar Baz"'

    def test_leaves_already_quoted_values_alone(self):
        block = 'title: "Foo: Bar"'
        assert _sanitize_yaml_block(block) == 'title: "Foo: Bar"'

    def test_leaves_single_quoted_values_alone(self):
        block = "title: 'Foo: Bar'"
        assert _sanitize_yaml_block(block) == "title: 'Foo: Bar'"

    def test_leaves_flow_sequence_alone(self):
        block = "tags: [ml, attention]"
        assert _sanitize_yaml_block(block) == "tags: [ml, attention]"

    def test_leaves_flow_mapping_alone(self):
        block = "meta: {key: val}"
        assert _sanitize_yaml_block(block) == "meta: {key: val}"

    def test_skips_indented_lines(self):
        block = "edges:\n  refines: [parent]\n  related: [a]"
        assert _sanitize_yaml_block(block) == block

    def test_no_colon_in_value_unchanged(self):
        block = "title: Simple Title"
        assert _sanitize_yaml_block(block) == "title: Simple Title"

    def test_escapes_double_quotes_in_value(self):
        block = 'title: He said "hello": world'
        assert _sanitize_yaml_block(block) == r'title: "He said \"hello\": world"'

    def test_multiple_lines_mixed(self):
        block = "id: my-note\ntitle: Concept: Important One\ntype: atom"
        result = _sanitize_yaml_block(block)
        assert result == 'id: my-note\ntitle: "Concept: Important One"\ntype: atom'
