from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from swarm import config, runtime
from goosecracker.api import REPO_CATALOG

router = APIRouter(prefix="/api/swarm", tags=["swarm"])


class RunRequest(BaseModel):
    task: str
    repo: str
    branch: str
    idempotency_key: str | None = None


def _dbos():
    if not config.enabled():
        raise HTTPException(status_code=503, detail="Swarm workflows are disabled")
    instance = runtime.init_dbos()
    if instance is None:
        raise HTTPException(status_code=503, detail="Swarm DBOS is not configured")
    if not runtime.is_launched():
        # A follower replica: the router is mounted everywhere but DBOS only
        # launches on the leader. Submitting here would target an unlaunched
        # runtime, so say so rather than failing obscurely downstream.
        raise HTTPException(
            status_code=503, detail="Swarm DBOS is not launched on this replica"
        )
    return instance


@router.post("/runs")
def start_run(request: RunRequest) -> dict:
    if request.repo not in REPO_CATALOG:
        raise HTTPException(status_code=400, detail=f"unknown repo {request.repo}")
    from swarm.workflows import implement_then_review

    dbos = _dbos()
    from dbos import SetWorkflowID

    context = (
        SetWorkflowID(request.idempotency_key) if request.idempotency_key else None
    )
    if context:
        with context:
            handle = dbos.start_workflow(
                implement_then_review, request.task, request.repo, request.branch
            )
    else:
        handle = dbos.start_workflow(
            implement_then_review, request.task, request.repo, request.branch
        )
    return {"workflow_id": handle.workflow_id}


@router.get("/runs/{workflow_id}")
def get_run(workflow_id: str) -> dict:
    status = _dbos().retrieve_workflow(workflow_id).get_status()
    return _status_payload(status)


@router.get("/runs")
def list_runs() -> list[dict]:
    return [
        _status_payload(status)
        for status in _dbos().list_workflows(limit=50, sort_desc=True)
    ]


@router.post("/runs/{workflow_id}/cancel")
async def cancel_run(workflow_id: str) -> dict:
    dbos = _dbos()
    # cancel_children: sessions are not created inline. The workflow enqueues
    # start_session_workflow as a CHILD on the codex queue, so cancelling only
    # the parent leaves a queued child that mints a fresh guest AFTER the reap
    # sweeps, recreating the orphan this endpoint exists to prevent.
    #
    # ..._async, not the sync call: DBOS's cancel_workflow starts with
    # check_async(), which raises whenever an event loop is running, so calling
    # it from this async handler would 500 every request without cancelling
    # anything. Unit tests with a fake DBOS cannot catch that.
    await dbos.cancel_workflow_async(workflow_id, cancel_children=True)
    # Cancel first so no new turn is scheduled while guest sessions are reaped.
    from agent_sessions.api import reap_sessions_for_workflow

    guest_sessions = await reap_sessions_for_workflow(workflow_id)
    return {
        "workflow_id": workflow_id,
        "cancelled": True,
        "guest_sessions": guest_sessions,
    }


def _status_payload(status) -> dict:
    # `error` is a live exception object (e.g. DBOSMaxStepRetriesExceeded), which
    # pydantic cannot serialize: returning it raw turned every failed run's
    # status into a 500, hiding the very failure the caller was asking about.
    error = getattr(status, "error", None)
    return {
        "workflow_id": status.workflow_id,
        "status": status.status,
        "result": getattr(status, "output", None),
        "error": None if error is None else f"{type(error).__name__}: {error}",
    }
