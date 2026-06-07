"""Tests for the identity_note injection in ChatBot._process_message.

The block around line 337-346 of bot.py injects a formatted string that tells
the model its own live Discord identity (display_name + numeric user ID) so it
can correctly recognise self-mentions without a separate lookup.

These tests verify:
- (1) the identity note contains the bot's actual display_name
- (2) the identity note contains the bot's numeric user.id
- (3) the full interpolated format matches the template exactly
- (4) the identity note appears before context messages (ordering)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai import PartDeltaEvent, TextPartDelta

from chat.bot import ChatBot
from chat.models import ChannelSummary, UserChannelSummary


# ---------------------------------------------------------------------------
# Helpers (mirrors bot_summary_injection_test.py patterns)
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


def _make_bot(*, user_id: int = 999, display_name: str = "BotUser") -> ChatBot:
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
    bot._connection.user.id = user_id
    bot._connection.user.display_name = display_name
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


async def _run_bot(bot: ChatBot, msg: MagicMock, mock_store) -> str:
    """Run on_message and return the prompt passed to run_stream_events."""
    events = [_text_delta("ok")]
    bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

    with (
        patch("chat.bot.get_engine"),
        patch("chat.bot.Session") as mock_session_cls,
        patch("chat.bot.MessageStore", return_value=mock_store),
    ):
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
        await bot.on_message(msg)

    return bot.agent.run_stream_events.call_args[0][0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIdentityNoteContainsDisplayName:
    @pytest.mark.asyncio
    async def test_display_name_quoted_in_prompt(self):
        """The bot's display_name appears quoted in the identity note."""
        bot = _make_bot(display_name="HomeLabBot")
        msg = _make_message(content="who are you?", mentions=[bot.user])
        prompt = await _run_bot(bot, msg, _make_store())

        # display_name must be enclosed in double-quotes as per the template
        assert '"HomeLabBot"' in prompt

    @pytest.mark.asyncio
    async def test_different_display_name_reflected(self):
        """Changing the bot's display_name is reflected in the prompt."""
        bot = _make_bot(display_name="GalacticAssistant")
        msg = _make_message(content="hey", mentions=[bot.user])
        prompt = await _run_bot(bot, msg, _make_store())

        assert '"GalacticAssistant"' in prompt
        # Ensure the old default name is NOT present (sanity check)
        assert '"BotUser"' not in prompt


class TestIdentityNoteContainsUserId:
    @pytest.mark.asyncio
    async def test_user_id_in_discord_user_id_phrase(self):
        """The bot's numeric user ID appears after 'Discord user ID'."""
        bot = _make_bot(user_id=123456789)
        msg = _make_message(content="ping", mentions=[bot.user])
        prompt = await _run_bot(bot, msg, _make_store())

        assert "Discord user ID 123456789" in prompt

    @pytest.mark.asyncio
    async def test_user_id_in_mention_syntax(self):
        """The bot's user ID appears inside a Discord mention (<@ID>) in the note."""
        bot = _make_bot(user_id=987654321)
        msg = _make_message(content="hi", mentions=[bot.user])
        prompt = await _run_bot(bot, msg, _make_store())

        assert "<@987654321>" in prompt

    @pytest.mark.asyncio
    async def test_different_user_id_not_leaked(self):
        """Only the bot's own user ID appears in the identity note section."""
        bot = _make_bot(user_id=111)
        msg = _make_message(content="hi", mentions=[bot.user])
        prompt = await _run_bot(bot, msg, _make_store())

        # The identity note prefix should use 111, not some other ID
        assert "Discord user ID 111" in prompt
        # The author's ID (42, set in _make_message) should NOT bleed into the
        # identity note portion — it may appear in context, but not in the ID
        # phrase that references the BOT's identity.
        identity_start = prompt.index("[Your identity here:")
        identity_end = prompt.index("]", identity_start)
        identity_note = prompt[identity_start : identity_end + 1]
        assert "Discord user ID 42" not in identity_note


