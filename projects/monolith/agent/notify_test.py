"""Tests for agent.notify."""

from unittest.mock import AsyncMock, patch

import pytest

from agent import notify as notify_mod


@pytest.fixture
def discord_env(monkeypatch):
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "S1")
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID", "C-default")
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_ALLOWED_CHANNEL_IDS", "9999, 8888")


@pytest.mark.asyncio
async def test_notify_defaults_to_settings_channel(discord_env):
    with patch.object(notify_mod, "send_message", AsyncMock()) as send:
        result = await notify_mod.notify("hi")

    send.assert_awaited_once_with("C-default", "hi", level="info")
    assert result == {"ok": True, "channel": "C-default"}


@pytest.mark.asyncio
async def test_notify_passes_level_through(discord_env):
    with patch.object(notify_mod, "send_message", AsyncMock()) as send:
        result = await notify_mod.notify("hi", level="warn")

    send.assert_awaited_once_with("C-default", "hi", level="warn")
    assert result == {"ok": True, "channel": "C-default"}


@pytest.mark.asyncio
async def test_notify_succeeds_for_allow_listed_channel(discord_env):
    with patch.object(notify_mod, "send_message", AsyncMock()) as send:
        result = await notify_mod.notify("hi", channel="9999")

    send.assert_awaited_once_with("9999", "hi", level="info")
    assert result == {"ok": True, "channel": "9999"}


@pytest.mark.asyncio
async def test_notify_rejects_channel_not_in_allow_list(discord_env):
    with patch.object(notify_mod, "send_message", AsyncMock()) as send:
        with pytest.raises(ValueError, match="not in allow-list"):
            await notify_mod.notify("hi", channel="forbidden")

    send.assert_not_awaited()
