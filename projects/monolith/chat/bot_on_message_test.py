"""Additional coverage for ChatBot -- on_message(), on_ready(), streaming response."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from pydantic_ai import (
    PartDeltaEvent,
    TextPartDelta,
    ThinkingPartDelta,
)
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from agent_sessions import api as agent_session_api
from agent_sessions import mcp as agent_session_mcp
from agent_sessions import store as agent_session_store
from agent_sessions.models import AgentSession
from agent_sessions.transport import Turn
from chat.bot import ChatBot, create_bot, should_respond
from chat.models import ReactionEvent


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
    # Ack-first (ADR 035 Task 8): on_message awaits message.add_reaction on the
    # agent path, so it must be awaitable on every message mock.
    msg.add_reaction = AsyncMock()
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
# ChatBot._maybe_handle_agent_thread_reply -- owner-only agent-session routing
# ---------------------------------------------------------------------------


def _make_goosecracker_mock(**overrides) -> MagicMock:
    """A chat.bot.goosecracker stand-in for an agent thread mid-turn."""
    mock = MagicMock()
    mock.is_goosecracker_thread = MagicMock(return_value=True)
    mock.session_scope = MagicMock(return_value="homelab")
    mock.is_owner = MagicMock(return_value=False)
    mock.build_roast = AsyncMock(return_value="Nice try.")
    mock.continue_session = MagicMock(return_value={"action": "steering"})
    mock.REACTION_RUNNING = "\U0001f440"
    mock.REACTION_QUEUED = "⏳"
    for key, value in overrides.items():
        setattr(mock, key, value)
    return mock


class TestAgentSessionReplyOwnership:
    @pytest.mark.asyncio
    async def test_owner_reply_is_queued_for_the_bound_session(
        self, monkeypatch, engine
    ):
        """Only the configured owner can queue a follow-up agent turn."""
        for module in (agent_session_api, agent_session_mcp, agent_session_store):
            monkeypatch.setattr(module, "get_engine", lambda: engine)
        with Session(engine) as session:
            agent_session_store.create_session(
                session,
                "bound-thread",
                "<guest>",
                "main",
                "luna",
                discord_thread="555",
            )
        bot = _make_bot()
        message = _make_message(content="also add tests", channel_id=555, msg_id=7)
        message.add_reaction = AsyncMock()

        with (
            patch("chat.bot.acl.is_owner", return_value=True),
            patch(
                "agent_sessions.api.send_to_thread_session",
                AsyncMock(return_value={"action": "queued"}),
            ) as send,
            patch.object(bot, "_complete_lock", MagicMock()),
        ):
            handled = await bot._maybe_handle_agent_thread_reply(message)

        assert handled is True
        send.assert_awaited_once_with("555", "also add tests")
        message.add_reaction.assert_awaited_once_with("⏳")
        message.reply.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_owner_gets_owner_only_refusal_and_nothing_enqueued(self):
        """A non-owner cannot continue a bound session, regardless of repo ACLs."""
        bot = _make_bot()
        message = _make_message(content="also add tests", channel_id=555, msg_id=7)
        message.add_reaction = AsyncMock()

        with (
            patch("chat.bot.acl.is_owner", return_value=False),
            patch("agent_sessions.api.session_id_for_thread", return_value=3),
            patch("agent_sessions.api.send_to_thread_session", AsyncMock()) as send,
            patch.object(bot, "_complete_lock", MagicMock()),
        ):
            handled = await bot._maybe_handle_agent_thread_reply(message)

        assert handled is True
        send.assert_not_awaited()
        message.reply.assert_awaited_once_with(
            "Only the configured owner can continue agent sessions."
        )
        message.add_reaction.assert_not_called()


@pytest.mark.asyncio
async def test_non_owner_ambient_engage_returns_refusal_without_session(
    monkeypatch, engine
):
    monkeypatch.setenv("OWNER_DISCORD_USER_ID", "999")
    bot = _make_bot()
    bot._complete_lock = MagicMock()
    bot.start_agent_flow = AsyncMock()
    message = _make_message(content="build it")
    message.channel = MagicMock(spec=discord.TextChannel)
    message.channel.id = 99
    message.channel.guild = None

    outcome = await bot._engage_agent(message)

    assert outcome is not None
    assert outcome.chat_reply == (
        "Only the configured owner can start or continue agent sessions."
    )
    bot.start_agent_flow.assert_not_awaited()
    with Session(engine) as session:
        assert len(session.exec(select(AgentSession)).all()) == 0
    # AMBIENT (no mention, no reply-to-bot): refused SILENTLY. A channel that
    # merely opted into ambient mode must not fill with refusals aimed at people
    # who were not addressing the bot.
    message.reply.assert_not_called()


@pytest.mark.asyncio
async def test_non_owner_explicit_mention_gets_spoken_refusal(monkeypatch, engine):
    """An EXPLICIT trigger is refused out loud, unlike the ambient case above."""
    monkeypatch.setenv("OWNER_DISCORD_USER_ID", "999")
    bot = _make_bot()
    bot._complete_lock = MagicMock()
    bot.start_agent_flow = AsyncMock()
    message = _make_message(content="build it", mentions=[bot.user])
    message.channel = MagicMock(spec=discord.TextChannel)
    message.channel.id = 99
    message.channel.guild = None

    outcome = await bot._engage_agent(message)

    bot.start_agent_flow.assert_not_awaited()
    message.reply.assert_awaited_once_with(outcome.chat_reply)
    with Session(engine) as session:
        assert len(session.exec(select(AgentSession)).all()) == 0


@pytest.mark.asyncio
async def test_non_owner_slash_agent_returns_refusal_without_session(
    monkeypatch, engine
):
    monkeypatch.setenv("OWNER_DISCORD_USER_ID", "999")
    bot = _make_bot()
    interaction = MagicMock()
    interaction.user.id = 42
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await bot._handle_agent_command(interaction, "build it", "")

    with Session(engine) as session:
        assert len(session.exec(select(AgentSession)).all()) == 0
    interaction.followup.send.assert_awaited_once_with(
        "Only the configured owner can start or continue agent sessions."
    )


@pytest.mark.asyncio
async def test_owner_slash_flow_creates_luna_session_bound_to_thread(
    monkeypatch, engine
):
    monkeypatch.setenv("OWNER_DISCORD_USER_ID", "42")
    for module in (agent_session_api, agent_session_mcp, agent_session_store):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setattr(agent_session_api, "_schedule_next_message", lambda _id: None)
    bot = _make_bot()
    bot._orchestrator_verdict = AsyncMock(
        return_value=__import__("chat.orchestrator", fromlist=["FailOpen"]).FailOpen(
            "test"
        )
    )
    channel = _flow_channel()
    thread = MagicMock(id=555, mention="<#555>")
    thread.send = AsyncMock()
    channel.create_thread = AsyncMock(return_value=thread)

    outcome = await bot.start_agent_flow(
        channel, SimpleNamespace(id=42, mention="<@42>"), "build it", "jomcgi/homelab"
    )

    assert outcome.thread is thread
    with Session(engine) as session:
        row = session.exec(select(AgentSession)).one()
        assert row.model == "luna"
        assert row.repo == "jomcgi/homelab"
        assert row.discord_thread == "555"


@pytest.mark.asyncio
async def test_unbound_agent_thread_reply_falls_through(monkeypatch, engine):
    for module in (agent_session_api, agent_session_mcp, agent_session_store):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    bot = _make_bot()
    message = _make_message(content="continue")
    message.channel.id = 555

    assert await bot._maybe_handle_agent_thread_reply(message) is False


@pytest.mark.asyncio
async def test_terminal_notification_routes_to_thread_or_default_channel(monkeypatch):
    calls = []

    async def notify(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(agent_session_mcp.agent_api, "notify", notify)
    turn = Turn("done", "completed", "end_turn", False, [], 1, "s", {}, 0, 1, [])
    bound = AgentSession(
        local_session_id="s", workspace="w", branch="main", discord_thread="555"
    )
    default = AgentSession(local_session_id="default", workspace="w", branch="main")

    await agent_session_mcp._notify_terminal(turn, "done", "completed", bound)
    await agent_session_mcp._notify_terminal(turn, "done", "completed", default)

    assert calls[0][1]["channel"] == "555"
    assert calls[1][1]["channel"] is None


# ---------------------------------------------------------------------------
# ChatBot.on_message -- attention gate (ADR 035 phase 3)
# ---------------------------------------------------------------------------


class TestAttentionGate:
    @pytest.mark.asyncio
    async def test_mention_in_ambient_channel_by_owner_starts_agent_flow(
        self, monkeypatch
    ):
        """A mention in an AMBIENT channel from the owner runs the shared agent
        flow off the trigger message, and
        completes the lock (Phase 3 containment: agent-triggering only fires
        in opted-in channels)."""
        bot = _make_bot()
        monkeypatch.setenv("OWNER_DISCORD_USER_ID", "42")
        bot_user = bot.user

        message = _make_message(content="hey bot help", mentions=[bot_user])
        message.reference = None
        message.guild = None
        message.channel = MagicMock(spec=discord.TextChannel)
        message.channel.id = 99

        mock_store = _make_store()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
            patch("chat.bot.acl.ambient_channels", return_value={"99"}),
            patch("chat.bot.directives.get_active", return_value=""),
            patch("chat.bot.directives.get_active_version", return_value=0),
            patch("chat.bot.attention_log.log_decision", MagicMock()),
            patch(
                "chat.bot.attention.needs_agent", AsyncMock(return_value=True)
            ) as mock_needs_agent,
            patch.object(
                bot,
                "start_agent_flow",
                AsyncMock(
                    return_value=SimpleNamespace(chat_reply=None, thread=MagicMock())
                ),
            ) as mock_start_flow,
            patch.object(bot, "_complete_lock", MagicMock()) as mock_complete_lock,
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

            mock_needs_agent.assert_called_once()
            mock_start_flow.assert_called_once()
            args, kwargs = mock_start_flow.call_args
            assert kwargs.get("trigger_message") is message
            mock_complete_lock.assert_called_once_with(str(message.id))

            # The agent path persists the triggering message (unlike the older
            # behaviour where only _process_message saved), so a channel whose
            # activity is agent triggers is not empty when the run builds its
            # injected context (ADR 040) from the parent channel history.
            mock_store.save_message.assert_awaited_once()
            saved = mock_store.save_message.call_args.kwargs
            assert saved["discord_message_id"] == str(message.id)
            assert saved["channel_id"] == "99"
            assert saved["content"] == "hey bot help"

    @pytest.mark.asyncio
    async def test_ambient_engage_chat_replies_in_monolith(self):
        """An ambient engage that the depth classify routes to chat (not
        repo/build/deep-research) replies in-monolith via _process_message
        with force_respond=True, and never touches the goose guest flow."""
        bot = _make_bot()

        message = _make_message(content="what's a good name for a boat?")
        message.reference = None
        message.guild = None
        message.channel = MagicMock(spec=discord.TextChannel)
        message.channel.id = 99

        mock_store = _make_store()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
            patch("chat.bot.acl.ambient_channels", return_value={"99"}),
            patch("chat.bot.directives.get_active", return_value=""),
            patch("chat.bot.directives.get_active_version", return_value=0),
            patch("chat.bot.attention_log.log_decision", MagicMock()),
            patch(
                "chat.bot.attention.evaluate",
                AsyncMock(
                    return_value=SimpleNamespace(
                        engage=True, confidence=0.9, explicit=False
                    )
                ),
            ),
            patch(
                "chat.bot.attention.needs_agent", AsyncMock(return_value=False)
            ) as mock_needs_agent,
            patch.object(
                bot, "_recently_tagged", MagicMock(return_value=True)
            ) as mock_recently_tagged,
            patch("chat.bot.attention.evaluate") as mock_evaluate,
            patch.object(bot, "_engage_agent", AsyncMock()) as mock_engage_agent,
            patch.object(bot, "start_agent_flow", AsyncMock()) as mock_start_flow,
            patch.object(bot, "_process_message", AsyncMock()) as mock_proc,
        ):
            mock_evaluate.return_value = SimpleNamespace(
                engage=True, confidence=0.9, explicit=False
            )
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

            mock_recently_tagged.assert_called_once_with("99", str(message.id))
            assert mock_evaluate.call_args.kwargs.get("recently_tagged") is True
            mock_needs_agent.assert_called_once()
            # The directive fetched for the pre-gate is threaded through to the
            # post-generation send-gate rather than re-read from the DB. A soft
            # classifier engage is not an explicit summons, so explicit=False and
            # the reply stays suppressible.
            mock_proc.assert_called_once_with(
                message, force_respond=True, explicit=False, directive=""
            )
            mock_engage_agent.assert_not_called()
            mock_start_flow.assert_not_called()

    @pytest.mark.asyncio
    async def test_explicit_ambient_engage_marks_reply_live(self):
        """A direct @mention in an ambient channel that routes to chat is an
        explicit summons: on_message threads explicit=True into _process_message
        so the no_reply tool / send-gate cannot silently eat an answer someone is
        waiting on (the ep-236 regression)."""
        bot = _make_bot()

        message = _make_message(content="<@1> the site is cool, true or false?")
        message.reference = None
        message.guild = None
        message.channel = MagicMock(spec=discord.TextChannel)
        message.channel.id = 99

        mock_store = _make_store()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
            patch("chat.bot.acl.ambient_channels", return_value={"99"}),
            patch("chat.bot.directives.get_active", return_value="only @-mentions"),
            patch("chat.bot.directives.get_active_version", return_value=4),
            patch("chat.bot.attention_log.log_decision", MagicMock()),
            patch("chat.bot.attention.needs_agent", AsyncMock(return_value=False)),
            patch.object(bot, "_recently_tagged", MagicMock(return_value=False)),
            patch(
                "chat.bot.attention.evaluate",
                AsyncMock(
                    return_value=SimpleNamespace(
                        engage=True, confidence=1.0, explicit=True
                    )
                ),
            ),
            patch.object(bot, "_engage_agent", AsyncMock()) as mock_engage_agent,
            patch.object(bot, "_process_message", AsyncMock()) as mock_proc,
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

            mock_proc.assert_called_once_with(
                message,
                force_respond=True,
                explicit=True,
                directive="only @-mentions",
            )
            mock_engage_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_mention_in_ambient_channel_by_non_owner_gets_refusal(
        self, monkeypatch
    ):
        """A non-owner ambient mention gets the owner-only refusal without
        reaching the session-start flow."""
        bot = _make_bot()
        monkeypatch.setenv("OWNER_DISCORD_USER_ID", "999")
        bot_user = bot.user

        message = _make_message(content="hey bot help", mentions=[bot_user])
        message.reference = None
        message.guild = None
        message.channel = MagicMock(spec=discord.TextChannel)
        message.channel.id = 99

        mock_store = _make_store()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
            patch("chat.bot.acl.ambient_channels", return_value={"99"}),
            patch("chat.bot.directives.get_active", return_value=""),
            patch("chat.bot.directives.get_active_version", return_value=0),
            patch("chat.bot.attention_log.log_decision", MagicMock()),
            patch("chat.bot.attention.needs_agent", AsyncMock(return_value=True)),
            patch.object(bot, "start_agent_flow", AsyncMock()) as mock_start_flow,
            patch.object(bot, "_complete_lock", MagicMock()) as mock_complete_lock,
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

            mock_start_flow.assert_not_called()
            message.reply.assert_awaited_once_with(
                "Only the configured owner can start or continue agent sessions."
            )
            mock_complete_lock.assert_called_once_with(str(message.id))

    @pytest.mark.asyncio
    async def test_mention_in_non_ambient_channel_falls_through_to_chat(self):
        """A mention in a NON-ambient channel is contained to today's inline
        chat reply: the attention gate/agent flow never fires, and the message
        goes straight to _process_message (which already replies to mentions
        inline). Proves the Phase 3 containment fix and the DM regression fix
        (DMs are never ambient)."""
        bot = _make_bot()
        bot_user = bot.user

        message = _make_message(content="hey bot help", mentions=[bot_user])
        message.reference = None
        message.guild = None

        mock_store = _make_store()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
            patch("chat.bot.acl.ambient_channels", return_value=set()),
            patch("chat.bot.attention.evaluate", AsyncMock()) as mock_evaluate,
            patch.object(bot, "start_agent_flow", AsyncMock()) as mock_start_flow,
            patch.object(bot, "_process_message", AsyncMock()) as mock_proc,
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

            mock_evaluate.assert_not_called()
            mock_start_flow.assert_not_called()
            mock_proc.assert_called_once_with(message)

    @pytest.mark.asyncio
    async def test_plain_message_skips_the_gate_entirely(self):
        """A plain, non-ambient, non-mention message never touches the
        attention classifier and falls straight through to _process_message,
        unchanged from pre-ADR-035 behavior."""
        bot = _make_bot()

        message = _make_message(content="just chatting", mentions=[])
        message.reference = None
        message.guild = None

        mock_store = _make_store()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
            patch("chat.bot.acl.ambient_channels", return_value=set()),
            patch("chat.bot.attention.evaluate", AsyncMock()) as mock_evaluate,
            patch.object(bot, "_process_message", AsyncMock()) as mock_proc,
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

            mock_evaluate.assert_not_called()
            mock_proc.assert_called_once_with(message)

    @pytest.mark.asyncio
    async def test_ambient_lookup_failure_falls_through_to_chat(self):
        """If the ambient grants read raises (e.g. a DB blip), on_message treats
        the channel as non-ambient and falls through to _process_message rather
        than dropping the message or raising."""
        bot = _make_bot()

        message = _make_message(content="hello", mentions=[])
        message.reference = None
        message.guild = None

        mock_store = _make_store()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
            patch(
                "chat.bot.acl.ambient_channels",
                side_effect=RuntimeError("db down"),
            ),
            patch("chat.bot.attention.evaluate", AsyncMock()) as mock_evaluate,
            patch.object(bot, "_process_message", AsyncMock()) as mock_proc,
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

            mock_evaluate.assert_not_called()
            mock_proc.assert_called_once_with(message)


class TestAmbientReplyIdRecorded:
    """The ambient in-channel reply's discord id is linked back to the engage
    decision (PR 1.5 of /improve-ambient), so a reaction on the reply joins to
    the engage exactly rather than by a time window."""

    @pytest.fixture(name="engine")
    def engine_fixture(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        original_schemas = {}
        for table in SQLModel.metadata.tables.values():
            if table.schema is not None:
                original_schemas[table.name] = table.schema
                table.schema = None
        SQLModel.metadata.create_all(engine)
        yield engine
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]

    @pytest.mark.asyncio
    async def test_process_message_links_reply_to_engage(self, engine):
        """_process_message(force_respond=True) records the delivered reply's id
        on the newest engage row for the trigger message."""
        from chat import attention_log
        from chat.models import AttentionDecision

        bot = _make_bot()
        message = _make_message(
            content="what's a good name for a boat?", channel_id=99, msg_id=7
        )
        message.reference = None

        # Seed the engage decision on_message would have logged for this trigger.
        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision("99", "7", "engage", 0.9, _rng=lambda: 0.0)

        mock_store = _make_store()
        sent = MagicMock(id=100)

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
            patch("chat.attention_log.get_engine", return_value=engine),
            patch.object(
                bot,
                "_stream_response",
                AsyncMock(return_value=(sent, "The SS Anytime", None)),
            ),
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot._process_message(message, force_respond=True)

        with Session(engine) as session:
            row = session.exec(select(AttentionDecision)).one()
        assert row.reply_message_id == "100"

    @pytest.mark.asyncio
    async def test_non_ambient_reply_does_not_link(self, engine):
        """A normal (non-force_respond) reply has no engage row and must not
        attempt to record a reply id, so the seeded ignore row stays untouched."""
        from chat import attention_log
        from chat.models import AttentionDecision

        bot = _make_bot()
        bot._connection.user.id = 999
        bot_user = bot.user
        message = _make_message(
            content="Hey bot!", channel_id=99, msg_id=7, mentions=[bot_user]
        )
        message.reference = None

        with patch("chat.attention_log.get_engine", return_value=engine):
            attention_log.log_decision("99", "7", "ignore", 0.1, _rng=lambda: 0.0)

        mock_store = _make_store()
        sent = MagicMock(id=100)

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
            patch("chat.attention_log.get_engine", return_value=engine),
            patch("chat.bot.attention_log.set_reply_message") as mock_set_reply,
            patch.object(
                bot,
                "_stream_response",
                AsyncMock(return_value=(sent, "Hi there", None)),
            ),
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=MagicMock()
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot._process_message(message, force_respond=False)

        mock_set_reply.assert_not_called()
        with Session(engine) as session:
            row = session.exec(select(AttentionDecision)).one()
        assert row.reply_message_id is None


class TestRecentlyTagged:
    """ChatBot._recently_tagged: recent-tag weighting for the attention gate."""

    def _recent_message(self, content: str, minutes_ago: float, msg_id: str = "1"):
        # SQLite created_at comes back tz-naive; mirror that here.
        created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return SimpleNamespace(
            discord_message_id=msg_id, content=content, created_at=created
        )

    def test_true_when_tag_within_window(self):
        bot = _make_bot()
        recent = [self._recent_message("<@999> can you check this", 2, msg_id="1")]
        mock_store = MagicMock()
        mock_store.get_recent = MagicMock(return_value=recent)

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session"),
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            assert bot._recently_tagged("99", "2") is True

    def test_false_when_tag_older_than_window(self):
        bot = _make_bot()
        recent = [self._recent_message("<@999> old ping", 30, msg_id="1")]
        mock_store = MagicMock()
        mock_store.get_recent = MagicMock(return_value=recent)

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session"),
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            assert bot._recently_tagged("99", "2") is False

    def test_false_when_no_tag_present(self):
        bot = _make_bot()
        recent = [self._recent_message("just chatting", 1, msg_id="1")]
        mock_store = MagicMock()
        mock_store.get_recent = MagicMock(return_value=recent)

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session"),
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            assert bot._recently_tagged("99", "2") is False

    def test_excludes_the_current_message(self):
        bot = _make_bot()
        # The only tag present is the message being evaluated itself.
        recent = [self._recent_message("<@999> hey", 1, msg_id="2")]
        mock_store = MagicMock()
        mock_store.get_recent = MagicMock(return_value=recent)

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session"),
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            assert bot._recently_tagged("99", "2") is False

    def test_fails_closed_on_store_error(self):
        bot = _make_bot()

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session"),
            patch("chat.bot.MessageStore", side_effect=RuntimeError("db down")),
        ):
            assert bot._recently_tagged("99", "2") is False


# ---------------------------------------------------------------------------
# ChatBot.on_raw_reaction_add -- directive propose-then-confirm (ADR 035
# Phase 5)
# ---------------------------------------------------------------------------


def _make_payload(
    emoji: str = "\U0001f44d",  # 👍
    user_id: int = 42,
    message_id: int = 555,
    channel_id: int = 99,
    message_author_id: int | None = None,
) -> MagicMock:
    payload = MagicMock()
    payload.emoji = emoji
    payload.user_id = user_id
    payload.message_id = message_id
    payload.channel_id = channel_id
    # Explicit (real discord.py raw reaction payloads populate this or leave it
    # None; a bare MagicMock would auto-vivify a truthy attribute instead of
    # None, silently defeating the "is this Bosun's own message" guard below).
    payload.message_author_id = message_author_id
    return payload


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with the chat schema stripped for SQLite compat.

    Mirrors chat.attention_log_test's fixture: ReactionEvent (and every other
    chat.* table) is declared with schema="chat" for Postgres, which SQLite
    does not support, so the schema is stripped for the duration of the test.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


class TestDirectiveReactionHandler:
    @pytest.mark.asyncio
    async def test_human_thumbs_up_applies_and_swaps_to_check(self):
        """A human 👍 on a proposal message applies it and swaps the seed
        reactions for ✅."""
        bot = _make_bot()

        summary = MagicMock()
        summary.clear_reaction = AsyncMock()
        summary.add_reaction = AsyncMock()
        channel = MagicMock()
        channel.fetch_message = AsyncMock(return_value=summary)
        bot.get_channel = MagicMock(return_value=channel)

        payload = _make_payload(emoji="\U0001f44d", user_id=42)

        with (
            patch("chat.bot.directives.is_proposal", return_value=True),
            patch(
                "chat.bot.directives.apply_proposal", return_value=True
            ) as mock_apply,
        ):
            await bot.on_raw_reaction_add(payload)
            mock_apply.assert_called_once_with(str(payload.message_id))

        summary.clear_reaction.assert_any_call("\U0001f44d")
        summary.clear_reaction.assert_any_call("\U0001f44e")
        summary.add_reaction.assert_called_once_with("✅")

    @pytest.mark.asyncio
    async def test_human_thumbs_up_on_expired_proposal_swaps_to_cross(self):
        """A 👍 on a proposal apply_proposal refuses (expired/stale) lands ❌,
        not ✅ - ✅ only when the apply actually succeeded."""
        bot = _make_bot()

        summary = MagicMock()
        summary.clear_reaction = AsyncMock()
        summary.add_reaction = AsyncMock()
        channel = MagicMock()
        channel.fetch_message = AsyncMock(return_value=summary)
        bot.get_channel = MagicMock(return_value=channel)

        payload = _make_payload(emoji="\U0001f44d", user_id=42)

        with (
            patch("chat.bot.directives.is_proposal", return_value=True),
            patch(
                "chat.bot.directives.apply_proposal", return_value=False
            ) as mock_apply,
        ):
            await bot.on_raw_reaction_add(payload)
            mock_apply.assert_called_once_with(str(payload.message_id))

        summary.add_reaction.assert_called_once_with("❌")

    @pytest.mark.asyncio
    async def test_human_thumbs_down_discards_and_swaps_to_cross(self):
        """A human 👎 on a proposal message never calls apply_proposal and
        swaps the seed reactions for ❌."""
        bot = _make_bot()

        summary = MagicMock()
        summary.clear_reaction = AsyncMock()
        summary.add_reaction = AsyncMock()
        channel = MagicMock()
        channel.fetch_message = AsyncMock(return_value=summary)
        bot.get_channel = MagicMock(return_value=channel)

        payload = _make_payload(emoji="\U0001f44e", user_id=42)

        with (
            patch("chat.bot.directives.is_proposal", return_value=True),
            patch("chat.bot.directives.apply_proposal") as mock_apply,
        ):
            await bot.on_raw_reaction_add(payload)
            mock_apply.assert_not_called()

        summary.add_reaction.assert_called_once_with("❌")

    @pytest.mark.asyncio
    async def test_bots_own_reaction_is_ignored(self):
        """The bot's own seed 👍/👎 reaction (payload.user_id == bot id) is
        ignored entirely -- no channel/message fetch, no DB call."""
        bot = _make_bot()
        bot.get_channel = MagicMock()

        payload = _make_payload(emoji="\U0001f44d", user_id=bot.user.id)

        with (
            patch("chat.bot.directives.is_proposal") as mock_is_proposal,
            patch("chat.bot.directives.apply_proposal") as mock_apply,
        ):
            await bot.on_raw_reaction_add(payload)
            mock_is_proposal.assert_not_called()
            mock_apply.assert_not_called()

        bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaction_on_non_proposal_message_does_nothing(self):
        """A human 👍 on a message that isn't a directive proposal (is_proposal
        False) does nothing -- no apply_proposal call, no channel fetch."""
        bot = _make_bot()
        bot.get_channel = MagicMock()

        payload = _make_payload(emoji="\U0001f44d", user_id=42)

        with (
            patch("chat.bot.directives.is_proposal", return_value=False),
            patch("chat.bot.directives.apply_proposal") as mock_apply,
        ):
            await bot.on_raw_reaction_add(payload)
            mock_apply.assert_not_called()

        bot.get_channel.assert_not_called()


class TestReactionPersistence:
    """Human reactions on Bosun's own messages become chat.reaction_event rows
    (the /improve-ambient ground-truth signal). Runs ahead of, and must not
    disturb, the directive proposal-confirm handling above."""

    @pytest.mark.asyncio
    async def test_reaction_on_bot_message_persisted(self, engine):
        """A human 👍 on a bot-authored message persists one add row."""
        bot = _make_bot()
        bot.get_channel = MagicMock()

        payload = _make_payload(
            emoji="\U0001f44d",
            user_id=42,
            message_id=555,
            channel_id=99,
            message_author_id=bot.user.id,
        )

        with (
            patch("chat.bot.get_engine", return_value=engine),
            patch("chat.bot.directives.is_proposal", return_value=False),
        ):
            await bot.on_raw_reaction_add(payload)

        with Session(engine) as session:
            rows = session.exec(select(ReactionEvent)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.action == "add"
        assert row.emoji == "\U0001f44d"
        assert row.reactor_id == "42"
        assert row.channel_id == "99"
        assert row.message_id == "555"
        assert row.target_is_bot is True

    @pytest.mark.asyncio
    async def test_reaction_on_human_message_not_persisted(self, engine):
        """A reaction on a message NOT authored by the bot is never persisted."""
        bot = _make_bot()
        bot.get_channel = MagicMock()

        payload = _make_payload(
            emoji="\U0001f44d",
            user_id=42,
            message_author_id=777,  # some human author, not the bot
        )

        with (
            patch("chat.bot.get_engine", return_value=engine),
            patch("chat.bot.directives.is_proposal", return_value=False),
        ):
            await bot.on_raw_reaction_add(payload)

        with Session(engine) as session:
            rows = session.exec(select(ReactionEvent)).all()
        assert rows == []

    @pytest.mark.asyncio
    async def test_bot_own_reaction_not_persisted(self, engine):
        """The bot's own seed reaction (payload.user_id == bot id) is never
        persisted -- the existing early return in on_raw_reaction_add."""
        bot = _make_bot()
        bot.get_channel = MagicMock()

        payload = _make_payload(
            emoji="\U0001f44d",
            user_id=bot.user.id,
            message_author_id=bot.user.id,
        )

        with patch("chat.bot.get_engine", return_value=engine):
            await bot.on_raw_reaction_add(payload)

        with Session(engine) as session:
            rows = session.exec(select(ReactionEvent)).all()
        assert rows == []

    @pytest.mark.asyncio
    async def test_reaction_remove_persisted(self, engine):
        """on_raw_reaction_remove persists a 'remove' row when a matching prior
        'add' row exists, cancelling that earlier add signal.

        Discord does not send message_author_id on REACTION_REMOVE, so the
        payload leaves it at the None default: the prior add row (stored only
        for bot-authored messages) is what proves the target was Bosun's."""
        bot = _make_bot()

        # Seed the prior add row directly, matching (message_id, reactor_id,
        # emoji) of the incoming remove.
        with Session(engine) as session:
            session.add(
                ReactionEvent(
                    channel_id="99",
                    message_id="555",
                    target_is_bot=True,
                    emoji="\U0001f44e",
                    reactor_id="42",
                    action="add",
                )
            )
            session.commit()

        payload = _make_payload(
            emoji="\U0001f44e",
            user_id=42,
            message_id=555,
            channel_id=99,
        )
        assert payload.message_author_id is None  # Discord omits it on remove

        with patch("chat.bot.get_engine", return_value=engine):
            await bot.on_raw_reaction_remove(payload)

        with Session(engine) as session:
            rows = session.exec(
                select(ReactionEvent).where(ReactionEvent.action == "remove")
            ).all()
        assert len(rows) == 1
        assert rows[0].emoji == "\U0001f44e"
        assert rows[0].reactor_id == "42"
        assert rows[0].message_id == "555"

    @pytest.mark.asyncio
    async def test_reaction_remove_without_prior_add_not_persisted(self, engine):
        """The real production case: a remove with no matching prior add row
        (e.g. an un-reaction on a message that was never a bot message, or whose
        add predates this table) writes nothing. Guards against logging a remove
        that cancels nothing."""
        bot = _make_bot()

        payload = _make_payload(
            emoji="\U0001f44e",
            user_id=42,
            message_id=555,
            channel_id=99,
        )

        with patch("chat.bot.get_engine", return_value=engine):
            await bot.on_raw_reaction_remove(payload)

        with Session(engine) as session:
            rows = session.exec(select(ReactionEvent)).all()
        assert rows == []

    @pytest.mark.asyncio
    async def test_reaction_remove_ignores_bots_own_reaction(self, engine):
        """The bot's own reaction removal is ignored on the top guard
        (payload.user_id == bot id), independent of message_author_id."""
        bot = _make_bot()

        # Even with a matching prior add present, the bot's own un-reaction
        # short-circuits before the persistence path.
        with Session(engine) as session:
            session.add(
                ReactionEvent(
                    channel_id="99",
                    message_id="555",
                    target_is_bot=True,
                    emoji="\U0001f44e",
                    reactor_id=str(bot.user.id),
                    action="add",
                )
            )
            session.commit()

        payload = _make_payload(
            emoji="\U0001f44e",
            user_id=bot.user.id,
            message_id=555,
            channel_id=99,
        )

        with patch("chat.bot.get_engine", return_value=engine):
            await bot.on_raw_reaction_remove(payload)

        with Session(engine) as session:
            rows = session.exec(
                select(ReactionEvent).where(ReactionEvent.action == "remove")
            ).all()
        assert rows == []

    @pytest.mark.asyncio
    async def test_directive_proposal_confirm_persists_reaction_too(self, engine):
        """The proposal-confirm path (TestDirectiveReactionHandler) also runs
        through persistence first: a 👍 on a bot-authored proposal message
        both persists the reaction AND still applies the proposal."""
        bot = _make_bot()

        summary = MagicMock()
        summary.clear_reaction = AsyncMock()
        summary.add_reaction = AsyncMock()
        channel = MagicMock()
        channel.fetch_message = AsyncMock(return_value=summary)
        bot.get_channel = MagicMock(return_value=channel)

        payload = _make_payload(
            emoji="\U0001f44d",
            user_id=42,
            message_author_id=bot.user.id,
        )

        with (
            patch("chat.bot.get_engine", return_value=engine),
            patch("chat.bot.directives.is_proposal", return_value=True),
            patch(
                "chat.bot.directives.apply_proposal", return_value=True
            ) as mock_apply,
        ):
            await bot.on_raw_reaction_add(payload)
            mock_apply.assert_called_once_with(str(payload.message_id))

        summary.add_reaction.assert_called_once_with("✅")
        with Session(engine) as session:
            rows = session.exec(select(ReactionEvent)).all()
        assert len(rows) == 1
        assert rows[0].action == "add"


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


