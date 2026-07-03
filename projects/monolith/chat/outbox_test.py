"""Tests for chat.outbox: enqueue validation and the drain posting logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

import chat.outbox as outbox
from chat.models import DiscordOutbox
from chat.outbox import drain_once, enqueue_edit, enqueue_message, enqueue_reaction


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with the chat schema stripped, so the DB-level
    CHECK constraint (mirrored into the model) is actually exercised."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original:
            table.schema = original[table.name]


def test_reaction_row_satisfies_content_or_embed_check(engine):
    """A reaction row (no content, no embed) must be accepted by the DB CHECK -
    the regression that wedged prod: the constraint required content OR embed."""
    with Session(engine) as session:
        enqueue_reaction(session, "chan", "msg", "⏳", remove=True)
        session.commit()
    with Session(engine) as session:
        row = session.query(DiscordOutbox).one()
    assert row.reaction == "⏳" and row.content is None and row.embed_json is None


def test_fully_empty_outbox_row_is_rejected(engine):
    """The relaxed CHECK still rejects a row with no content, embed, or reaction."""
    with Session(engine) as session:
        session.add(DiscordOutbox(channel_id="chan"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_enqueue_text_adds_row():
    session = MagicMock()
    enqueue_message(session, "C1", content="hello", level="warn")
    session.add.assert_called_once()
    row = session.add.call_args.args[0]
    assert row.channel_id == "C1"
    assert row.content == "hello"
    assert row.embed_json is None
    assert row.level == "warn"


def test_enqueue_embed_serialises_json():
    session = MagicMock()
    enqueue_message(session, "C1", embed={"title": "T"})
    row = session.add.call_args.args[0]
    assert row.content is None
    assert row.embed_json == '{"title": "T"}'


def test_enqueue_requires_exactly_one_of_content_embed():
    session = MagicMock()
    with pytest.raises(ValueError, match="exactly one"):
        enqueue_message(session, "C1")
    with pytest.raises(ValueError, match="exactly one"):
        enqueue_message(session, "C1", content="x", embed={"a": 1})


@pytest.mark.asyncio
async def test_drain_posts_text_and_marks_posted():
    row = {
        "id": 1,
        "channel_id": "100",
        "content": "ping",
        "embed_json": None,
        "level": "info",
    }
    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted") as mark_posted,
        patch.object(outbox, "_mark_failed") as mark_failed,
    ):
        posted = await drain_once(bot, engine=object())

    channel.send.assert_awaited_once_with("ping")
    mark_posted.assert_called_once()
    mark_failed.assert_not_called()
    assert posted == 1


@pytest.mark.asyncio
async def test_drain_posts_embed():
    row = {
        "id": 2,
        "channel_id": "100",
        "content": None,
        "embed_json": '{"title": "Changelog"}',
        "level": "info",
    }
    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted"),
        patch.object(outbox, "_mark_failed"),
    ):
        await drain_once(bot, engine=object())

    # Posted as an embed, not text.
    assert channel.send.await_args.kwargs.get("embed") is not None


def test_enqueue_reaction_adds_row():
    session = MagicMock()
    enqueue_reaction(session, "C1", "M9", "⏳", remove=True)
    row = session.add.call_args.args[0]
    assert row.channel_id == "C1"
    assert row.target_message_id == "M9"
    assert row.reaction == "⏳"
    assert row.reaction_remove is True
    assert row.content is None and row.embed_json is None


@pytest.mark.asyncio
async def test_drain_adds_reaction():
    row = {
        "id": 4,
        "channel_id": "100",
        "content": None,
        "embed_json": None,
        "level": "info",
        "target_message_id": "555",
        "reaction": "\U0001f440",
        "reaction_remove": False,
    }
    message = MagicMock()
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted") as mark_posted,
        patch.object(outbox, "_mark_failed") as mark_failed,
    ):
        await drain_once(bot, engine=object())

    channel.fetch_message.assert_awaited_once_with(555)
    message.add_reaction.assert_awaited_once_with("\U0001f440")
    message.remove_reaction.assert_not_awaited()
    mark_posted.assert_called_once()
    mark_failed.assert_not_called()


@pytest.mark.asyncio
async def test_drain_reaction_remove_swallows_missing():
    """Removing an absent reaction resolves the row (idempotent), not a failure."""
    row = {
        "id": 5,
        "channel_id": "100",
        "content": None,
        "embed_json": None,
        "level": "info",
        "target_message_id": "555",
        "reaction": "⏳",
        "reaction_remove": True,
    }
    message = MagicMock()
    message.remove_reaction = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "no reaction")
    )
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted") as mark_posted,
        patch.object(outbox, "_mark_failed") as mark_failed,
    ):
        await drain_once(bot, engine=object())

    # A missing prior reaction is swallowed: the row counts as posted.
    mark_posted.assert_called_once()
    mark_failed.assert_not_called()


def test_enqueue_edit_adds_row():
    session = MagicMock()
    enqueue_edit(session, "C1", "M9", "final result")
    row = session.add.call_args.args[0]
    assert row.channel_id == "C1"
    assert row.target_message_id == "M9"
    assert row.content == "final result"
    assert row.reaction is None and row.embed_json is None


def test_edit_row_satisfies_content_or_embed_check(engine):
    """An edit row (content + target_message_id, no reaction) is a valid outbox
    entry: content is non-NULL, so it passes the content-or-embed-or-reaction
    CHECK, and the target_message_id makes the drain edit instead of post."""
    with Session(engine) as session:
        enqueue_edit(session, "chan", "msg", "final result")
        session.commit()
    with Session(engine) as session:
        row = session.query(DiscordOutbox).one()
    assert row.target_message_id == "msg" and row.content == "final result"
    assert row.reaction is None


@pytest.mark.asyncio
async def test_drain_edits_message_in_place():
    """An edit row overwrites the target message and never posts a new one."""
    row = {
        "id": 6,
        "channel_id": "100",
        "content": "Artifact ready: https://x",
        "embed_json": None,
        "level": "info",
        "target_message_id": "777",
        "reaction": None,
        "reaction_remove": False,
    }
    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted") as mark_posted,
        patch.object(outbox, "_mark_failed") as mark_failed,
    ):
        await drain_once(bot, engine=object())

    channel.fetch_message.assert_awaited_once_with(777)
    message.edit.assert_awaited_once_with(content="Artifact ready: https://x")
    channel.send.assert_not_called()
    mark_posted.assert_called_once()
    mark_failed.assert_not_called()


@pytest.mark.asyncio
async def test_drain_edit_swallows_missing_message():
    """Editing a deleted message resolves the row (nothing to settle), not fail."""
    row = {
        "id": 7,
        "channel_id": "100",
        "content": "final",
        "embed_json": None,
        "level": "info",
        "target_message_id": "777",
        "reaction": None,
        "reaction_remove": False,
    }
    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted") as mark_posted,
        patch.object(outbox, "_mark_failed") as mark_failed,
    ):
        await drain_once(bot, engine=object())

    mark_posted.assert_called_once()
    mark_failed.assert_not_called()


@pytest.mark.asyncio
async def test_drain_marks_failed_on_post_error():
    row = {
        "id": 3,
        "channel_id": "bad",
        "content": "x",
        "embed_json": None,
        "level": "info",
    }
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=RuntimeError("no such channel"))

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted") as mark_posted,
        patch.object(outbox, "_mark_failed") as mark_failed,
    ):
        posted = await drain_once(bot, engine=object())

    mark_failed.assert_called_once()
    mark_posted.assert_not_called()
    assert posted == 0
