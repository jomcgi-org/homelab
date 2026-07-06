"""Tests for thinking mode handling in the Discord bot."""

from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import discord


@pytest.fixture(autouse=True)
def _route_mentions_to_chat():
    """on_message now runs the depth+repo classify for every engaged message
    (ADR 035 follow-up: unified entry points). These tests exercise the inline
    chat reply path for a mention, so stub the classify to "chat" and the ACL
    reads to empty, keeping routing deterministic and offline."""
    with (
        patch("chat.bot.acl.ambient_channels", return_value=set()),
        patch("chat.bot.acl.allowed_scopes", return_value=set()),
        patch(
            "chat.bot.attention.classify_engagement",
            AsyncMock(return_value=SimpleNamespace(needs_agent=False, repo="")),
        ),
    ):
        yield


from pydantic_ai import (
    PartDeltaEvent,
    TextPartDelta,
    ThinkingPartDelta,
)

from chat.bot import _truncate_thinking, BotMessageView, ChatBot


class TestTruncateThinking:
    def test_short_thinking_returned_as_is(self):
        """Thinking under 2000 chars is not truncated."""
        assert _truncate_thinking("short reasoning") == "short reasoning"

    def test_long_thinking_truncated(self):
        """Thinking over 2000 chars is truncated with suffix."""
        long_text = "x" * 2500
        result = _truncate_thinking(long_text)
        assert len(result) <= 2000
        assert result.endswith("... (truncated)")

    def test_exactly_2000_chars_not_truncated(self):
        """Thinking at exactly 2000 chars passes through."""
        text = "x" * 2000
        assert _truncate_thinking(text) == text


class TestBotMessageView:
    def test_view_with_thinking_has_two_buttons(self):
        """BotMessageView with thinking_text shows both buttons."""
        view = BotMessageView("response text", thinking_text="some thinking")
        buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
        labels = {b.label for b in buttons}
        assert labels == {"Show thinking", "Get your facts STR8!"}

    def test_view_without_thinking_has_one_button(self):
        """BotMessageView without thinking_text shows only the fact-check button."""
        view = BotMessageView("response text")
        buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
        assert len(buttons) == 1
        assert buttons[0].label == "Get your facts STR8!"

    def test_show_thinking_button_is_secondary(self):
        """Show thinking button uses secondary (gray) style."""
        view = BotMessageView("response", thinking_text="reasoning")
        button = next(
            b
            for b in view.children
            if isinstance(b, discord.ui.Button) and b.label == "Show thinking"
        )
        assert button.style == discord.ButtonStyle.secondary

    def test_fact_check_button_is_danger(self):
        """Fact-check button uses danger (red) style."""
        view = BotMessageView("response", thinking_text="reasoning")
        button = next(
            b
            for b in view.children
            if isinstance(b, discord.ui.Button) and b.label == "Get your facts STR8!"
        )
        assert button.style == discord.ButtonStyle.danger

    def test_view_no_timeout(self):
        """BotMessageView has no timeout."""
        view = BotMessageView("response", thinking_text="some thinking")
        assert view.timeout is None

    def test_show_thinking_button_has_custom_id(self):
        """show_thinking button has fixed custom_id for add_view() persistence."""
        view = BotMessageView("response", thinking_text="some thinking")
        button = next(
            b
            for b in view.children
            if isinstance(b, discord.ui.Button) and b.label == "Show thinking"
        )
        assert button.custom_id == "show_thinking"

    def test_fact_check_button_has_custom_id(self):
        """fact_check button has fixed custom_id for add_view() persistence."""
        view = BotMessageView("response", thinking_text="some thinking")
        button = next(
            b
            for b in view.children
            if isinstance(b, discord.ui.Button) and b.label == "Get your facts STR8!"
        )
        assert button.custom_id == "fact_check"

    @pytest.mark.asyncio
    async def test_show_thinking_sends_ephemeral(self):
        """Clicking Show thinking sends thinking text as an ephemeral message."""
        view = BotMessageView("response text", thinking_text="my reasoning")
        button = next(
            b
            for b in view.children
            if isinstance(b, discord.ui.Button) and b.label == "Show thinking"
        )

        interaction = AsyncMock()
        interaction.response = AsyncMock()
        interaction.response.send_message = AsyncMock()

        await button.callback(interaction)

        interaction.response.send_message.assert_called_once_with(
            "my reasoning", ephemeral=True
        )


# Helpers for integration tests


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


def _make_message(content="hello", mentions=None, msg_id=1):
    msg = MagicMock()
    msg.id = msg_id
    msg.content = content
    msg.author.bot = False
    msg.author.id = 42
    msg.author.display_name = "TestUser"
    msg.channel.id = 99
    msg.channel.typing = MagicMock(return_value=_async_cm())
    msg.mentions = mentions if mentions is not None else []
    msg.reference = None
    msg.attachments = []
    msg.embeds = []
    sent = MagicMock(id=100)
    sent.edit = AsyncMock()
    msg.reply = AsyncMock(return_value=sent)
    return msg


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


