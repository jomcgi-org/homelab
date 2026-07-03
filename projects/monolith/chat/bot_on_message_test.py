"""Additional coverage for ChatBot -- on_message(), on_ready(), streaming response."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai import (
    PartDeltaEvent,
    TextPartDelta,
    ThinkingPartDelta,
)

from chat.bot import ChatBot, create_bot, should_respond


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_message(
    content: str = "hello",
    author_bot: bool = False,
    channel_id: int = 99,
    msg_id: int = 1,
    mentions: list | None = None,
    reference=None,
) -> MagicMock:
    msg = MagicMock()
    msg.id = msg_id
    msg.content = content
    msg.author.bot = author_bot
    msg.author.id = 42
    msg.author.display_name = "TestUser"
    msg.channel.id = channel_id
    msg.channel.typing = MagicMock(return_value=_async_cm())
    msg.mentions = mentions if mentions is not None else []
    msg.reference = reference
    msg.attachments = []
    msg.embeds = []
    sent = MagicMock(id=100)
    sent.edit = AsyncMock()
    msg.reply = AsyncMock(return_value=sent)
    return msg


class _AsyncCtxManager:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _async_cm():
    return _AsyncCtxManager()


def _make_bot() -> ChatBot:
    """Build a ChatBot with mocked internals so it never touches real services."""
    with (
        patch("chat.bot.EmbeddingClient") as mock_ec,
        patch("chat.bot.create_agent") as mock_ca,
    ):
        mock_ec.return_value = AsyncMock()
        mock_ca.return_value = MagicMock()
        bot = ChatBot()
    # Patch the internal user reference
    bot._connection = MagicMock()
    bot._connection.user = MagicMock()
    bot._connection.user.id = 999
    bot._connection.user.display_name = "BotUser"
    return bot


def _make_store():
    """Create a mock MessageStore with standard defaults."""
    mock_store = AsyncMock()
    mock_store.save_message = AsyncMock()
    mock_store.get_recent = MagicMock(return_value=[])
    mock_store.get_attachments = MagicMock(return_value={})
    mock_store.get_channel_summary = MagicMock(return_value=None)
    mock_store.get_user_summaries_for_users = MagicMock(return_value=[])
    mock_store.acquire_lock = MagicMock(return_value=True)
    mock_store.mark_completed = MagicMock()
    return mock_store


def _text_delta(content: str) -> PartDeltaEvent:
    return PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=content))


async def _async_iter(events):
    for e in events:
        yield e


# ---------------------------------------------------------------------------
# should_respond edge cases (reference with no .resolved attribute)
# ---------------------------------------------------------------------------


class TestShouldRespondEdgeCases:
    def test_reference_without_resolved_attr(self):
        """Reference object without a resolved attribute is handled gracefully."""
        message = MagicMock()
        message.author.bot = False
        message.mentions = []
        bot_user = MagicMock()
        bot_user.id = 12345
        # reference present but has no 'resolved' attribute
        reference = MagicMock(spec=[])
        message.reference = reference
        assert should_respond(message, bot_user) is False

    def test_reference_resolved_is_none(self):
        """Reference with resolved=None does not trigger a response."""
        message = MagicMock()
        message.author.bot = False
        message.mentions = []
        bot_user = MagicMock()
        bot_user.id = 12345
        reference = MagicMock()
        reference.resolved = None
        message.reference = reference
        assert should_respond(message, bot_user) is False


# ---------------------------------------------------------------------------
# ChatBot.on_ready
# ---------------------------------------------------------------------------


class TestOnReady:
    @pytest.mark.asyncio
    async def test_on_ready_logs_without_error(self):
        """on_ready() completes without raising even with a mock user."""
        bot = _make_bot()
        # _make_bot() already sets bot._connection.user; user is a read-only property
        bot._connection.user.__str__ = MagicMock(return_value="BotUser#0001")
        await bot.on_ready()  # should not raise


# ---------------------------------------------------------------------------
# ChatBot.on_message -- store-always branch
# ---------------------------------------------------------------------------


class TestOnMessageStoreAlways:
    @pytest.mark.asyncio
    async def test_stores_every_message_even_when_not_responding(self):
        """on_message always calls save_message regardless of should_respond."""
        bot = _make_bot()

        message = _make_message(author_bot=False, mentions=[])
        message.reference = None

        mock_store = _make_store()

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

        mock_store.save_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_swallows_store_exception(self):
        """on_message does not propagate exceptions from the store phase."""
        bot = _make_bot()

        message = _make_message(author_bot=False, mentions=[])
        message.reference = None

        mock_store = _make_store()
        mock_store.save_message = AsyncMock(side_effect=RuntimeError("db down"))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            # Should not raise
            await bot.on_message(message)


# ---------------------------------------------------------------------------
# ChatBot.on_message -- should_respond guard
# ---------------------------------------------------------------------------


class TestOnMessageShouldRespondGuard:
    @pytest.mark.asyncio
    async def test_does_not_reply_to_bot_messages(self):
        """on_message returns early and does not call reply for bot-authored messages."""
        bot = _make_bot()

        message = _make_message(author_bot=True)

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore") as mock_store_cls,
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_store_cls.return_value.save_message = AsyncMock()
            await bot.on_message(message)

        message.reply.assert_not_called()


# ---------------------------------------------------------------------------
# ChatBot.on_message -- streaming generate + reply branch
# ---------------------------------------------------------------------------


class TestOnMessageGenerateReply:
    @pytest.mark.asyncio
    async def test_replies_when_mentioned(self):
        """on_message sends a reply when the bot is mentioned (streaming)."""
        bot = _make_bot()
        bot._connection.user.id = 999
        bot._connection.user.display_name = "BotUser"
        bot_user = bot.user

        message = _make_message(content="Hey bot!", mentions=[bot_user])
        message.reference = None
        mock_store = _make_store()

        events = [_text_delta("Hello human!")]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        # Initial reply was sent
        message.reply.assert_called()
        first_reply_text = message.reply.call_args_list[0][0][0]
        assert "Hello human!" in first_reply_text

    @pytest.mark.asyncio
    async def test_swallows_reply_exception(self):
        """on_message does not propagate exceptions from the streaming/reply phase."""
        bot = _make_bot()
        bot._connection.user.id = 999
        bot._connection.user.display_name = "BotUser"
        bot_user = bot.user

        message = _make_message(content="Hey bot!", mentions=[bot_user])
        message.reference = None
        message.reply = AsyncMock(side_effect=RuntimeError("discord error"))

        mock_store = _make_store()

        events = [_text_delta("Hello!")]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            # Should not raise
            await bot.on_message(message)


# ---------------------------------------------------------------------------
# ChatBot -- streaming response includes recent context
# ---------------------------------------------------------------------------


class TestStreamResponseContext:
    @pytest.mark.asyncio
    async def test_includes_recent_messages_in_prompt(self):
        """Streaming response calls agent.run_stream_events with recent conversation context."""
        from chat.models import Message

        bot = _make_bot()
        bot._connection.user.id = 999
        bot_user = bot.user

        msg = _make_message(content="What is the weather?", mentions=[bot_user])

        recent_msg = Message(
            id=1,
            discord_message_id="1",
            channel_id="99",
            user_id="u1",
            username="Alice",
            content="recent message",
            is_bot=False,
            embedding=[0.0] * 1024,
            created_at=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
        )

        mock_store = _make_store()
        mock_store.get_recent = MagicMock(return_value=[recent_msg])

        events = [_text_delta("Sunny!")]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(msg)

        # Verify run_stream_events was called with recent context in prompt
        prompt_arg = bot.agent.run_stream_events.call_args[0][0]
        assert "recent message" in prompt_arg
        # Verify deps were passed
        assert "deps" in bot.agent.run_stream_events.call_args[1]


# ---------------------------------------------------------------------------
# ChatBot._maybe_handle_goosecracker_reply -- agent-thread ACL gate + steering
# (ADR 035 Phase 2)
# ---------------------------------------------------------------------------


def _make_goosecracker_mock(**overrides) -> MagicMock:
    """A chat.bot.goosecracker stand-in for an agent thread mid-turn."""
    mock = MagicMock()
    mock.is_goosecracker_thread = MagicMock(return_value=True)
    mock.is_agent_thread = MagicMock(return_value=True)
    mock.session_scope = MagicMock(return_value="homelab")
    mock.is_owner = MagicMock(return_value=False)
    mock.build_roast = AsyncMock(return_value="Nice try.")
    mock.continue_session = MagicMock(return_value={"action": "steering"})
    mock.REACTION_RUNNING = "\U0001f440"
    mock.REACTION_QUEUED = "⏳"
    for key, value in overrides.items():
        setattr(mock, key, value)
    return mock


class TestGoosecrackerReplySteering:
    @pytest.mark.asyncio
    async def test_permitted_user_steering_gets_running_reaction(self):
        """A user holding the agent grant for the thread's repo steers a running
        turn: 👀 reaction, no text reply, continue_session is called."""
        bot = _make_bot()
        message = _make_message(content="also add tests", channel_id=555, msg_id=7)
        message.add_reaction = AsyncMock()

        mock_gc = _make_goosecracker_mock()
        mock_acl = MagicMock()
        mock_acl.is_granted = MagicMock(return_value=True)

        with (
            patch("chat.bot.goosecracker", mock_gc),
            patch("chat.bot.acl", mock_acl),
            patch.object(bot, "_complete_lock", MagicMock()),
        ):
            handled = await bot._maybe_handle_goosecracker_reply(message)

        assert handled is True
        mock_gc.continue_session.assert_called_once()
        message.add_reaction.assert_called_once_with(mock_gc.REACTION_RUNNING)
        message.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_unpermitted_user_gets_refusal_and_nothing_enqueued(self):
        """A user without the agent grant for the thread's repo gets a refusal
        reply naming the missing grant; continue_session is never called."""
        bot = _make_bot()
        message = _make_message(content="also add tests", channel_id=555, msg_id=7)
        message.add_reaction = AsyncMock()

        mock_gc = _make_goosecracker_mock()
        mock_acl = MagicMock()
        mock_acl.is_granted = MagicMock(return_value=False)
        mock_acl.allowed_scopes = MagicMock(return_value={"otherrepo"})

        with (
            patch("chat.bot.goosecracker", mock_gc),
            patch("chat.bot.acl", mock_acl),
            patch.object(bot, "_complete_lock", MagicMock()),
        ):
            handled = await bot._maybe_handle_goosecracker_reply(message)

        assert handled is True
        mock_gc.continue_session.assert_not_called()
        message.reply.assert_called_once()
        refusal = message.reply.call_args[0][0]
        assert "homelab" in refusal
        assert "otherrepo" in refusal
        message.add_reaction.assert_not_called()


# ---------------------------------------------------------------------------
# create_bot
# ---------------------------------------------------------------------------


class TestCreateBot:
    def test_returns_chatbot_instance(self):
        """create_bot() returns a ChatBot."""
        with (
            patch("chat.bot.EmbeddingClient"),
            patch("chat.bot.create_agent"),
        ):
            bot = create_bot()
        assert isinstance(bot, ChatBot)
