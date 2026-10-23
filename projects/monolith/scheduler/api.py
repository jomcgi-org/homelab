"""Scheduler domain API: retained rows and Argo run-now submission.

Scheduled execution uses Argo CronWorkflows invoking app/jobs_main.py. Legacy
scheduled_jobs rows remain readable and supply accepted names for run-now.
"""

import os
from datetime import datetime
from typing import TYPE_CHECKING

from sqlmodel import Field, Session, SQLModel

if TYPE_CHECKING:
    from scheduler.service import RunNowResult
    from scheduler.views import SchedulerJobView


def argo_handled(job_name: str) -> bool:
    """True if ARGO_JOBS accepts this name for manual Workflow submission.

    ``ARGO_JOBS`` is derived from every chart CronWorkflow with ``replaces`` set,
    including suspended manual-only entries. The same ``replaces`` annotation
    identifies the CronWorkflow to submit. ``suspend`` controls recurring Argo
    execution independently of membership here.
    """
    handled = {
        n.strip() for n in os.environ.get("ARGO_JOBS", "").split(",") if n.strip()
    }
    return job_name in handled


# last_run_at/locked_at are intentionally NULL until set
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


def list_jobs(session: Session) -> list["SchedulerJobView"]:
    """List all scheduled job rows as view models (cross-domain facade).

    Thin delegate to ``scheduler.service.list_jobs``, imported lazily to
    avoid a module-load cycle (``scheduler.service`` imports from this
    module). Other domains must call this instead of importing
    ``scheduler.service`` directly.
    """
    from scheduler.service import list_jobs as _list_jobs

    return _list_jobs(session)


async def run_now(session: Session, name: str) -> "RunNowResult":
    """Submit the Argo CronWorkflow replacing ``name`` as a one-off Workflow.

    Cross-domain entry point (agent checks, the MCP trigger tool). The
    implementation lives in ``scheduler.service``, imported lazily because
    that module imports this one.
    """
    from scheduler.service import run_now as _run_now  # noqa: PLC0415

    return await _run_now(session, name)
