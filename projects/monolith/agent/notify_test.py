"""Tests for the shared notification implementation and agent re-export."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from agent import notify as agent_notify_mod
from shared import notify as notify_mod


@pytest.fixture
def discord_env(monkeypatch):
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "S1")
    monkeypatch.setenv("MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID", "C-default")


@contextmanager
def _patched_outbox():
    """Patch the Session/get_engine/enqueue_message machinery; yield the
    enqueue_message mock and the session it is called with."""
    session = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    with (
        patch.object(notify_mod, "get_engine"),
        patch.object(notify_mod, "Session", return_value=ctx),
        patch.object(notify_mod, "enqueue_message") as enqueue,
    ):
        yield enqueue, session


@pytest.mark.asyncio
async def test_notify_defaults_to_settings_channel(discord_env):
    with _patched_outbox() as (enqueue, session):
        result = await agent_notify_mod.notify("hi")
    enqueue.assert_called_once_with(session, "C-default", content="hi", level="info")
    session.commit.assert_called_once()
    assert result == {"ok": True, "channel": "C-default", "queued": True}


@pytest.mark.asyncio
async def test_notify_passes_level_through(discord_env):
    with _patched_outbox() as (enqueue, session):
        result = await agent_notify_mod.notify("hi", level="warn")
    enqueue.assert_called_once_with(session, "C-default", content="hi", level="warn")
    assert result["channel"] == "C-default"


@pytest.mark.asyncio
async def test_notify_enqueues_explicit_channel(discord_env):
    # Any channel is enqueued; the bot's server membership (not an app
    # allow-list) bounds where it can actually post.
    with _patched_outbox() as (enqueue, session):
        result = await agent_notify_mod.notify("hi", channel="9999")
    enqueue.assert_called_once_with(session, "9999", content="hi", level="info")
    assert result == {"ok": True, "channel": "9999", "queued": True}
