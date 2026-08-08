"""Tests for Discord bot integration."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from chat.bot import (
    _format_codex_login_message,
    download_image_attachments,
    should_respond,
)


def test_format_codex_login_message_with_url_and_code():
    result = {
        "message": "Approve the login.",
        "verification_url": "https://example.test/device",
        "user_code": "ABCD-1234",
    }
    assert _format_codex_login_message(result) == (
        "🔐 Approve the login.\n"
        "Approve in your browser: https://example.test/device\n"
        "User code: `ABCD-1234`"
    )


def test_format_codex_login_message_pending():
    assert (
        _format_codex_login_message({"pending": True, "message": "Already pending."})
        == "🔐 Already pending."
    )


def test_format_codex_login_message_error():
    assert (
        _format_codex_login_message(
            {"error": "broker offline", "message": "Try again."}
        )
        == "🔐 Try again.\nBroker error: broker offline"
    )


class TestShouldRespond:
    def test_responds_to_mention(self):
        """Bot responds when mentioned."""
        message = MagicMock()
        message.author.bot = False
        message.content = "Hello"
        bot_user = MagicMock()
        bot_user.id = 12345
        message.mentions = [bot_user]
        assert should_respond(message, bot_user) is True

    def test_ignores_bot_messages(self):
        """Bot does not respond to other bots."""
        message = MagicMock()
        message.author.bot = True
        message.content = "Hello"
        bot_user = MagicMock()
        message.mentions = []
        assert should_respond(message, bot_user) is False

    def test_ignores_unmentioned_messages(self):
        """Bot does not respond to messages that don't mention it."""
        message = MagicMock()
        message.author.bot = False
        message.content = "Hello everyone"
        bot_user = MagicMock()
        bot_user.id = 12345
        message.mentions = []
        message.reference = None
        assert should_respond(message, bot_user) is False

    def test_responds_to_reply(self):
        """Bot responds when a message is a reply to a bot message."""
        message = MagicMock()
        message.author.bot = False
        message.content = "Thanks"
        bot_user = MagicMock()
        bot_user.id = 12345
        message.mentions = []
        message.reference = MagicMock()
        message.reference.resolved = MagicMock()
        message.reference.resolved.author.id = 12345
        assert should_respond(message, bot_user) is True


class TestDownloadImageAttachments:
    @pytest.mark.asyncio
    async def test_downloads_image_attachments(self):
        """download_image_attachments downloads images and describes them."""
        att = MagicMock()
        att.content_type = "image/png"
        att.filename = "photo.png"
        att.read = AsyncMock(return_value=b"\x89PNG")

        vision_client = AsyncMock()
        vision_client.describe.return_value = "A cat sitting on a chair"

        result = await download_image_attachments([att], vision_client)

        assert len(result) == 1
        assert result[0]["data"] == b"\x89PNG"
        assert result[0]["content_type"] == "image/png"
        assert result[0]["filename"] == "photo.png"
        assert result[0]["description"] == "A cat sitting on a chair"

    @pytest.mark.asyncio
    async def test_skips_non_image_attachments(self):
        """download_image_attachments ignores non-image content types."""
        att = MagicMock()
        att.content_type = "application/pdf"
        att.filename = "doc.pdf"

        vision_client = AsyncMock()

        result = await download_image_attachments([att], vision_client)

        assert len(result) == 0
        vision_client.describe.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_attachment_with_no_content_type(self):
        """download_image_attachments skips attachments without content_type."""
        att = MagicMock()
        att.content_type = None
        att.filename = "unknown"

        vision_client = AsyncMock()

        result = await download_image_attachments([att], vision_client)

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_blob_cache_hit_skips_vision_call(self):
        """download_image_attachments reuses blob description when store has a cache hit."""
        att = MagicMock()
        att.content_type = "image/png"
        att.filename = "cached.png"
        att.read = AsyncMock(return_value=b"\x89PNG")

        vision_client = AsyncMock()

        mock_blob = MagicMock()
        mock_blob.description = "Cached description"
        mock_store = MagicMock()
        mock_store.get_blob.return_value = mock_blob

        result = await download_image_attachments(
            [att], vision_client, store=mock_store
        )

        assert len(result) == 1
        assert result[0]["description"] == "Cached description"
        vision_client.describe.assert_not_called()

    @pytest.mark.asyncio
    async def test_blob_cache_miss_calls_vision(self):
        """download_image_attachments calls vision when store has no matching blob."""
        att = MagicMock()
        att.content_type = "image/png"
        att.filename = "new.png"
        att.read = AsyncMock(return_value=b"\x89PNG_NEW")

        vision_client = AsyncMock()
        vision_client.describe.return_value = "A new image"

        mock_store = MagicMock()
        mock_store.get_blob.return_value = None

        result = await download_image_attachments(
            [att], vision_client, store=mock_store
        )

        assert len(result) == 1
        assert result[0]["description"] == "A new image"
        vision_client.describe.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_store_always_calls_vision(self):
        """download_image_attachments calls vision when no store is provided."""
        att = MagicMock()
        att.content_type = "image/jpeg"
        att.filename = "photo.jpg"
        att.read = AsyncMock(return_value=b"\xff\xd8\xff")

        vision_client = AsyncMock()
        vision_client.describe.return_value = "A sunset"

        result = await download_image_attachments([att], vision_client, store=None)

        assert len(result) == 1
        assert result[0]["description"] == "A sunset"
        vision_client.describe.assert_called_once()
