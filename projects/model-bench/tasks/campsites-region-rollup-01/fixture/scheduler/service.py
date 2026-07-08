"""Read + trigger operations for the scheduler API."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from scheduler.views import SchedulerJobView
from scheduler.api import ScheduledJob, is_registered


def _to_view(job: ScheduledJob) -> SchedulerJobView:
    return SchedulerJobView(
        name=job.name,
        interval_secs=job.interval_secs,
        ttl_secs=job.ttl_secs,
        next_run_at=job.next_run_at,
        last_run_at=job.last_run_at,
        last_status=job.last_status,
        has_handler=is_registered(job.name),
    )


def list_jobs(session: Session) -> list[SchedulerJobView]:
    rows = session.exec(select(ScheduledJob).order_by(ScheduledJob.name)).all()
    return [_to_view(r) for r in rows]


def get_job(session: Session, name: str) -> SchedulerJobView | None:
    job = session.get(ScheduledJob, name)
    return _to_view(job) if job else None


def mark_for_immediate_run(session: Session, name: str) -> SchedulerJobView | None:
    """Set ``next_run_at = now()`` so the next scheduler tick claims the job.

    Concurrency-safe by construction: ``scheduler.api._claim_next_job`` uses
    ``SELECT ... FOR UPDATE SKIP LOCKED``. If a tick is mid-flight on the same row,
    this UPDATE blocks until the tick commits, then runs against the freshly
    released row. No application-level lock check is needed.
    """
    job = session.get(ScheduledJob, name)
    if job is None:
        return None
    job.next_run_at = datetime.now(timezone.utc)
    session.add(job)
    session.commit()
    session.refresh(job)
    return _to_view(job)
