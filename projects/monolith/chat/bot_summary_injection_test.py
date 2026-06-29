"""Tests for summary injection into agent context via the streaming flow.

Verifies that channel and user summaries are fetched from the store and
prepended to the prompt passed to run_stream_events.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai import PartDeltaEvent, TextPartDelta

from chat.bot import ChatBot
from chat.models import ChannelSummary, UserChannelSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AsyncCtxManager:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _async_cm():
    return _AsyncCtxManager()


def _text_delta(content: str) -> PartDeltaEvent:
    return PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=content))


async def _async_iter(events):
    for e in events:
        yield e


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


def _make_store(channel_summary=None, user_summaries=None):
    mock_store = AsyncMock()
    mock_store.save_message = AsyncMock()
    mock_store.get_recent = MagicMock(return_value=[])
    mock_store.get_attachments = MagicMock(return_value={})
    mock_store.get_channel_summary = MagicMock(return_value=channel_summary)
    mock_store.get_user_summaries_for_users = MagicMock(
        return_value=user_summaries or []
    )
    mock_store.acquire_lock = MagicMock(return_value=True)
    mock_store.mark_completed = MagicMock()
    return mock_store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestChannelSummaryInjection:
    @pytest.mark.asyncio
    async def test_channel_summary_appears_in_prompt(self):
        """When a channel summary exists, its text is included in the prompt."""
        bot = _make_bot()
        bot_user = bot.user
        msg = _make_message(content="What's up?", mentions=[bot_user])

        cs = ChannelSummary(
            id=1,
            channel_id="99",
            summary="This channel discusses homelab infrastructure.",
            message_count=50,
            last_message_id=100,
        )
        mock_store = _make_store(channel_summary=cs)

        events = [_text_delta("Not much!")]
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
            await bot.on_message(msg)

        prompt_arg = bot.agent.run_stream_events.call_args[0][0]
        assert (
            "[Channel context: This channel discusses homelab infrastructure.]"
            in prompt_arg
        )


class TestUserSummaryInjection:
    @pytest.mark.asyncio
    async def test_user_summaries_appear_in_prompt(self):
        """When user summaries exist, they are included in the prompt."""
        bot = _make_bot()
        bot_user = bot.user
        msg = _make_message(content="Hello", mentions=[bot_user])

        user_sums = [
            UserChannelSummary(
                id=1,
                channel_id="99",
                user_id="42",
                username="Alice",
                summary="Interested in Kubernetes and Go.",
            ),
            UserChannelSummary(
                id=2,
                channel_id="99",
                user_id="43",
                username="Bob",
                summary="Asks about Python packaging.",
            ),
        ]
        mock_store = _make_store(user_summaries=user_sums)

        events = [_text_delta("Hi there!")]
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
            await bot.on_message(msg)

        prompt_arg = bot.agent.run_stream_events.call_args[0][0]
        assert "[People in this conversation:" in prompt_arg
        assert " - Alice: Interested in Kubernetes and Go." in prompt_arg
        assert " - Bob: Asks about Python packaging." in prompt_arg


class TestNoSummariesGracefulSkip:
    @pytest.mark.asyncio
    async def test_no_summaries_no_header(self):
        """When no summaries exist, the prompt has no summary header."""
        bot = _make_bot()
        bot_user = bot.user
        msg = _make_message(content="Hey", mentions=[bot_user])

        mock_store = _make_store(channel_summary=None, user_summaries=[])

        events = [_text_delta("Hey back!")]
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
            await bot.on_message(msg)

        prompt_arg = bot.agent.run_stream_events.call_args[0][0]
        assert "[Channel context:" not in prompt_arg
        assert "[People in this conversation:" not in prompt_arg
        # The identity note is always injected first, then the conversation.
        assert prompt_arg.startswith("[Your identity here:")
        assert "Recent conversation:" in prompt_arg


class TestIdentityInjection:
    @pytest.mark.asyncio
    async def test_bot_identity_appears_in_prompt(self):
        """The bot's live display name and user ID are injected into the prompt."""
        bot = _make_bot()
        bot_user = bot.user
        msg = _make_message(content="who are you", mentions=[bot_user])

        mock_store = _make_store()

        events = [_text_delta("I'm Bosun.")]
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
            await bot.on_message(msg)

        prompt_arg = bot.agent.run_stream_events.call_args[0][0]
        # _make_bot sets user id 999 / display name "BotUser".
        assert '"BotUser"' in prompt_arg
        assert "Discord user ID 999" in prompt_arg
        assert "<@999>" in prompt_arg
