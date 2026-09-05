"""HTTP routes for the scheduler API (``/api/scheduler/jobs/...``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from core.db import get_session
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
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a job's Argo CronWorkflow as a one-off Workflow",
)
async def run_now(
    name: str,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    """Run a job now by submitting its Argo CronWorkflow as a one-off Workflow."""
    result = await service.run_now(session, name)
    if result.status_code != status.HTTP_202_ACCEPTED:
        raise HTTPException(status_code=result.status_code, detail=result.message)
    assert result.workflow_name is not None
    return {
        "job": result.job,
        "workflow_name": result.workflow_name,
        "namespace": result.namespace,
    }
