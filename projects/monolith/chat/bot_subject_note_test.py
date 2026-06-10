"""Tests for format_subject_note + its injection into the agent prompt.

format_subject_note resolves *who the current message is about* from Discord's
structured mention/reply data, so the model targets the right person instead of
scraping a stale "<@id>" out of the rendered conversation history. These tests
cover the pure helper (mention, reply, exclusion, empty cases) and verify the
note actually reaches the prompt passed to the agent.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai import PartDeltaEvent, TextPartDelta

from chat.bot import ChatBot, format_subject_note


def _user(uid: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=uid, display_name=name)


def _reply_to(author: SimpleNamespace) -> SimpleNamespace:
    """A message.reference whose resolved message was written by `author`."""
    return SimpleNamespace(resolved=SimpleNamespace(author=author))


def _msg(mentions=None, reference=None) -> SimpleNamespace:
    return SimpleNamespace(mentions=mentions or [], reference=reference)


BOT_ID = 999


# ---------------------------------------------------------------------------
# format_subject_note (pure helper)
# ---------------------------------------------------------------------------


class TestFormatSubjectNote:
    def test_empty_for_plain_message(self):
        """No mentions and no reply -> no subject note (plain chat)."""
        assert format_subject_note(_msg(), BOT_ID) == ""

    def test_mention_surfaces_id_and_name(self):
        """A direct @-mention is named with its numeric id."""
        note = format_subject_note(
            _msg(mentions=[_user(98146497454960640, "𝔲𝔤𝔩𝔶𝔟𝔬𝔶")]), BOT_ID
        )
        assert "directly @-mentions" in note
        assert "Discord user ID 98146497454960640" in note
        assert "𝔲𝔤𝔩𝔶𝔟𝔬𝔶" in note

    def test_excludes_the_bot_itself(self):
        """A mention of the bot is dropped; only third parties remain."""
        note = format_subject_note(
            _msg(mentions=[_user(BOT_ID, "Qwen"), _user(42, "alice")]), BOT_ID
        )
        assert "Discord user ID 42" in note
        assert str(BOT_ID) not in note
        assert "Qwen" not in note

    def test_mention_only_bot_yields_empty(self):
        """If the only mention is the bot, there is no third-party subject."""
        assert format_subject_note(_msg(mentions=[_user(BOT_ID, "Qwen")]), BOT_ID) == ""

    def test_reply_target_author_surfaced(self):
        """Replying to someone's message names that author as the subject."""
        note = format_subject_note(_msg(reference=_reply_to(_user(555, "bob"))), BOT_ID)
        assert "is a reply to a message from" in note
        assert "Discord user ID 555" in note
        assert "bob" in note

    def test_reply_to_bot_is_ignored(self):
        """A reply to the bot's own message is not a third-party subject."""
        assert (
            format_subject_note(
                _msg(reference=_reply_to(_user(BOT_ID, "Qwen"))), BOT_ID
            )
            == ""
        )

    def test_mention_and_reply_both_listed(self):
        """Both an @-mention and a reply target appear when both are present."""
        note = format_subject_note(
            _msg(
                mentions=[_user(42, "alice")],
                reference=_reply_to(_user(555, "bob")),
            ),
            BOT_ID,
        )
        assert "Discord user ID 42" in note
        assert "Discord user ID 555" in note

    def test_reply_with_no_author_ignored(self):
        """A resolved reference lacking an author (e.g. deleted) is skipped."""
        ref = SimpleNamespace(resolved=SimpleNamespace(author=None))
        assert format_subject_note(_msg(reference=ref), BOT_ID) == ""

    def test_history_only_mention_not_surfaced(self):
        """Ids that exist only in history (not on the triggering message) are
        never in the note: the helper reads structured fields, not text."""
        note = format_subject_note(_msg(mentions=[_user(42, "alice")]), BOT_ID)
        # An unrelated id that might appear in rendered history must not leak in.
        assert "123456789012345678" not in note


# ---------------------------------------------------------------------------
# Injection into the agent prompt (wiring)
# ---------------------------------------------------------------------------


class _AsyncCtxManager:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def _async_iter(events):
    for e in events:
        yield e


def _make_bot(user_id: int = BOT_ID) -> ChatBot:
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
    bot._connection.user.display_name = "Qwen"
    return bot


def _make_store():
    store = AsyncMock()
    store.save_message = AsyncMock()
    store.get_recent = MagicMock(return_value=[])
    store.get_attachments = MagicMock(return_value={})
    store.get_channel_summary = MagicMock(return_value=None)
    store.get_user_summaries_for_users = MagicMock(return_value=[])
    store.acquire_lock = MagicMock(return_value=True)
    store.mark_completed = MagicMock()
    return store


def _make_discord_message(mentions, reference=None) -> MagicMock:
    msg = MagicMock()
    msg.id = 1
    msg.content = "what do your notes say about them"
    msg.author.bot = False
    msg.author.id = 42
    msg.author.display_name = "asker"
    msg.channel.id = 99
    msg.channel.typing = MagicMock(return_value=_AsyncCtxManager())
    msg.mentions = mentions
    msg.reference = reference
    msg.attachments = []
    msg.embeds = []
    sent = MagicMock(id=100)
    sent.edit = AsyncMock()
    msg.reply = AsyncMock(return_value=sent)
    return msg


async def _run(bot: ChatBot, msg: MagicMock, store) -> str:
    bot.agent.run_stream_events = MagicMock(
        return_value=_async_iter(
            [PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="ok"))]
        )
    )
    with (
        patch("chat.bot.get_engine"),
        patch("chat.bot.Session") as mock_session_cls,
        patch("chat.bot.MessageStore", return_value=store),
    ):
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
        await bot.on_message(msg)
    return bot.agent.run_stream_events.call_args[0][0]


class TestSubjectNoteInPrompt:
    @pytest.mark.asyncio
    async def test_mentioned_user_id_reaches_prompt(self):
        """The mentioned user's id is stated authoritatively in the prompt."""
        bot = _make_bot()
        mentioned = MagicMock()
        mentioned.id = 98146497454960640
        mentioned.display_name = "𝔲𝔤𝔩𝔶𝔟𝔬𝔶"
        msg = _make_discord_message(mentions=[bot.user, mentioned])
        prompt = await _run(bot, msg, _make_store())

        assert "Who this message is about" in prompt
        assert "Discord user ID 98146497454960640" in prompt
