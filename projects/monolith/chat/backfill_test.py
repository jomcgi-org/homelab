"""Tests for the Discord history backfill loop."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.backfill import BackfillProgress, run_backfill
from chat.store import SaveResult


class _AsyncIterator:
    """Simulate an async iterator for channel.history()."""

    def __init__(self, items):
        self._items = list(items)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


def _make_discord_message(
    id,
    content,
    author_name="Alice",
    author_id="u1",
    author_bot=False,
    attachments=None,
):
    msg = MagicMock()
    msg.id = id
    msg.content = content
    msg.author.display_name = author_name
    msg.author.id = author_id
    msg.author.bot = author_bot
    msg.channel.id = "ch1"
    msg.attachments = attachments or []
    return msg


def _make_bot(guilds):
    bot = MagicMock()
    bot.guilds = guilds
    bot.embed_client = MagicMock()
    bot.vision_client = MagicMock()
    return bot


def _make_channel(name, channel_id, messages):
    ch = MagicMock()
    ch.name = name
    ch.id = channel_id
    ch.history = MagicMock(return_value=_AsyncIterator(messages))
    return ch


def _make_guild(text_channels):
    guild = MagicMock()
    guild.text_channels = text_channels
    return guild


@pytest.mark.asyncio
@patch("chat.backfill.get_engine")
@patch("chat.backfill.Session")
@patch("chat.backfill.MessageStore")
@patch("chat.backfill.download_image_attachments", new_callable=AsyncMock)
async def test_backfills_messages_from_channel(
    mock_download, mock_store_cls, mock_session, mock_engine
):
    """Iterates channel history and calls save_messages with correct data."""
    mock_download.return_value = []
    mock_store_instance = MagicMock()
    mock_store_instance.save_messages = AsyncMock(
        return_value=SaveResult(stored=2, skipped=0)
    )
    mock_store_cls.return_value = mock_store_instance
    mock_session.return_value.__enter__ = MagicMock()
    mock_session.return_value.__exit__ = MagicMock()

    msgs = [
        _make_discord_message(1, "hello"),
        _make_discord_message(2, "world"),
    ]
    channel = _make_channel("general", "ch1", msgs)
    guild = _make_guild([channel])
    bot = _make_bot([guild])

    await run_backfill(bot)

    mock_store_instance.save_messages.assert_called_once()
    batch = mock_store_instance.save_messages.call_args[0][0]
    assert len(batch) == 2
    assert batch[0]["discord_message_id"] == "1"
    assert batch[0]["content"] == "hello"
    assert batch[1]["discord_message_id"] == "2"
    assert batch[1]["content"] == "world"


@pytest.mark.asyncio
@patch("chat.backfill._flush_batch", new_callable=AsyncMock)
@patch("chat.backfill.download_image_attachments", new_callable=AsyncMock)
async def test_channel_filter_limits_processing_and_reports_progress(
    mock_download, mock_flush
):
    """Only selected channel IDs are visited and batch totals are reported."""
    mock_download.return_value = []
    mock_flush.side_effect = [
        SaveResult(stored=45, skipped=5),
        SaveResult(stored=1, skipped=0),
    ]
    ignored = _make_channel(
        "ignored", "101", [_make_discord_message(1, "not requested")]
    )
    selected = _make_channel(
        "selected",
        "202",
        [_make_discord_message(i, f"msg-{i}") for i in range(51)],
    )
    bot = _make_bot([_make_guild([ignored, selected])])
    updates = []

    result = await run_backfill(bot, channel_ids=["202"], on_progress=updates.append)

    ignored.history.assert_not_called()
    selected.history.assert_called_once_with(limit=None, oldest_first=True)
    assert mock_flush.call_count == 2
    assert result == BackfillProgress(
        channels_total=1,
        channels_completed=1,
        messages_stored=46,
        messages_skipped=5,
    )
    assert updates[0] == BackfillProgress(channels_total=1)
    assert any(update.messages_stored == 45 for update in updates)
    assert updates[-1] == result


@pytest.mark.asyncio
@patch("chat.backfill.get_engine")
@patch("chat.backfill.Session")
@patch("chat.backfill.MessageStore")
@patch("chat.backfill.download_image_attachments", new_callable=AsyncMock)
async def test_batches_at_50_messages(
    mock_download, mock_store_cls, mock_session, mock_engine
):
    """75 messages = 2 save_messages calls (50 + 25)."""
    mock_download.return_value = []
    mock_store_instance = MagicMock()
    mock_store_instance.save_messages = AsyncMock(
        return_value=SaveResult(stored=1, skipped=0)
    )
    mock_store_cls.return_value = mock_store_instance
    mock_session.return_value.__enter__ = MagicMock()
    mock_session.return_value.__exit__ = MagicMock()

    msgs = [_make_discord_message(i, f"msg-{i}") for i in range(75)]
    channel = _make_channel("general", "ch1", msgs)
    guild = _make_guild([channel])
    bot = _make_bot([guild])

    await run_backfill(bot)

    assert mock_store_instance.save_messages.call_count == 2
    first_batch = mock_store_instance.save_messages.call_args_list[0][0][0]
    second_batch = mock_store_instance.save_messages.call_args_list[1][0][0]
    assert len(first_batch) == 50
    assert len(second_batch) == 25


@pytest.mark.asyncio
@patch("chat.backfill.get_engine")
@patch("chat.backfill.Session")
@patch("chat.backfill.MessageStore")
@patch("chat.backfill.download_image_attachments", new_callable=AsyncMock)
async def test_processes_image_attachments(
    mock_download, mock_store_cls, mock_session, mock_engine
):
    """Downloads/describes images and passes attachment data to save_messages."""
    attachment_data = [
        {
            "data": b"png-bytes",
            "content_type": "image/png",
            "filename": "cat.png",
            "description": "A photo of a cat",
        }
    ]
    mock_download.return_value = attachment_data
    mock_store_instance = MagicMock()
    mock_store_instance.save_messages = AsyncMock(
        return_value=SaveResult(stored=1, skipped=0)
    )
    mock_store_cls.return_value = mock_store_instance
    mock_session.return_value.__enter__ = MagicMock()
    mock_session.return_value.__exit__ = MagicMock()

    discord_attachment = MagicMock()
    msgs = [
        _make_discord_message(1, "look at my cat", attachments=[discord_attachment])
    ]
    channel = _make_channel("general", "ch1", msgs)
    guild = _make_guild([channel])
    bot = _make_bot([guild])

    await run_backfill(bot)

    mock_download.assert_called_once_with(
        [discord_attachment], bot.vision_client, store=None
    )
    batch = mock_store_instance.save_messages.call_args[0][0]
    assert len(batch) == 1
    assert batch[0]["attachments"] == attachment_data


@pytest.mark.asyncio
@patch("chat.backfill.get_engine")
@patch("chat.backfill.Session")
@patch("chat.backfill.MessageStore")
@patch("chat.backfill.download_image_attachments", new_callable=AsyncMock)
async def test_skips_empty_guilds(
    mock_download, mock_store_cls, mock_session, mock_engine
):
    """No text channels = no save_messages calls."""
    guild = _make_guild([])
    bot = _make_bot([guild])

    await run_backfill(bot)

    mock_store_instance = mock_store_cls.return_value
    mock_store_instance.save_messages.assert_not_called()
