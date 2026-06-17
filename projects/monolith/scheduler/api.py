"""Scheduler domain API: Postgres-backed job scheduler with distributed locking."""

import asyncio
import logging
import platform
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from sqlmodel import Field, Session, SQLModel, select, text

from app.db import get_engine

logger = logging.getLogger("monolith.scheduler")

_HOSTNAME = platform.node()


# nosemgrep: sqlmodel-datetime-without-factory (last_run_at/locked_at are intentionally NULL until set)
class ScheduledJob(SQLModel, table=True):
    __tablename__ = "scheduled_jobs"
    __table_args__ = {"schema": "scheduler", "extend_existing": True}

    name: str = Field(primary_key=True)
    interval_secs: int
    next_run_at: datetime
    last_run_at: datetime | None = None
    last_status: str | None = None
    locked_by: str | None = None
    locked_at: datetime | None = None
    ttl_secs: int = Field(default=1200)


# Handler signature: receives a Session, returns optional next_run_at override.
# Stateless handlers that don't need a session should be wrapped at the call
# site (e.g. ``handler=lambda _: my_handler()``).
Handler = Callable[[Session], Awaitable[datetime | None]]

# In-memory handler registry (populated at startup)
_registry: dict[str, Handler] = {}


def is_registered(name: str) -> bool:
    """True if a handler is registered for name (public view of the registry)."""
    return name in _registry


def registered_names() -> list[str]:
    """Names of all jobs with a registered handler."""
    return list(_registry)


def register_job(
    session: Session,
    *,
    name: str,
    interval_secs: int,
    handler: Handler,
    ttl_secs: int = 1200,
) -> None:
    """Register a job handler and upsert its row in the database."""
    _registry[name] = handler

    now = datetime.now(timezone.utc)
    # Upsert: insert if new, update interval/ttl if changed, preserve timing
    existing = session.get(ScheduledJob, name)
    if existing:
        existing.interval_secs = interval_secs
        existing.ttl_secs = ttl_secs
        session.add(existing)
    else:
        session.add(
            ScheduledJob(
                name=name,
                interval_secs=interval_secs,
                next_run_at=now,
                ttl_secs=ttl_secs,
            )
        )
    session.commit()
    logger.info(
        "Registered job %s (interval=%ds, ttl=%ds)", name, interval_secs, ttl_secs
    )


def purge_stale_jobs(session: Session) -> None:
    """Delete DB rows for jobs that have no registered handler.

    Call after all register_job() calls are complete to clean up jobs
    from previous configs (e.g. removed changelog channels).
    """
    all_jobs = session.exec(select(ScheduledJob)).all()
    for job in all_jobs:
        if job.name not in _registry:
            logger.info("Purging stale job %s (no handler registered)", job.name)
            session.delete(job)
    session.commit()


async def run_scheduler_loop(poll_interval: int = 30, max_concurrent: int = 5) -> None:
    """Poll for due jobs and run them with bounded concurrency. Runs forever."""
    logger.info(
        "Scheduler loop started (poll=%ds, max_concurrent=%d)",
        poll_interval,
        max_concurrent,
    )
    while True:
        try:
            await dispatch_due_jobs(max_concurrent=max_concurrent)
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(poll_interval)


async def dispatch_due_jobs(max_concurrent: int = 5) -> int:
    """Claim every currently-due job and run it, up to ``max_concurrent`` in
    parallel. Awaits all spawned handlers before returning.

    Each handler runs on its own ``Session`` because SQLAlchemy sessions are
    not safe to share across concurrently awaiting coroutines.
    """
    if max_concurrent < 1:
        raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")

    job_names: list[str] = []
    with Session(get_engine()) as session:
        while True:
            name = _claim_next_job(session)
            if name is None:
                break
            job_names.append(name)

    if not job_names:
        return 0

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _run(name: str) -> None:
        async with semaphore:
            await _run_claimed_job(name)

    await asyncio.gather(*(_run(name) for name in job_names))
    return len(job_names)


async def _run_claimed_job(job_name: str) -> None:
    """Execute a single already-claimed job in its own DB session."""
    with Session(get_engine()) as session:
        job = session.get(ScheduledJob, job_name)
        if job is None:
            return

        handler = _registry.get(job_name)
        if handler is None:
            logger.warning("No handler registered for job %s", job_name)
            _release_lock(session, job)
            return

        try:
            override = await handler(session)
            _complete_job(session, job, override)
        except Exception as exc:
            logger.exception("Job %s failed", job_name)
            _fail_job(session, job, str(exc))


def _claim_next_job(session: Session) -> str | None:
    """Claim the next due job using SELECT FOR UPDATE SKIP LOCKED."""
    now = datetime.now(timezone.utc)
    result = session.execute(
        text("""
            UPDATE scheduler.scheduled_jobs
            SET locked_by = :hostname, locked_at = :now
            WHERE name = (
                SELECT name FROM scheduler.scheduled_jobs
                WHERE next_run_at <= :now
                  AND (locked_by IS NULL
                       OR locked_at < :now - make_interval(secs => ttl_secs))
                ORDER BY next_run_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING name
        """),
        {"hostname": _HOSTNAME, "now": now},
    )
    row = result.fetchone()
    session.commit()
    return row[0] if row else None


def _complete_job(
    session: Session, job: ScheduledJob, override: datetime | None
) -> None:
    """Mark a job as succeeded and advance next_run_at."""
    now = datetime.now(timezone.utc)
    job.locked_by = None
    job.locked_at = None
    job.last_run_at = now
    job.last_status = "ok"
    job.next_run_at = override or (now + timedelta(seconds=job.interval_secs))
    session.add(job)
    session.commit()
    logger.info(
        "Job %s completed, next run at %s", job.name, job.next_run_at.isoformat()
    )


def _fail_job(session: Session, job: ScheduledJob, error: str) -> None:
    """Mark a job as failed, still advance next_run_at to avoid blocking."""
    now = datetime.now(timezone.utc)
    job.locked_by = None
    job.locked_at = None
    job.last_run_at = now
    job.last_status = f"error: {error[:200]}"
    job.next_run_at = now + timedelta(seconds=job.interval_secs)
    session.add(job)
    session.commit()


def _release_lock(session: Session, job: ScheduledJob) -> None:
    """Release a lock without advancing the schedule (for missing handler)."""
    job.locked_by = None
    job.locked_at = None
    session.add(job)
    session.commit()
