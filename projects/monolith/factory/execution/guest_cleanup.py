"""Leader-owned retries for settled workflow guests left behind by node cleanup."""

import asyncio
import logging

from sqlmodel import Session, select

from core.db import get_engine
from factory.execution import execution_api, store
from factory.execution.models import AgentSession

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 30
BATCH_SIZE = 16


def _candidates(after_id):
    with Session(get_engine()) as db:
        return db.exec(
            select(AgentSession.id)
            .where(
                AgentSession.id > after_id,
                *store.settled_guest_cleanup_conditions(),
            )
            .order_by(AgentSession.id)
            .limit(BATCH_SIZE)
        ).all()


async def sweep_once(after_id=0):
    """Bound each pass and advance past held rows so later guests cannot starve.

    Include unfenced bindings and cleanup claims: a prior pass may have released
    the fence and committed a claim before a DELETE or confirmation failed.
    """
    candidates = await asyncio.to_thread(_candidates, after_id)
    for session_id in candidates:
        try:
            await execution_api.reap_settled_session(session_id)
        except Exception as exc:
            if execution_api._lock_not_available(exc):
                logger.info(
                    "Guest cleanup deferred for session %s: pool busy", session_id
                )
            else:
                logger.exception("Guest cleanup failed for session %s", session_id)
    return candidates[-1] if len(candidates) == BATCH_SIZE else 0


async def _loop():
    after_id = 0
    while True:
        try:
            after_id = await sweep_once(after_id)
        except Exception as exc:
            if execution_api._lock_not_available(exc):
                logger.info("Guest cleanup sweep deferred: pool busy")
            else:
                logger.exception("Guest cleanup sweep failed")
        await asyncio.sleep(INTERVAL_SECONDS)


def start_guest_cleanup_loop():
    from framework import log_task_exception

    task = asyncio.create_task(_loop(), name="agent-guest-cleanup")
    task.add_done_callback(log_task_exception)
    return [task]
