"""Scheduler domain API: registry metadata, rows, and Argo run-now submission.

The in-process dispatch loop was deleted when scheduled execution moved to Argo
CronWorkflows (see app/jobs_main.py). Startup registration now feeds only the
scheduler views and agent orphan-job check. It never executes a handler.
"""

import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlmodel import Field, Session, SQLModel

if TYPE_CHECKING:
    from scheduler.service import RunNowResult
    from scheduler.views import SchedulerJobView

logger = logging.getLogger("monolith.scheduler")


def argo_handled(job_name: str) -> bool:
    """True if a replacing Argo CronWorkflow owns this job's execution path.

    ``ARGO_JOBS`` is derived from every chart CronWorkflow with ``replaces`` set,
    including suspended manual-only entries. Startup callers skip registry and
    row metadata for those names. ``suspend`` controls recurring Argo execution,
    not membership here, and there is no in-process dispatch loop.
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

# In-memory handler registry, populated at startup for views and orphan checks.
_registry: dict[str, Handler] = {}

# Legacy registration metadata. No current dispatcher consumes this set; Argo
# Workflow resources and concurrency policies govern actual batch execution.
_heavy: set[str] = set()


def is_registered(name: str) -> bool:
    """True if a handler is registered for name (public view of the registry)."""
    return name in _registry


def is_heavy(name: str) -> bool:
    """True if legacy registration metadata flags the job as memory-heavy."""
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
    """Record handler metadata and upsert its legacy scheduler row.

    This function does not schedule or execute the handler. ``heavy`` is retained
    as legacy metadata only; Argo owns execution controls for migrated jobs.

    Jobs with a replacing Argo CronWorkflow (listed in ``ARGO_JOBS`` regardless
    of ``suspend``) are skipped. This is centralized so every module's
    ``on_startup_jobs`` gets the metadata suppression for free. A previous row
    remains until the orphan cleanup removes it.
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


async def run_now(session: Session, name: str) -> "RunNowResult":
    """Submit the Argo CronWorkflow replacing ``name`` as a one-off Workflow.

    Cross-domain entry point (agent checks, the MCP trigger tool). The
    implementation lives in ``scheduler.service``, imported lazily because
    that module imports this one.
    """
    from scheduler.service import run_now as _run_now  # noqa: PLC0415

    return await _run_now(session, name)
