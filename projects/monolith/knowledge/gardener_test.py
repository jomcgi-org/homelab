"""Tests for shared knowledge-pipeline constants and slug normalization."""

from knowledge.gardener import GARDENER_VERSION, MAX_GARDENER_RETRIES, _slugify


class TestSlugify:
    def test_ascii_text(self):
        assert _slugify("Hello World") == "hello-world"

    def test_unicode_nfkd_strips_accents(self):
        assert _slugify("Héllo") == "hello"

    def test_empty_string_returns_note(self):
        assert _slugify("") == "note"

    def test_multiple_special_chars_collapse_to_single_hyphen(self):
        assert _slugify("Hello!! World") == "hello-world"


class TestSurvivingConstants:
    def test_gardener_version_stamp(self):
        assert GARDENER_VERSION == "claude-sonnet-4-6@v1"

    def test_max_retries_ceiling(self):
        assert MAX_GARDENER_RETRIES == 3
