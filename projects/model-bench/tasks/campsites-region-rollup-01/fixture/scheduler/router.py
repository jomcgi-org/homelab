"""HTTP routes for the scheduler API (``/api/scheduler/jobs/...``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.db import get_session
from scheduler import service
from scheduler.views import SchedulerJobView

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get(
    "/jobs",
    response_model=list[SchedulerJobView],
    summary="List all scheduled jobs",
)
def list_jobs(
    session: Session = Depends(get_session),
) -> list[SchedulerJobView]:
    return service.list_jobs(session)


@router.get(
    "/jobs/{name}",
    response_model=SchedulerJobView,
    summary="Get a single scheduled job by name",
)
def get_job(
    name: str,
    session: Session = Depends(get_session),
) -> SchedulerJobView:
    job = service.get_job(session, name)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {name}")
    return job


@router.post(
    "/jobs/{name}/run-now",
    response_model=SchedulerJobView,
    summary="Schedule a job to run on the next scheduler tick",
)
def run_now(
    name: str,
    session: Session = Depends(get_session),
) -> SchedulerJobView:
    job = service.mark_for_immediate_run(session, name)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {name}")
    return job