# ---------------------------------------------------------------------------
# start_agent_flow: ADR 036 orchestrator verdict branching
# ---------------------------------------------------------------------------


def _flow_channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 99
    channel.guild = None
    return channel


class TestStartAgentFlowOrchestrator:
    @pytest.mark.asyncio
    async def test_chat_verdict_replies_without_thread_or_session(self):
        """A chat verdict produces a reply and opens no thread or session."""
        from chat import orchestrator

        bot = _make_bot()
        channel = _flow_channel()
        channel.create_thread = AsyncMock()
        user = MagicMock()

        verdict = orchestrator.ChatVerdict(context="c", direction="d")
        with (
            patch.object(bot, "_orchestrator_verdict", AsyncMock(return_value=verdict)),
            patch.object(
                bot,
                "_orchestrator_chat_reply",
                AsyncMock(return_value=("here is a friendly answer", [], [])),
            ),
            patch("chat.bot.acl.is_owner", return_value=True),
        ):
            outcome = await bot.start_agent_flow(channel, user, "name my boat", "")

        assert outcome.chat_reply == "here is a friendly answer"
        assert outcome.generated_files == []
        assert outcome.thread is None
        channel.create_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_plan_verdict_opens_thread_and_submits_raw_prompt(
        self,
    ):
        """A PlanVerdict opens a thread and submits the RAW prompt.

        The plan itself is no longer consumed: its only reader was the retired
        goosecracker runner, which delivered it to the guest via injectedContext.
        Agent sessions take the prompt alone, so a PlanVerdict now behaves
        exactly like a FailOpen here. Kept as a regression test that a non-chat
        verdict still opens a thread rather than silently doing nothing."""
        from chat import orchestrator
        from chat.orchestrator_plan import Plan, PlanStep

        bot = _make_bot()
        channel = _flow_channel()
        thread = MagicMock()
        thread.id = 555
        thread.send = AsyncMock()
        channel.create_thread = AsyncMock(return_value=thread)
        user = MagicMock()

        plan = Plan(
            enabled_subrecipes=("query", "implement"),
            steps=(
                PlanStep(sub_recipe="query", context="Find the failing test."),
                PlanStep(sub_recipe="implement", context="Fix it and open a PR."),
            ),
            done_criteria=("CI green",),
        )
        verdict = orchestrator.PlanVerdict(
            plan=plan,
            repo="jomcgi/homelab",
            repo_paths=["projects/monolith/chat/bot.py"],
            repo_replaced=False,
        )
        with (
            patch.object(bot, "_orchestrator_verdict", AsyncMock(return_value=verdict)),
            patch("chat.bot.acl.is_owner", return_value=True),
            patch(
                "agent_sessions.api.start_session_for_thread", new_callable=AsyncMock
            ) as start_session,
        ):
            outcome = await bot.start_agent_flow(channel, user, "raw prompt", "")

        assert outcome.thread is thread
        # The submitted task is the raw prompt (ground truth), never brief-prefixed.
        # None, not "", because start_session_for_thread validates a non-None repo
        # against REPO_CATALOG.
        start_session.assert_awaited_once_with("555", "raw prompt", None)
        # The echo is an attributed, FENCED block (_format_agent_prompt_echo), so
        # match on the prompt being inside it rather than on the exact framing.
        # The checklist that used to be pre-rendered here from the plan's steps is
        # gone with the plan itself, so the intro is now the bare placeholder.
        sent_bodies = [c.args[0] for c in thread.send.call_args_list]
        assert sent_bodies[0].startswith("Prompt from ")
        assert "raw prompt" in sent_bodies[0]
        assert sent_bodies[1] == "🤖 Planning..."

    @pytest.mark.asyncio
    async def test_failopen_preserves_raw_prompt_submit(self):
        """A FailOpen verdict is byte-for-byte today's behaviour: the raw prompt
        is the task and the intro is the bare Planning placeholder."""
        from chat import orchestrator

        bot = _make_bot()
        channel = _flow_channel()
        thread = MagicMock()
        thread.id = 777
        thread.send = AsyncMock()
        channel.create_thread = AsyncMock(return_value=thread)
        user = MagicMock()

        verdict = orchestrator.FailOpen("orchestrator disabled")
        with (
            patch.object(bot, "_orchestrator_verdict", AsyncMock(return_value=verdict)),
            patch("chat.bot.acl.is_owner", return_value=True),
            patch(
                "agent_sessions.api.start_session_for_thread", new_callable=AsyncMock
            ) as start_session,
        ):
            outcome = await bot.start_agent_flow(channel, user, "raw prompt", "")

        assert outcome.thread is thread
        # A repo-less run passes None, not "": start_session_for_thread validates
        # any non-None repo against REPO_CATALOG, and "" is not in it.
        start_session.assert_awaited_once_with("777", "raw prompt", None)
        sent_bodies = [c.args[0] for c in thread.send.call_args_list]
        assert "🤖 Planning..." in sent_bodies

    @pytest.mark.asyncio
    async def test_orchestrator_verdict_short_circuits_when_disabled(self):
        """_orchestrator_verdict returns FailOpen without calling compile when
        the tier is disabled, so the failopen path does no extra gathering."""
        from chat import orchestrator

        bot = _make_bot()
        user = MagicMock()
        with (
            patch("chat.bot.orchestrator.enabled", return_value=False),
            patch("chat.bot.orchestrator.compile", AsyncMock()) as mock_compile,
        ):
            verdict = await bot._orchestrator_verdict("g", "c", user, "p", "")

        assert isinstance(verdict, orchestrator.FailOpen)
        mock_compile.assert_not_called()
