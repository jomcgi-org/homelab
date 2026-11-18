"""Chat domain leader-elected singletons (moved verbatim from app/main.py).

Everything here runs on exactly one replica at a time (the elected leader):
the Discord bot, the outbox drain that posts rows enqueued by any replica or
Argo job and the bot-coupled message-lock sweep. With
``DISCORD_GATEWAY_ENABLED=false`` the bot logs in over REST only: the outbox
drain and the scheduler still run, and the gateway work (replies, commands,
ambient processing, the lock sweep) does not. The framework invokes
``leader_start`` on acquire and
``leader_stop`` on resign/shutdown (see framework/core.py).
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI
from framework import log_task_exception, register_leader_tasks

logger = logging.getLogger("monolith.chat.leader")


async def wait_for_sidecar() -> None:
    """Block until the frontend sidecar is healthy, or return immediately if unconfigured."""
    url = os.environ.get("FRONTEND_HEALTH_URL", "")
    if not url:
        return
    import httpx

    logger.info("Waiting for frontend sidecar at %s", url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        while True:
            try:
                resp = await client.get(url, timeout=2)
                if resp.status_code < 500:
                    logger.info("Frontend sidecar is ready")
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2)


async def leader_start(app: FastAPI) -> list[asyncio.Task]:
    """Start the chat singletons on the elected leader. Returns spawned tasks.

    No-op (returns []) when DISCORD_BOT_TOKEN is not configured: every chat
    singleton is coupled to the bot connection.
    """
    from core.db import get_engine
    from sqlmodel import Session

    discord_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not discord_token:
        return []

    from chat.acl import bootstrap_defaults
    from chat.bot import create_bot

    tasks: list[asyncio.Task] = []

    # Idempotently seed the default Discord feature grants (ADR 029) before
    # the bot accepts commands, so the home server + owner work out of the
    # box. Non-fatal: a failed seed (e.g. DB briefly unreachable) leaves the
    # ACL fail-closed and re-seeds on the next restart, rather than taking
    # down the bot.
    try:
        bootstrap_defaults()
    except Exception:
        logger.exception("acl: failed to seed default feature grants; continuing")

    bot = create_bot()
    app.state.bot = bot

    # Discord gives every process with the token its own gateway session, and
    # each one receives every message. When another process (the factory's
    # Discord bridge, HOME-162) holds the session, this one must not open a
    # second, or each tag gets two replies. login() alone is REST: it fetches
    # channels and sends, and on_ready (the command sync) never runs.
    gateway = os.environ.get("DISCORD_GATEWAY_ENABLED", "true").lower() != "false"

    async def _start_bot_when_ready():
        if not gateway:
            await bot.login(discord_token)
            logger.info("Discord bot logged in over REST only; gateway off")
            return
        await wait_for_sidecar()
        await bot.start(discord_token)

    def _ready() -> bool:
        return bot.is_ready() if gateway else bot.user is not None

    bot_task = asyncio.create_task(_start_bot_when_ready())
    bot_task.add_done_callback(log_task_exception)
    register_leader_tasks(app, [bot_task])
    tasks.append(bot_task)
    logger.info("Discord bot starting (gateway=%s)", gateway)

    # Leader-only Discord outbox drain: posts rows enqueued by any replica
    # or Argo job (notify, changelog) through this pod's bot connection.
    from chat.outbox import run_outbox_drain

    drain_task = asyncio.create_task(run_outbox_drain(bot, get_engine(), ready=_ready))
    drain_task.add_done_callback(log_task_exception)
    register_leader_tasks(app, [drain_task])
    tasks.append(drain_task)
    logger.info("Discord outbox drain starting")

    # Persisted reminder/digest scheduler. Claims are database-leased, so a
    # leader transition can recover abandoned work without double-enqueueing
    # an occurrence. The framework tracks and cancels this task on resign or
    # shutdown alongside the bot and outbox drain.
    from chat.scheduled_tasks import run_scheduler

    # Without the gateway nothing new reaches chat.messages, so digests end
    # (Joe, 2026-11-18, #8189); reminders keep running. Retire them before the
    # scheduler starts, so the boundary exists from the first moment the
    # gateway is off; each scheduler pass repeats it.
    if not gateway:
        from chat.scheduled_tasks import retire_digests_with_engine

        # A few tries before the scheduler starts. If the database still
        # fails, the scheduler retires first on every pass and claims nothing
        # until that succeeds, so no digest runs unretired here.
        for attempt in range(3):
            try:
                await asyncio.to_thread(retire_digests_with_engine, get_engine())
                break
            except Exception:
                logger.exception("scheduled digests not retired (try %d)", attempt + 1)
                await asyncio.sleep(2)
    scheduler_task = asyncio.create_task(run_scheduler(get_engine(), digests=gateway))
    scheduler_task.add_done_callback(log_task_exception)
    register_leader_tasks(app, [scheduler_task])
    tasks.append(scheduler_task)
    logger.info("Discord scheduled-task drain starting")

    if not gateway:
        # The lock sweep re-processes messages the gateway received.
        return tasks

    # Bot-coupled lock sweep (reclaims expired message locks via SKIP LOCKED).
    async def _lock_sweep_loop():
        from shared.embedding import EmbeddingClient

        from chat.store import MessageStore

        embed_client = EmbeddingClient()
        while not bot.is_ready():
            await asyncio.sleep(2)
        while True:
            await asyncio.sleep(30)
            try:
                with Session(get_engine()) as session:
                    store = MessageStore(session=session, embed_client=embed_client)
                    expired = store.reclaim_expired(ttl_seconds=30, limit=5)
                    for lock in expired:
                        # Reclaim-and-reprocess is Discord-specific:
                        # reprocess_message re-fetches by numeric channel and
                        # message snowflakes. Skip non-numeric channel ids so one
                        # malformed lock cannot block reclaim and cleanup.
                        if not str(lock.channel_id).isdigit():
                            continue
                        logger.info(
                            "Reclaiming expired lock for message %s",
                            lock.discord_message_id,
                        )
                        await bot.reprocess_message(
                            lock.discord_message_id, lock.channel_id
                        )
                    cleaned = store.cleanup_completed(max_age_seconds=3600)
                    if cleaned:
                        logger.debug("Cleaned up %d completed locks", cleaned)
            except Exception:
                logger.exception("Lock sweep failed")

    sweep_task = asyncio.create_task(_lock_sweep_loop())
    sweep_task.add_done_callback(log_task_exception)
    register_leader_tasks(app, [sweep_task])
    tasks.append(sweep_task)
    logger.info("Message lock sweep started (30s interval)")

    return tasks


async def leader_stop(app: FastAPI) -> None:
    """Stop the chat singletons. Idempotent; runs on resign or shutdown."""
    bot = getattr(app.state, "bot", None)
    if bot is not None:
        try:
            await bot.close()
        except Exception:
            logger.exception("Discord bot close failed")
        app.state.bot = None
