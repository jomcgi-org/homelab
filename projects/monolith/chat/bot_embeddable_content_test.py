"""Tests for _has_embeddable_content() and the early-return path in _process_message()."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from chat.bot import _has_embeddable_content, ChatBot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_attachment(
    content_type: str | None = "image/jpeg", filename: str = "img.jpg"
):
    att = MagicMock()
    att.content_type = content_type
    att.filename = filename
    return att


def _make_message(content: str = "", attachments=None):
    """Build a minimal mock discord.Message."""
    msg = MagicMock()
    msg.content = content
    msg.attachments = attachments if attachments is not None else []
    return msg


class _AsyncCtxManager:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _async_cm():
    return _AsyncCtxManager()


def _make_bot() -> ChatBot:
    with (
        patch("chat.bot.EmbeddingClient") as mock_ec,
        patch("chat.bot.create_agent") as mock_ca,
    ):
        mock_ec.return_value = AsyncMock()
        mock_ca.return_value = MagicMock()
        bot = ChatBot()
    bot._connection = MagicMock()
    bot._connection.user = MagicMock()
    bot._connection.user.id = 999
    bot._connection.user.display_name = "BotUser"
    return bot


# ---------------------------------------------------------------------------
# Tests for _has_embeddable_content()
# ---------------------------------------------------------------------------


class TestHasEmbeddableContent:
    def test_text_only_returns_true(self):
        """A message with non-empty text content returns True."""
        msg = _make_message(content="hello world")
        assert _has_embeddable_content(msg) is True

    def test_whitespace_only_text_is_not_embeddable(self):
        """Content that is only whitespace is treated as empty — returns False with no attachments."""
        msg = _make_message(content="   \t\n  ")
        assert _has_embeddable_content(msg) is False

    def test_image_attachments_only_returns_true(self):
        """A message with no text but one image attachment returns True."""
        img = _make_attachment(content_type="image/png")
        msg = _make_message(content="", attachments=[img])
        assert _has_embeddable_content(msg) is True

    def test_text_and_image_attachments_returns_true(self):
        """A message with both text and an image attachment returns True."""
        img = _make_attachment(content_type="image/gif")
        msg = _make_message(content="check this out", attachments=[img])
        assert _has_embeddable_content(msg) is True

    def test_empty_content_no_attachments_returns_false(self):
        """A message with no text and no attachments returns False."""
        msg = _make_message(content="", attachments=[])
        assert _has_embeddable_content(msg) is False

    def test_non_image_attachments_only_returns_false(self):
        """A message with only non-image attachments (e.g. PDF) returns False."""
        pdf = _make_attachment(content_type="application/pdf", filename="doc.pdf")
        msg = _make_message(content="", attachments=[pdf])
        assert _has_embeddable_content(msg) is False

    def test_multiple_non_image_attachments_returns_false(self):
        """Multiple non-image attachments do not make the message embeddable."""
        zip_att = _make_attachment(
            content_type="application/zip", filename="archive.zip"
        )
        txt_att = _make_attachment(content_type="text/plain", filename="notes.txt")
        msg = _make_message(content="", attachments=[zip_att, txt_att])
        assert _has_embeddable_content(msg) is False

    def test_attachment_with_none_content_type_returns_false(self):
        """An attachment whose content_type is None is not treated as an image."""
        att = _make_attachment(content_type=None)
        msg = _make_message(content="", attachments=[att])
        assert _has_embeddable_content(msg) is False

    def test_mixed_attachments_image_among_non_images_returns_true(self):
        """If any attachment is an image the function returns True."""
        pdf = _make_attachment(content_type="application/pdf", filename="doc.pdf")
        img = _make_attachment(content_type="image/jpeg", filename="photo.jpg")
        msg = _make_message(content="", attachments=[pdf, img])
        assert _has_embeddable_content(msg) is True

    def test_image_jpeg_variant_returns_true(self):
        """Content types like 'image/jpeg' (not just 'image/png') are accepted."""
        img = _make_attachment(content_type="image/jpeg")
        msg = _make_message(content="", attachments=[img])
        assert _has_embeddable_content(msg) is True

    def test_video_attachment_not_treated_as_image(self):
        """'video/mp4' does not start with 'image/' so it is not embeddable."""
        vid = _make_attachment(content_type="video/mp4", filename="clip.mp4")
        msg = _make_message(content="", attachments=[vid])
        assert _has_embeddable_content(msg) is False


# ---------------------------------------------------------------------------
# Tests for the early-return path in _process_message()
# ---------------------------------------------------------------------------


class TestProcessMessageEarlyReturn:
    @pytest.mark.asyncio
    async def test_no_embeddable_content_calls_mark_completed_and_returns(self):
        """When _has_embeddable_content() is False, mark_completed is called immediately."""
        bot = _make_bot()

        # Message with empty content and no attachments — not embeddable
        msg = MagicMock()
        msg.id = 1
        msg.content = ""
        msg.attachments = []
        msg.channel.id = 99

        mock_store = MagicMock()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot._process_message(msg)

        mock_store.mark_completed.assert_called_once_with("1")

    @pytest.mark.asyncio
    async def test_no_embeddable_content_does_not_call_agent(self):
        """When _has_embeddable_content() is False, the LLM agent is never invoked."""
        bot = _make_bot()

        msg = MagicMock()
        msg.id = 2
        msg.content = ""
        msg.attachments = []
        msg.channel.id = 99

        mock_store = MagicMock()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot._process_message(msg)

        bot.agent.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_embeddable_content_does_not_save_message(self):
        """When _has_embeddable_content() is False, save_message is never called."""
        bot = _make_bot()

        msg = MagicMock()
        msg.id = 3
        msg.content = ""
        msg.attachments = []
        msg.channel.id = 99

        mock_store = MagicMock()
        mock_store.save_message = AsyncMock()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot._process_message(msg)

        mock_store.save_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_only_content_triggers_early_return(self):
        """Whitespace-only content is treated as empty — triggers early-return path."""
        bot = _make_bot()

        msg = MagicMock()
        msg.id = 4
        msg.content = "   "
        msg.attachments = []
        msg.channel.id = 99

        mock_store = MagicMock()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot._process_message(msg)

        mock_store.mark_completed.assert_called_once_with("4")
        bot.agent.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_image_attachment_only_triggers_early_return(self):
        """A message with only a PDF attachment triggers the early-return path."""
        bot = _make_bot()

        pdf = _make_attachment(content_type="application/pdf", filename="report.pdf")
        msg = MagicMock()
        msg.id = 5
        msg.content = ""
        msg.attachments = [pdf]
        msg.channel.id = 99

        mock_store = MagicMock()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot._process_message(msg)

        mock_store.mark_completed.assert_called_once_with("5")
        bot.agent.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_embeddable_message_does_not_early_return(self):
        """A message with text content does NOT trigger the early-return path."""
        bot = _make_bot()

        msg = MagicMock()
        msg.id = 6
        msg.content = "hello"
        msg.attachments = []
        msg.author.bot = False
        msg.author.id = 42
        msg.author.display_name = "TestUser"
        msg.channel.id = 99
        msg.mentions = []
        msg.reference = None
        msg.reply = AsyncMock(return_value=MagicMock(id=200))
        msg.channel.typing = MagicMock(return_value=_async_cm())

        mock_store = AsyncMock()
        mock_store.save_message = AsyncMock()
        mock_store.get_recent = MagicMock(return_value=[])
        mock_store.get_attachments = MagicMock(return_value={})
        mock_store.get_channel_summary = MagicMock(return_value=None)
        mock_store.get_user_summaries_for_users = MagicMock(return_value=[])
        mock_store.mark_completed = MagicMock()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot._process_message(msg)

        # save_message should have been called (message was stored)
        mock_store.save_message.assert_called()


class TestExplicitSummonsIsLive:
    """An explicit summons (a direct @mention / reply to Bosun) routed to the
    ambient chat path must be marked live=True so the no_reply tool and the
    post-generation send-gate cannot silently eat it (the ep-236 regression)."""

    def _make_msg(self):
        msg = MagicMock()
        msg.id = 7
        msg.content = "<@1> the site is cool, true or false?"
        msg.attachments = []
        msg.author.bot = False
        msg.author.id = 42
        msg.author.display_name = "TestUser"
        msg.channel.id = 99
        msg.mentions = []
        msg.reference = None
        msg.reply = AsyncMock(return_value=MagicMock(id=200))
        msg.channel.typing = MagicMock(return_value=_async_cm())
        return msg

    def _make_store(self):
        store = AsyncMock()
        store.save_message = AsyncMock()
        store.mark_completed = MagicMock()
        return store

    async def _run(self, *, force_respond: bool, explicit: bool):
        bot = _make_bot()
        msg = self._make_msg()
        mock_store = self._make_store()
        stream = AsyncMock(return_value=(None, "", None))
        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
            patch("chat.bot.download_image_attachments", AsyncMock(return_value=[])),
            patch.object(bot, "_stream_response", stream),
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot._process_message(
                msg, force_respond=force_respond, explicit=explicit
            )
        return stream

    @pytest.mark.asyncio
    async def test_explicit_ambient_engage_is_live(self):
        stream = await self._run(force_respond=True, explicit=True)
        stream.assert_called_once()
        assert stream.call_args.kwargs["live"] is True

    @pytest.mark.asyncio
    async def test_soft_ambient_engage_stays_suppressible(self):
        stream = await self._run(force_respond=True, explicit=False)
        stream.assert_called_once()
        assert stream.call_args.kwargs["live"] is False
