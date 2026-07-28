"""Routine-facing Discord notification.

Enqueues a Discord post to the outbox; the leader's bot drains and posts it.
Defaults to the homelab channel from settings when no channel is given. There
is no application-level allow-list: the bot can only post to channels in the
server(s) it belongs to, which is the operative boundary.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from sqlmodel import Session

from agent.config import load_settings
from core.db import get_engine
from chat.api import enqueue_message


def _enqueue_sync(channel_id: str, content: str, level: str) -> None:
    """Open a session, enqueue, commit. Sync so the async caller can hand it to
    a worker thread (a sync Session must not run on the event loop - semgrep
    no-sync-session-in-async-def)."""
    with Session(get_engine()) as session:
        enqueue_message(session, channel_id, content=content, level=level)
        session.commit()


async def notify(
    message: str,
    level: Literal["info", "warn", "error"] = "info",
    channel: str | None = None,
) -> dict:
    settings = load_settings()
    target = channel or settings.discord_default_channel_id
    # Enqueue rather than post directly: this can run on any replica, but the
    # Discord bot is a leader-only singleton, so the leader's drain loop posts
    # the row. notify is non-interactive, so the few-second drain delay is fine.
    await asyncio.to_thread(_enqueue_sync, target, message, level)
    return {"ok": True, "channel": target, "queued": True}
