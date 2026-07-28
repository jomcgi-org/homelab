"""Scheduler job draining due reminders into the Discord outbox.

Runs as the chat-drain-reminders Argo CronWorkflow one-shot (see
app/jobs_main.py, chart/values.yaml jobs.cronWorkflows), not the legacy
in-process scheduler.register_job path: that dispatch loop was deleted (see
app/main.py's lifespan comment), so Argo's own cron schedule is the sole
cadence driver here and the handler's return value is unused by anything.

Pure DB work, no network phase, so the whole job runs in one worker thread via
asyncio.to_thread rather than splitting an async network phase from a sync DB
phase (mirrors ships.retention.partition_maintenance_handler, not
hikes.jobs which has a real network fetch to await first).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlmodel import Session

from chat import reminders

logger = logging.getLogger("monolith.chat")


def _drain_reminders_core(session: Session, now: datetime) -> datetime | None:
    """Deliver due reminders and report the earliest remaining pending due_at
    (or None). Session-parameterized so the SQLite create_all test fixture can
    drive it directly. Caller commits.
    """
    delivered = reminders.deliver_due(session, now)
    if delivered:
        logger.info("chat.reminders: delivered %d due reminder(s)", delivered)
    return reminders.next_due(session)


def _drain_reminders() -> datetime | None:
    """Open a fresh session and drain due reminders. Runs off the event loop
    via asyncio.to_thread (so it must own its session, never the caller's).
    """
    from core.db import get_engine

    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        next_run = _drain_reminders_core(session, now)
        session.commit()
    return next_run


async def drain_reminders_handler(session: Session) -> None:
    """Drain due reminders into the Discord outbox.

    All Session I/O runs in a worker thread (semgrep no-sync-session-in-async-def
    / no-session-in-to-thread); the ``session`` argument passed in by the
    one-shot CLI wrapper is unused and never touched here. The Argo cron
    schedule is what actually determines cadence, so unlike the legacy
    register_job contract there is no next-run hint to return.
    """
    await asyncio.to_thread(_drain_reminders)
