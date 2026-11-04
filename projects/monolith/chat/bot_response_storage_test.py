"""Tests for response storage after streaming refactor.

Covers:
1. on_message(): after a successful streaming reply the bot stores its own response;
   the second save_message() call must receive is_bot=True, the correct content, and
   the sent message id.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai import (
    PartDeltaEvent,
    TextPartDelta,
)

from chat.bot import (
    ChatBot,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _AsyncCtxManager:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _async_cm():
    return _AsyncCtxManager()


def _make_message(
    content: str = "hello",
    channel_id: int = 99,
    msg_id: int = 1,
    mentions: list | None = None,
) -> MagicMock:
    msg = MagicMock()
    msg.id = msg_id
    msg.content = content
    msg.author.bot = False
    msg.author.id = 42
    msg.author.display_name = "TestUser"
    msg.channel.id = channel_id
    msg.channel.typing = MagicMock(return_value=_async_cm())
    msg.mentions = mentions if mentions is not None else []
    msg.reference = None
    msg.attachments = []
    msg.embeds = []
    sent = MagicMock(id=100)
    sent.edit = AsyncMock()
    msg.reply = AsyncMock(return_value=sent)
    return msg


def _make_bot() -> ChatBot:
    """Build a ChatBot with mocked internals."""
    with (
        patch("chat.bot.EmbeddingClient") as mock_ec,
        patch("chat.bot.VisionClient"),
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


def _make_store() -> MagicMock:
    """Return a store mock wired with empty recent/attachments."""
    store = MagicMock()
    store.get_recent = MagicMock(return_value=[])
    store.get_attachments = MagicMock(return_value={})
    store.get_channel_summary = MagicMock(return_value=None)
    store.get_user_summaries_for_users = MagicMock(return_value=[])
    store.save_message = AsyncMock()
    store.acquire_lock = MagicMock(return_value=True)
    store.mark_completed = MagicMock()
    return store


def _text_delta(content: str) -> PartDeltaEvent:
    return PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=content))


async def _async_iter(events):
    for e in events:
        yield e


# ---------------------------------------------------------------------------
# Bot response stored with correct arguments after streaming
# ---------------------------------------------------------------------------


class TestOnMessageBotResponseStorage:
    @pytest.mark.asyncio
    async def test_bot_response_saved_with_is_bot_true(self):
        """After a successful streaming reply, on_message saves the bot response with is_bot=True."""
        bot = _make_bot()
        bot_user = bot.user

        sent_msg = MagicMock(id=888)
        sent_msg.edit = AsyncMock()
        message = _make_message(content="Hey bot!", mentions=[bot_user])
        message.reply = AsyncMock(return_value=sent_msg)

        mock_store = _make_store()

        events = [_text_delta("My answer!")]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        # save_message is called twice: once for user message, once for bot response
        assert mock_store.save_message.call_count == 2
        # The second call is the bot's own response
        bot_save_kwargs = mock_store.save_message.call_args_list[1][1]
        assert bot_save_kwargs["is_bot"] is True

    @pytest.mark.asyncio
    async def test_bot_response_saved_with_correct_content(self):
        """The bot response stored in the second save_message call matches the streamed text."""
        bot = _make_bot()
        bot_user = bot.user

        sent_msg = MagicMock(id=777)
        sent_msg.edit = AsyncMock()
        message = _make_message(content="Hey bot!", mentions=[bot_user])
        message.reply = AsyncMock(return_value=sent_msg)

        mock_store = _make_store()

        events = [_text_delta("Specific "), _text_delta("response text")]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        bot_save_kwargs = mock_store.save_message.call_args_list[1][1]
        assert bot_save_kwargs["content"] == "Specific response text"

    @pytest.mark.asyncio
    async def test_bot_response_saved_with_sent_message_id(self):
        """The discord_message_id in the second save_message call is the sent message's id."""
        bot = _make_bot()
        bot_user = bot.user

        sent_msg = MagicMock(id=12345)
        sent_msg.edit = AsyncMock()
        message = _make_message(content="Hey bot!", mentions=[bot_user])
        message.reply = AsyncMock(return_value=sent_msg)

        mock_store = _make_store()

        events = [_text_delta("A response")]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        bot_save_kwargs = mock_store.save_message.call_args_list[1][1]
        assert bot_save_kwargs["discord_message_id"] == str(sent_msg.id)

    @pytest.mark.asyncio
    async def test_bot_response_saved_with_bot_user_id(self):
        """The user_id in the second save_message call is the bot's own user id."""
        bot = _make_bot()
        bot._connection.user.id = 999
        bot_user = bot.user

        sent_msg = MagicMock(id=100)
        sent_msg.edit = AsyncMock()
        message = _make_message(content="Hey bot!", mentions=[bot_user])
        message.reply = AsyncMock(return_value=sent_msg)

        mock_store = _make_store()

        events = [_text_delta("Response!")]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        bot_save_kwargs = mock_store.save_message.call_args_list[1][1]
        assert bot_save_kwargs["user_id"] == str(bot._connection.user.id)
