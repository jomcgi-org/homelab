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


def _record_scheduled_outcome(
    session: Session, row: DiscordOutbox, status: str, error: str | None = None
) -> None:
    """Mirror the outbox result onto its durable occurrence audit row."""
    if row.kind != "scheduled_task" or not row.payload_json:
        return
    try:
        from chat.models import ScheduledTaskOccurrence

        occurrence_id = json.loads(row.payload_json)["occurrence_id"]
        occurrence = session.get(ScheduledTaskOccurrence, occurrence_id)
        if occurrence is not None:
            occurrence.status = status
            occurrence.last_error = error[:500] if error else None
            session.add(occurrence)
    except Exception:
        # Delivery state on the outbox is authoritative. A corrupt legacy
        # payload must not roll back that state transition or cause a resend.
        logger.exception(
            "outbox: failed to record scheduled outcome for row %s", row.id
        )


def enqueue_message(
    session: Session,
    channel_id: str,
    *,
    content: str | None = None,
    embed: dict | None = None,
    level: str = "info",
    kind: str = "",
    payload: dict | None = None,
    dedupe_key: str | None = None,
) -> None:
    """Enqueue a Discord post. Exactly one of content/embed must be set.

    Caller is responsible for committing the session (matches the rest of the
    chat write API). The leader's drain loop posts the row asynchronously.

    ``kind``/``payload`` tag a row for post-processing after it posts: the
    observer enqueues a content post with ``kind="directive_proposal"`` and a
    payload the drain's post-hook reads to wire the propose-then-confirm flow
    (see ``_run_directive_proposal_hook``).
    """
    if (content is None) == (embed is None):
        raise ValueError("enqueue_message requires exactly one of content/embed")
    session.add(
        DiscordOutbox(
            channel_id=channel_id,
            content=content,
            embed_json=json.dumps(embed) if embed is not None else None,
            level=level,
            kind=kind,
            payload_json=json.dumps(payload) if payload is not None else None,
            dedupe_key=dedupe_key,
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


def enqueue_edit(
    session: Session,
    channel_id: str,
    message_id: str,
    content: str,
) -> None:
    """Enqueue an in-place edit of ``message_id`` in ``channel_id`` to ``content``.

    An edit row carries a target_message_id AND content but no reaction; that
    tuple is otherwise unused (a post row never sets target_message_id, a reaction
    row never sets content), so the drain distinguishes it without a discriminator
    column. Used off-loop by the goose runner to settle a run's single live
    message to its final result, so the checklist message and the result are one
    message rather than two. Caller commits the session.
    """
    session.add(
        DiscordOutbox(
            channel_id=channel_id,
            target_message_id=message_id,
            content=content,
        )
    )


def _claim_pending(engine) -> list[dict]:
    """Read the oldest unposted, not-exhausted rows. Returns plain dicts so the
    async drain never holds an ORM row across an await."""
    with Session(engine) as session:
        # A scheduled row left in sending across a process boundary has an
        # unknowable Discord outcome. Quarantine it rather than risk a second
        # post. This is deliberately at-most-once after dispatch begins, not a
        # false exactly-once claim.
        interrupted = session.exec(
            select(DiscordOutbox).where(DiscordOutbox.delivery_state == "sending")
        ).all()
        for row in interrupted:
            row.delivery_state = "uncertain"
            row.uncertain_at = datetime.now(timezone.utc)
            row.last_error = "process restarted while Discord send outcome was unknown"
            _record_scheduled_outcome(session, row, "uncertain", row.last_error)
            session.add(row)
        if interrupted:
            session.commit()
        rows = session.exec(
            select(DiscordOutbox)
            .where(DiscordOutbox.posted_at.is_(None))
            .where(DiscordOutbox.delivery_state == "pending")
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
                "kind": r.kind,
                "payload_json": r.payload_json,
                "dedupe_key": r.dedupe_key,
                "delivery_state": r.delivery_state,
            }
            for r in rows
        ]


def _mark_posted(engine, row_id: int) -> None:
    with Session(engine) as session:
        row = session.get(DiscordOutbox, row_id)
        if row is not None:
            row.posted_at = datetime.now(timezone.utc)
            row.delivery_state = "posted"
            _record_scheduled_outcome(session, row, "delivered")
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


def _mark_sending(engine, row_id: int) -> bool:
    """Durably cross the no-retry boundary for a scheduled Discord send."""
    from sqlalchemy import update

    with Session(engine) as session:
        result = session.exec(
            update(DiscordOutbox)
            .where(DiscordOutbox.id == row_id)
            .where(DiscordOutbox.delivery_state == "pending")
            .values(
                delivery_state="sending",
                send_started_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
        return result.rowcount == 1


def _mark_uncertain(engine, row_id: int, error: str) -> None:
    with Session(engine) as session:
        row = session.get(DiscordOutbox, row_id)
        if row is not None and row.posted_at is None:
            row.delivery_state = "uncertain"
            row.uncertain_at = datetime.now(timezone.utc)
            row.attempts += 1
            row.last_error = f"Discord send outcome unknown: {error}"[:500]
            _record_scheduled_outcome(session, row, "uncertain", row.last_error)
            session.add(row)
            session.commit()


_LEVEL_PREFIX = {"info": "", "warn": "⚠️ ", "error": "\U0001f534 "}


async def _resolve_channel(bot, row: dict):
    channel = bot.get_channel(int(row["channel_id"]))
    if channel is None:
        channel = await bot.fetch_channel(int(row["channel_id"]))
    return channel


async def _post_row(bot, row: dict, *, channel=None):
    """Post one outbox row via the bot. Mirrors chat.bot.send_message's channel
    resolution (cache, then API fetch).

    Returns the ``discord.Message`` a content/embed post created (so a post-hook
    like the directive-proposal wiring can react on it), or ``None`` for a
    reaction/edit row, which mutates an existing message rather than creating one.
    """
    import discord

    if channel is None:
        channel = await _resolve_channel(bot, row)
    if row.get("reaction") is not None:
        await _apply_reaction(bot, channel, row)
        return None
    elif row.get("target_message_id") is not None and row["content"] is not None:
        await _apply_edit(bot, channel, row)
        return None
    elif row["embed_json"] is not None:
        embed = discord.Embed.from_dict(json.loads(row["embed_json"]))
        return await channel.send(embed=embed)
    else:
        prefix = _LEVEL_PREFIX.get(row["level"], "")
        return await channel.send(f"{prefix}{row['content']}")


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


async def _apply_edit(bot, channel, row: dict) -> None:
    """Overwrite a target message's content in place.

    A missing *message* resolves the row (nothing to edit) rather than burning the
    retry budget: the run's single live message was deleted, so there is nothing
    to settle. An edit failure propagates so the drain retries it."""
    import discord

    try:
        message = await channel.fetch_message(int(row["target_message_id"]))
    except discord.NotFound:
        logger.debug("outbox: edit target %s gone; skipping", row["target_message_id"])
        return
    await message.edit(content=row["content"])


# The rejection copy the interactive bot path uses when the style-only guard
# blocks a proposed directive (chat.bot); the drain hook reuses it verbatim so a
# guard-rejected observer proposal reads identically to a rejected /agent one.
_DIRECTIVE_REJECTED = (
    "That change was rejected (it tried to alter tools, permissions, or access)."
)


async def _run_directive_proposal_hook(row: dict, posted_message) -> None:
    """Wire the propose-then-confirm flow onto a freshly-posted directive proposal.

    The observer cannot know the Discord message id until the leader posts the
    summary, so it enqueues a ``kind="directive_proposal"`` content row and lets
    this hook, running right after the post, stage the proposal keyed on that
    message id and seed 👍/👎 (or edit to the rejection copy if the style-only
    guard, re-run inside ``propose_update`` for defense in depth, blocks it).

    Never raises: any failure here (bad payload, a Discord hiccup adding the
    reactions) is logged and swallowed so the caller still marks the row posted
    and the drain keeps going, exactly as the task requires. propose_update opens
    its own session, so it goes through asyncio.to_thread (no session crosses the
    await, no sync Session in this async def).
    """
    from chat import directives

    try:
        payload = json.loads(row["payload_json"] or "{}")
        channel_id = payload["channel_id"]
        directive_change = payload["directive_change"]
        motivating_message_id = payload.get("motivating_message_id", "")
        ok, _ = await asyncio.to_thread(
            directives.propose_update,
            channel_id,
            directive_change,
            "observer",
            motivating_message_id,
            str(posted_message.id),
        )
        if ok:
            await posted_message.add_reaction("👍")
            await posted_message.add_reaction("👎")
        else:
            await posted_message.edit(content=_DIRECTIVE_REJECTED)
    except Exception:
        logger.exception(
            "outbox: directive-proposal hook failed for row %s", row.get("id")
        )


async def drain_once(bot, engine) -> int:
    """Post every currently-pending row. Returns the number posted."""
    rows = await asyncio.to_thread(_claim_pending, engine)
    posted = 0
    for row in rows:
        scheduled = row.get("kind") == "scheduled_task"
        channel = None
        if scheduled:
            # Channel lookup is known to happen before dispatch. A failure here
            # is safely retryable and should not be labelled an uncertain send.
            try:
                channel = await _resolve_channel(bot, row)
            except Exception as exc:  # noqa: BLE001 - row failure is isolated
                logger.warning(
                    "outbox: failed to resolve channel for row %s: %s",
                    row["id"],
                    exc,
                )
                await asyncio.to_thread(_mark_failed, engine, row["id"], str(exc))
                continue
            if not await asyncio.to_thread(_mark_sending, engine, row["id"]):
                continue
        try:
            posted_message = await _post_row(bot, row, channel=channel)
        except Exception as exc:  # noqa: BLE001 - a bad row must not stall the rest
            logger.warning("outbox: failed to post row %s: %s", row["id"], exc)
            if scheduled:
                await asyncio.to_thread(_mark_uncertain, engine, row["id"], str(exc))
            else:
                await asyncio.to_thread(_mark_failed, engine, row["id"], str(exc))
        else:
            if row.get("kind") == "directive_proposal" and posted_message is not None:
                await _run_directive_proposal_hook(row, posted_message)
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
