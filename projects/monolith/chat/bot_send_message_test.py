"""Tests for chat.bot.send_message helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat import bot as bot_module


@pytest.fixture
def fake_bot():
    """Install a MagicMock in place of the module-level bot for the duration of a test."""
    fake = MagicMock()
    fake.fetch_channel = AsyncMock()
    with patch.object(bot_module, "bot", fake):
        yield fake


@pytest.mark.asyncio
async def test_send_message_uses_cached_channel(fake_bot):
    channel = AsyncMock()
    fake_bot.get_channel.return_value = channel

    await bot_module.send_message("123", "hello", level="info")

    fake_bot.get_channel.assert_called_once_with(123)
    fake_bot.fetch_channel.assert_not_awaited()
    channel.send.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_send_message_falls_back_to_fetch(fake_bot):
    channel = AsyncMock()
    fake_bot.get_channel.return_value = None
    fake_bot.fetch_channel.return_value = channel

    await bot_module.send_message("123", "boom", level="error")

    fake_bot.fetch_channel.assert_awaited_once_with(123)
    channel.send.assert_awaited_once_with("\U0001f534 boom")


@pytest.mark.asyncio
async def test_send_message_warn_prefix(fake_bot):
    channel = AsyncMock()
    fake_bot.get_channel.return_value = channel

    await bot_module.send_message("123", "careful", level="warn")

    channel.send.assert_awaited_once_with("⚠️ careful")
