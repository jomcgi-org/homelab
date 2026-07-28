"""Chat domain leader-elected singletons (moved verbatim from app/main.py).

Everything here runs on exactly one replica at a time (the elected leader):
the Discord bot, the outbox drain that posts rows enqueued by any replica or
Argo job, the goosecracker orphaned-turn reclaim sweep, and the bot-coupled
message-lock sweep. The framework invokes ``leader_start`` on acquire and
``leader_stop`` on resign/shutdown (see framework/core.py).
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI

from framework import log_task_exception

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
    from sqlmodel import Session

    from core.db import get_engine

    discord_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not discord_token:
        return []

    from chat.acl import bootstrap_defaults
    from chat.bot import create_bot
    from chat.summarizer import build_llm_caller
    from chat.summarizer import on_startup as chat_startup

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
    with Session(get_engine()) as session:
        chat_startup(session, bot=bot, llm_call=build_llm_caller())

    async def _start_bot_when_ready():
        await wait_for_sidecar()
        await bot.start(discord_token)

    bot_task = asyncio.create_task(_start_bot_when_ready())
    bot_task.add_done_callback(log_task_exception)
    tasks.append(bot_task)
    logger.info("Discord bot starting")

    # Leader-only Discord outbox drain: posts rows enqueued by any replica
    # or Argo job (notify, changelog) through this pod's bot connection.
    from chat.outbox import run_outbox_drain

    drain_task = asyncio.create_task(run_outbox_drain(bot, get_engine()))
    drain_task.add_done_callback(log_task_exception)
    tasks.append(drain_task)
    logger.info("Discord outbox drain starting")

    # Reclaim goosecracker agent turns orphaned by the prior owner's death
    # (this replica just became leader, so any turn still marked running was
    # owned by a process that is gone). Re-dispatches them so queued replies
    # do not wedge forever on ⏳. One-shot; non-fatal so a sweep failure never
    # blocks startup. Runs off the loop (sync DB work) via to_thread.
    try:
        from chat.api import reclaim_orphaned_agent_sessions

        reclaimed = await asyncio.to_thread(reclaim_orphaned_agent_sessions)
        if reclaimed:
            logger.info("Reclaimed %d orphaned goosecracker turn(s)", reclaimed)
    except Exception:
        logger.exception("goosecracker: orphaned-turn reclaim sweep failed")

    # Bot-coupled lock sweep (reclaims expired message locks via SKIP LOCKED).
    async def _lock_sweep_loop():
        from chat.store import MessageStore
        from shared.embedding import EmbeddingClient

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
                        # reprocess_message re-fetches the message by int()
                        # channel/message snowflake. A WhatsApp lock carries a
                        # string group JID (e.g. "...@g.us"), processed inline
                        # by the inbound handler (not re-fetchable via the
                        # Discord bot), so skip it -- otherwise a single
                        # non-numeric lock raises ValueError and poisons the
                        # whole sweep, blocking Discord reclaim + cleanup too.
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
