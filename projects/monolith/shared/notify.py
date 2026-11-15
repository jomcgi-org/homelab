"""Routine-facing Discord notification.

Enqueues a Discord post to the outbox; the leader's bot drains and posts it.
Defaults to the homelab channel from the environment when no channel is given.
There is no application-level allow-list: the bot can only post to channels in
the server(s) it belongs to, which is the operative boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from chat.models import DiscordOutbox
from chat.outbox import enqueue_message
from core.db import get_engine


def _enqueue_sync(
    channel_id: str, content: str, level: str, dedupe_key: str | None = None
) -> bool:
    """Open a session, enqueue, commit. Sync so the async caller can hand it to
    a worker thread (a sync Session must not run on the event loop - semgrep
    no-sync-session-in-async-def)."""
    with Session(get_engine()) as session:
        kwargs = {}
        if dedupe_key is not None:
            # Scope to the channel, keep caller keys out of stored metadata,
            # and separate this namespace from scheduled-task occurrences.
            key = hashlib.sha256(f"{channel_id}\0{dedupe_key}".encode()).hexdigest()
            kwargs = {"dedupe_key": f"notify:{key}", "kind": "factory_notify"}
        enqueue_message(session, channel_id, content=content, level=level, **kwargs)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            if (
                dedupe_key is None
                or session.exec(
                    select(DiscordOutbox.id).where(
                        DiscordOutbox.dedupe_key == kwargs["dedupe_key"]
                    )
                ).first()
                is None
            ):
                raise
            return False
        return True


async def notify(
    message: str,
    level: Literal["info", "warn", "error"] = "info",
    channel: str | None = None,
    dedupe_key: str | None = None,
) -> dict:
    target = channel or os.environ["MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID"]
    # Enqueue rather than post directly: this can run on any replica, but the
    # Discord bot is a leader-only singleton, so the leader's drain loop posts
    # the row. notify is non-interactive, so the few-second drain delay is fine.
    if dedupe_key is not None and not 1 <= len(dedupe_key) <= 128:
        raise ValueError("dedupe_key must contain 1 to 128 characters")
    queued = await asyncio.to_thread(_enqueue_sync, target, message, level, dedupe_key)
    return {"ok": True, "channel": target, "queued": queued}