def _thinking_delta(content: str) -> PartDeltaEvent:
    return PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=content))


async def _async_iter(events):
    for e in events:
        yield e


class TestThinkingIntegration:
    @pytest.mark.asyncio
    async def test_response_with_thinking_adds_view_with_thinking(self):
        """When model returns thinking, final edit includes BotMessageView with thinking."""
        bot = _make_bot()
        bot_user = bot.user

        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        events = [
            _thinking_delta("reasoning here"),
            _text_delta("Hello!"),
        ]
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

        sent = message.reply.return_value
        final_edit = sent.edit.call_args_list[-1]
        view = final_edit.kwargs.get("view")
        assert isinstance(view, BotMessageView)
        assert view.thinking_text == "reasoning here"
        # Verify thinking was passed to save_message for the bot response
        bot_save_call = mock_store.save_message.call_args_list[-1]
        assert bot_save_call.kwargs.get("thinking") == "reasoning here"

    @pytest.mark.asyncio
    async def test_response_without_thinking_still_has_view(self):
        """When model returns plain text, final edit still has a BotMessageView (fact-check button)."""
        bot = _make_bot()
        bot_user = bot.user

        message = _make_message(content="Hi", mentions=[bot_user])
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
            await bot.on_message(message)

        sent = message.reply.return_value
        final_edit = sent.edit.call_args_list[-1]
        view = final_edit.kwargs.get("view")
        assert isinstance(view, BotMessageView)
        assert view.thinking_text is None

    @pytest.mark.asyncio
    async def test_empty_stream_sends_fallback(self):
        """When stream produces no events, fallback message is sent."""
        bot = _make_bot()
        bot_user = bot.user

        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        bot.agent.run_stream_events = MagicMock(return_value=_async_iter([]))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        reply_text = message.reply.call_args_list[0][0][0]
        assert "Sorry" in reply_text
        assert "trouble" in reply_text

    @pytest.mark.asyncio
    async def test_thinking_only_no_text_sends_fallback(self):
        """When stream produces only thinking events (no text), fallback is sent."""
        bot = _make_bot()
        bot_user = bot.user

        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        events = [
            _thinking_delta("just reasoning"),
        ]
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

        # The sent message should be edited to fallback since no text was produced
        sent = message.reply.return_value
        last_edit = sent.edit.call_args_list[-1]
        fallback = last_edit.kwargs.get("content", "")
        assert "Sorry" in fallback
        assert "trouble" in fallback

    @pytest.mark.asyncio
    async def test_literal_think_tag_passes_through_unchanged(self):
        """Output with '<think>' as literal text is not stripped."""
        bot = _make_bot()
        bot_user = bot.user

        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        literal_output = "Use <think> tags to structure your reasoning."
        events = [_text_delta(literal_output)]
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

        sent = message.reply.return_value
        last_edit = sent.edit.call_args_list[-1]
        assert last_edit.kwargs.get("content") == literal_output


class TestOnReadyViewRegistration:
    @pytest.mark.asyncio
    async def test_on_ready_registers_views_for_recent_bot_messages(self):
        """on_ready calls add_view for each recent bot message."""
        bot = _make_bot()

        msg1 = MagicMock()
        msg1.discord_message_id = "111"
        msg1.content = "response A"
        msg1.thinking = "thought A"
        msg2 = MagicMock()
        msg2.discord_message_id = "222"
        msg2.content = "response B"
        msg2.thinking = None

        mock_store = MagicMock()
        mock_store.get_recent_bot_messages.return_value = [msg1, msg2]

        bot.add_view = MagicMock()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_ready()

        assert bot.add_view.call_count == 2
        calls = bot.add_view.call_args_list
        assert calls[0].kwargs["message_id"] == 111
        assert calls[1].kwargs["message_id"] == 222
        assert isinstance(calls[0].args[0], BotMessageView)
        assert isinstance(calls[1].args[0], BotMessageView)
        assert calls[0].args[0].thinking_text == "thought A"
        assert calls[1].args[0].thinking_text is None

    @pytest.mark.asyncio
    async def test_on_ready_no_views_when_no_bot_messages(self):
        """on_ready does not call add_view when there are no stored bot messages."""
        bot = _make_bot()

        mock_store = MagicMock()
        mock_store.get_recent_bot_messages.return_value = []

        bot.add_view = MagicMock()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_ready()

        bot.add_view.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_ready_store_failure_does_not_crash(self):
        """on_ready swallows store errors so the bot still starts."""
        bot = _make_bot()
        bot.add_view = MagicMock()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", side_effect=Exception("db error")),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            # Should not raise
            await bot.on_ready()

        bot.add_view.assert_not_called()
