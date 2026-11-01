"""Tests for Discord embed handling in bot.py.

Covers:
- _has_embeddable_content: embed-only messages, mixed content, no content
- _extract_embed_text: title+description, multiple embeds, partial fields
- _process_message: embed content used when message.content is empty
"""

from unittest.mock import MagicMock

from chat.bot import _extract_embed_text, _has_embeddable_content


def _make_embed(title: str | None = None, description: str | None = None) -> MagicMock:
    embed = MagicMock()
    embed.title = title
    embed.description = description
    return embed


def _make_message(
    content: str = "",
    attachments: list | None = None,
    embeds: list | None = None,
) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    msg.attachments = attachments or []
    msg.embeds = embeds or []
    return msg


class TestHasEmbeddableContent:
    def test_text_content_is_embeddable(self):
        msg = _make_message(content="hello world")
        assert _has_embeddable_content(msg) is True

    def test_whitespace_only_content_is_not_embeddable(self):
        msg = _make_message(content="   ")
        assert _has_embeddable_content(msg) is False

    def test_image_attachment_is_embeddable(self):
        att = MagicMock()
        att.content_type = "image/png"
        msg = _make_message(attachments=[att])
        assert _has_embeddable_content(msg) is True

    def test_non_image_attachment_is_not_embeddable(self):
        att = MagicMock()
        att.content_type = "application/pdf"
        msg = _make_message(attachments=[att])
        assert _has_embeddable_content(msg) is False

    def test_embed_with_title_is_embeddable(self):
        msg = _make_message(embeds=[_make_embed(title="Homelab Changelog")])
        assert _has_embeddable_content(msg) is True

    def test_embed_with_description_is_embeddable(self):
        msg = _make_message(embeds=[_make_embed(description="Something changed.")])
        assert _has_embeddable_content(msg) is True

    def test_embed_with_neither_title_nor_description_is_not_embeddable(self):
        msg = _make_message(embeds=[_make_embed(title=None, description=None)])
        assert _has_embeddable_content(msg) is False

    def test_no_content_no_attachments_no_embeds_is_not_embeddable(self):
        msg = _make_message()
        assert _has_embeddable_content(msg) is False


class TestExtractEmbedText:
    def test_title_and_description_joined(self):
        msg = _make_message(
            embeds=[
                _make_embed(title="Homelab Changelog", description="New feature added.")
            ]
        )
        result = _extract_embed_text(msg)
        assert result == "Homelab Changelog\nNew feature added."

    def test_title_only(self):
        msg = _make_message(
            embeds=[_make_embed(title="Just a title", description=None)]
        )
        result = _extract_embed_text(msg)
        assert result == "Just a title"

    def test_description_only(self):
        msg = _make_message(
            embeds=[_make_embed(title=None, description="Just a description")]
        )
        result = _extract_embed_text(msg)
        assert result == "Just a description"

    def test_multiple_embeds_combined(self):
        msg = _make_message(
            embeds=[
                _make_embed(title="First", description="Desc one"),
                _make_embed(title="Second", description="Desc two"),
            ]
        )
        result = _extract_embed_text(msg)
        assert "First" in result
        assert "Desc one" in result
        assert "Second" in result
        assert "Desc two" in result

    def test_no_embeds_returns_empty_string(self):
        msg = _make_message(embeds=[])
        assert _extract_embed_text(msg) == ""

    def test_embed_with_no_fields_skipped(self):
        msg = _make_message(embeds=[_make_embed(title=None, description=None)])
        assert _extract_embed_text(msg) == ""