class TestIdentityNoteFullFormat:
    @pytest.mark.asyncio
    async def test_full_template_matches(self):
        """The identity note matches the exact interpolated template from bot.py."""
        user_id = 999
        display_name = "BotUser"
        bot = _make_bot(user_id=user_id, display_name=display_name)
        msg = _make_message(content="what's up", mentions=[bot.user])
        prompt = await _run_bot(bot, msg, _make_store())

        expected_note = (
            f'[Your identity here: you are "{display_name}" '
            f'(Discord user ID {user_id}). Any "<@{user_id}>" '
            "mention, or a reply to one of your own messages, in this "
            "conversation is someone talking to or about YOU \u2014 not a "
            "third party to look up.]"
        )
        assert expected_note in prompt

    @pytest.mark.asyncio
    async def test_template_interpolated_with_custom_values(self):
        """Full template is correctly interpolated for non-default bot values."""
        user_id = 555000555
        display_name = "CustomBot"
        bot = _make_bot(user_id=user_id, display_name=display_name)
        msg = _make_message(content="hello", mentions=[bot.user])
        prompt = await _run_bot(bot, msg, _make_store())

        expected_note = (
            f'[Your identity here: you are "{display_name}" '
            f'(Discord user ID {user_id}). Any "<@{user_id}>" '
            "mention, or a reply to one of your own messages, in this "
            "conversation is someone talking to or about YOU \u2014 not a "
            "third party to look up.]"
        )
        assert expected_note in prompt


class TestIdentityNoteOrdering:
    @pytest.mark.asyncio
    async def test_identity_note_precedes_recent_conversation(self):
        """The identity note must appear before the 'Recent conversation:' section."""
        bot = _make_bot()
        msg = _make_message(content="test ordering", mentions=[bot.user])
        prompt = await _run_bot(bot, msg, _make_store())

        identity_pos = prompt.index("[Your identity here:")
        context_pos = prompt.index("Recent conversation:")
        assert identity_pos < context_pos, (
            "Identity note should appear before 'Recent conversation:' in the prompt"
        )

    @pytest.mark.asyncio
    async def test_identity_note_precedes_channel_summary(self):
        """The identity note must appear before channel summary context, when present."""
        bot = _make_bot()
        msg = _make_message(content="test ordering with summary", mentions=[bot.user])
        cs = ChannelSummary(
            id=1,
            channel_id="99",
            summary="A homelab discussion channel.",
            message_count=10,
            last_message_id=50,
        )
        mock_store = _make_store(channel_summary=cs)
        prompt = await _run_bot(bot, msg, mock_store)

        identity_pos = prompt.index("[Your identity here:")
        channel_ctx_pos = prompt.index("[Channel context:")
        assert identity_pos < channel_ctx_pos, (
            "Identity note should appear before channel context in the prompt"
        )

    @pytest.mark.asyncio
    async def test_identity_note_precedes_user_summaries(self):
        """The identity note must appear before user summary context, when present."""
        bot = _make_bot()
        msg = _make_message(content="test ordering with users", mentions=[bot.user])
        user_sums = [
            UserChannelSummary(
                id=1,
                channel_id="99",
                user_id="42",
                username="Alice",
                summary="Likes Go.",
            )
        ]
        mock_store = _make_store(user_summaries=user_sums)
        prompt = await _run_bot(bot, msg, mock_store)

        identity_pos = prompt.index("[Your identity here:")
        people_pos = prompt.index("[People in this conversation:")
        assert identity_pos < people_pos, (
            "Identity note should appear before people context in the prompt"
        )

    @pytest.mark.asyncio
    async def test_identity_note_is_prompt_prefix(self):
        """The prompt starts with the identity note (it is the very first content)."""
        bot = _make_bot()
        msg = _make_message(content="anything", mentions=[bot.user])
        prompt = await _run_bot(bot, msg, _make_store())

        assert prompt.startswith("[Your identity here:"), (
            f"Expected prompt to start with '[Your identity here:' but got: {prompt[:80]!r}"
        )
