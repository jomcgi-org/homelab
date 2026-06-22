"""Tests for chat.outbox: enqueue validation and the drain posting logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import chat.outbox as outbox
from chat.outbox import drain_once, enqueue_message


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
