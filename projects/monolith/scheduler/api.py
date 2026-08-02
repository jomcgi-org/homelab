"""Scheduler domain API: job registry + ScheduledJob rows (views + agent checks).

The in-process dispatch loop that used to claim and run these jobs was deleted
when the jobs moved to Argo CronWorkflows (see app/jobs_main.py); what remains
is the registry that feeds the scheduler views and the agent orphan-job check.
"""

import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlmodel import Field, Session, SQLModel

if TYPE_CHECKING:
    from scheduler.views import SchedulerJobView

logger = logging.getLogger("monolith.scheduler")


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
