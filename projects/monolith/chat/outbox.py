"""Leader-safe Discord outbox.

Producers (any replica's MCP/notify path, or an Argo job) enqueue a row; the
leader's bot drains it and posts. This keeps the bot a singleton while letting
posting originate anywhere - see chart/migrations/...chat_discord_outbox.sql.

The bot's own interactive replies (on_message -> message.reply) do NOT use this:
they post directly on the leader with no drain latency. The outbox is only for
posts that originate off the bot (notify, changelog), where a few seconds of
drain delay is fine.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from chat.models import DiscordOutbox

logger = logging.getLogger("monolith.chat.outbox")

# Give up after this many failed post attempts so one bad row (e.g. a deleted
# channel) does not get retried forever every drain tick.
_MAX_ATTEMPTS = 5
# How many pending rows a single drain tick posts before yielding.
_BATCH = 20


def enqueue_message(
    session: Session,
    channel_id: str,
    *,
    content: str | None = None,
    embed: dict | None = None,
    level: str = "info",
) -> None:
    """Enqueue a Discord post. Exactly one of content/embed must be set.

    Caller is responsible for committing the session (matches the rest of the
    chat write API). The leader's drain loop posts the row asynchronously.
    """
    if (content is None) == (embed is None):
        raise ValueError("enqueue_message requires exactly one of content/embed")
    session.add(
        DiscordOutbox(
            channel_id=channel_id,
            content=content,
            embed_json=json.dumps(embed) if embed is not None else None,
            level=level,
        )
    )


def enqueue_reaction(
    session: Session,
    channel_id: str,
    message_id: str,
    emoji: str,
    *,
    remove: bool = False,
) -> None:
    """Enqueue an add/remove of ``emoji`` on ``message_id`` in ``channel_id``.

    A reaction row carries no content/embed; the drain resolves the message and
    adds (or removes) the bot's reaction. Used off-loop by the goose runner to
    drive the ⏳→👀→✅/❌ lifecycle on a queued reply. Caller commits the session.
    """
    session.add(
        DiscordOutbox(
            channel_id=channel_id,
            target_message_id=message_id,
            reaction=emoji,
            reaction_remove=remove,
        )
    )


def _claim_pending(engine) -> list[dict]:
    """Read the oldest unposted, not-exhausted rows. Returns plain dicts so the
    async drain never holds an ORM row across an await."""
    with Session(engine) as session:
        rows = session.exec(
            select(DiscordOutbox)
            .where(DiscordOutbox.posted_at.is_(None))
            .where(DiscordOutbox.attempts < _MAX_ATTEMPTS)
            # Order by (created_at, id) so a remove-then-add reaction pair enqueued
            # together drains in insertion order (id breaks created_at ties), never
            # leaving the stale emoji on top of the new one.
            .order_by(DiscordOutbox.created_at, DiscordOutbox.id)
            .limit(_BATCH)
        ).all()
        return [
            {
                "id": r.id,
                "channel_id": r.channel_id,
                "content": r.content,
                "embed_json": r.embed_json,
                "level": r.level,
                "target_message_id": r.target_message_id,
                "reaction": r.reaction,
                "reaction_remove": r.reaction_remove,
            }
            for r in rows
        ]


def _mark_posted(engine, row_id: int) -> None:
    with Session(engine) as session:
        row = session.get(DiscordOutbox, row_id)
        if row is not None:
            row.posted_at = datetime.now(timezone.utc)
            session.add(row)
            session.commit()


def _mark_failed(engine, row_id: int, error: str) -> None:
    with Session(engine) as session:
        row = session.get(DiscordOutbox, row_id)
        if row is not None:
            row.attempts += 1
            row.last_error = error[:500]
            session.add(row)
            session.commit()


_LEVEL_PREFIX = {"info": "", "warn": "⚠️ ", "error": "\U0001f534 "}


async def _post_row(bot, row: dict) -> None:
    """Post one outbox row via the bot. Mirrors chat.bot.send_message's channel
    resolution (cache, then API fetch)."""
    import discord

    channel = bot.get_channel(int(row["channel_id"]))
    if channel is None:
        channel = await bot.fetch_channel(int(row["channel_id"]))
    if row.get("reaction") is not None:
        await _apply_reaction(bot, channel, row)
    elif row["embed_json"] is not None:
        embed = discord.Embed.from_dict(json.loads(row["embed_json"]))
        await channel.send(embed=embed)
    else:
        prefix = _LEVEL_PREFIX.get(row["level"], "")
        await channel.send(f"{prefix}{row['content']}")


async def _apply_reaction(bot, channel, row: dict) -> None:
    """Add or remove the bot's reaction on a target message.

    A removal of an absent reaction (or a message that lost it) is not an error:
    the lifecycle is idempotent, so a missing prior emoji is swallowed rather than
    burning the row's retry budget. A missing *message* likewise resolves the row
    (nothing to react to). An add failure propagates so the drain retries it."""
    import discord

    try:
        message = await channel.fetch_message(int(row["target_message_id"]))
    except discord.NotFound:
        # The target message was deleted: nothing to react to. Resolve the row
        # rather than burn its retry budget re-fetching a message that is gone.
        logger.debug(
            "outbox: reaction target %s gone; skipping", row["target_message_id"]
        )
        return
    emoji = row["reaction"]
    if row["reaction_remove"]:
        try:
            await message.remove_reaction(emoji, bot.user)
        except (discord.NotFound, discord.HTTPException) as exc:
            logger.debug("outbox: reaction remove no-op (%s): %s", emoji, exc)
    else:
        await message.add_reaction(emoji)


async def drain_once(bot, engine) -> int:
    """Post every currently-pending row. Returns the number posted."""
    rows = await asyncio.to_thread(_claim_pending, engine)
    posted = 0
    for row in rows:
        try:
            await _post_row(bot, row)
        except Exception as exc:  # noqa: BLE001 - a bad row must not stall the rest
            logger.warning("outbox: failed to post row %s: %s", row["id"], exc)
            await asyncio.to_thread(_mark_failed, engine, row["id"], str(exc))
        else:
            await asyncio.to_thread(_mark_posted, engine, row["id"])
            posted += 1
    return posted


async def run_outbox_drain(bot, engine, poll_interval: float = 3.0) -> None:
    """Leader-only loop: drain the outbox forever. Started in _start_singletons
    alongside the bot, so it only runs on the elected leader (the replica that
    actually holds the bot connection)."""
    logger.info("Discord outbox drain started (poll=%.1fs)", poll_interval)
    while True:
        try:
            # Skip ticks until the gateway is connected, else channel resolution
            # fails and burns each row's retry budget before the bot is usable.
            # is_ready() is a sync check; bot.wait_until_ready() is banned here
            # (it deadlocks from a background task - semgrep no-wait-until-ready).
            if bot.is_ready():
                await drain_once(bot, engine)
        except Exception:
            logger.exception("outbox drain tick failed")
        await asyncio.sleep(poll_interval)
