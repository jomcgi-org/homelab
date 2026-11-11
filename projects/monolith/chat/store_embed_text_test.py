"""Tests for _build_embed_text helper in chat.store."""

from chat.store import _build_embed_text


class TestBuildEmbedText:
    def test_empty_descriptions_returns_content_unchanged(self):
        """When descriptions list is empty, return content as-is."""
        result = _build_embed_text("Hello world", [])
        assert result == "Hello world"

    def test_single_description_appended(self):
        """Single description is formatted and appended to content."""
        result = _build_embed_text("Check this out", ["A cat sitting on a mat"])
        assert result == "Check this out\n\n[Image: A cat sitting on a mat]"

    def test_multiple_descriptions_all_appended(self):
        """Multiple descriptions are each formatted as separate lines."""
        result = _build_embed_text(
            "Beautiful day!", ["A sunset", "Blue sky with clouds"]
        )
        assert (
            result
            == "Beautiful day!\n\n[Image: A sunset]\n[Image: Blue sky with clouds]"
        )

    def test_empty_content_with_descriptions(self):
        """Empty content string with descriptions still formats correctly."""
        result = _build_embed_text("", ["Some description"])
        assert result == "\n\n[Image: Some description]"
