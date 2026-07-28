"""Scheduler domain API: Postgres-backed job scheduler with distributed locking."""

import asyncio
import logging
import os
import platform
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlmodel import Field, Session, SQLModel, select, text

from core.db import get_engine

if TYPE_CHECKING:
    from scheduler.views import SchedulerJobView

logger = logging.getLogger("monolith.scheduler")

_HOSTNAME = platform.node()


def argo_handled(job_name: str) -> bool:
    """True if an active Argo CronWorkflow owns this job (set via ARGO_JOBS env).

    on_startup_jobs callers skip register_job for these so the job does not run
    both in-process and as a CronWorkflow. ARGO_JOBS is a comma-separated list of
    in-process job names, derived by the chart from the non-suspended
    cronWorkflows entries (see chart/templates/deployment.yaml).
    """
    handled = {
        n.strip() for n in os.environ.get("ARGO_JOBS", "").split(",") if n.strip()
    }
    return job_name in handled


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

# Names of jobs flagged memory-heavy at registration. The dispatcher runs at most
# ``max_heavy`` of these at once (default 1) so two big jobs (e.g. the FA2 graph
# layout) never pile up and OOMKill the shared pod. Light jobs stay fully
# parallel. Bounding concurrency by COUNT alone (the old semaphore) does not stop
# this: several heavy jobs can be admitted in one tick before any has spiked.
_heavy: set[str] = set()


def is_registered(name: str) -> bool:
    """True if a handler is registered for name (public view of the registry)."""
    return name in _registry


def is_heavy(name: str) -> bool:
    """True if the job is flagged memory-heavy (serialized against other heavies)."""
    return name in _heavy


def registered_names() -> list[str]:
    """Names of all jobs with a registered handler."""
    return list(_registry)


def list_jobs(session: Session) -> list["SchedulerJobView"]:
    """List all scheduled job rows as view models (cross-domain facade).

    Thin delegate to ``scheduler.service.list_jobs``, imported lazily to
    avoid a module-load cycle (``scheduler.service`` imports from this
    module). Other domains must call this instead of importing
    ``scheduler.service`` directly.
    """
    from scheduler.service import list_jobs as _list_jobs

    return _list_jobs(session)


def register_job(
    session: Session,
    *,
    name: str,
    interval_secs: int,
    handler: Handler,
    ttl_secs: int = 1200,
    heavy: bool = False,
) -> None:
    """Register a job handler and upsert its row in the database.

    Set ``heavy=True`` for memory-intensive jobs (e.g. the graph layout pass) so
    the dispatcher never co-schedules two of them.

    Jobs an active Argo CronWorkflow owns (listed in ARGO_JOBS) are skipped here
    so they never run both in-process and as a CronWorkflow. This is centralized
    so every module's on_startup_jobs gets the skip for free - callers do not
    each need to gate on argo_handled. A previously-registered row is left for
    purge_unregistered_jobs to drop on the next sweep.
    """
    if argo_handled(name):
        logger.info(
            "%s: owned by Argo CronWorkflow, skipping in-process register", name
        )
        return

    _registry[name] = handler
    if heavy:
        _heavy.add(name)
    else:
        _heavy.discard(name)

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


async def run_scheduler_loop(
    poll_interval: int = 30, max_concurrent: int = 5, max_heavy: int = 1
) -> None:
    """Poll for due jobs and run them with bounded concurrency. Runs forever."""
    logger.info(
        "Scheduler loop started (poll=%ds, max_concurrent=%d, max_heavy=%d)",
        poll_interval,
        max_concurrent,
        max_heavy,
    )
    while True:
        try:
            await dispatch_due_jobs(max_concurrent=max_concurrent, max_heavy=max_heavy)
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(poll_interval)


async def dispatch_due_jobs(max_concurrent: int = 5, max_heavy: int = 1) -> int:
    """Claim every currently-due job and run it, up to ``max_concurrent`` in
    parallel. Awaits all spawned handlers before returning.

    Concurrency is bounded two ways. ``max_concurrent`` caps total simultaneous
    handlers; ``max_heavy`` additionally caps how many memory-heavy jobs (those
    registered ``heavy=True``) run at once. A heavy job holds BOTH a heavy slot
    and a regular slot, so it still counts toward the total while guaranteeing no
    two big jobs (e.g. graph layout + a large rollup) overlap and OOMKill the
    shared pod. Light jobs are unaffected and stay fully parallel.

    Each handler runs on its own ``Session`` because SQLAlchemy sessions are
    not safe to share across concurrently awaiting coroutines.
    """
    if max_concurrent < 1:
        raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
    if max_heavy < 1:
        raise ValueError(f"max_heavy must be >= 1, got {max_heavy}")

    job_names: list[str] = []
    with Session(get_engine()) as session:
        while True:
            name = _claim_next_job(session)
            if name is None:
                break
            job_names.append(name)

    if not job_names:
        return 0

    slots = asyncio.Semaphore(max_concurrent)
    heavy_slots = asyncio.Semaphore(max_heavy)

    async def _run(name: str) -> None:
        if name in _heavy:
            # A heavy job takes a heavy slot first, then a regular slot, so it is
            # serialized against other heavies AND counts toward the total cap.
            async with heavy_slots, slots:
                await _run_claimed_job(name)
        else:
            async with slots:
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
