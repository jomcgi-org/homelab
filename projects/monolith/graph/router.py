from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from graph import config, runtime
from goosecracker.api import REPO_CATALOG

router = APIRouter(prefix="/api/graph", tags=["graph"])


class RunRequest(BaseModel):
    task: str
    repo: str
    branch: str
    idempotency_key: str | None = None


def _dbos():
    if not config.enabled():
        raise HTTPException(status_code=503, detail="Graph workflows are disabled")
    instance = runtime.init_dbos()
    if instance is None:
        raise HTTPException(status_code=503, detail="Graph DBOS is not configured")
    if not runtime.is_launched():
        # A follower replica: the router is mounted everywhere but DBOS only
        # launches on the leader. Submitting here would target an unlaunched
        # runtime, so say so rather than failing obscurely downstream.
        raise HTTPException(
            status_code=503, detail="Graph DBOS is not launched on this replica"
        )
    return instance


@router.post("/runs")
def start_run(request: RunRequest) -> dict:
    if request.repo not in REPO_CATALOG:
        raise HTTPException(status_code=400, detail=f"unknown repo {request.repo}")
    from graph.workflows import implement_then_review

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
def cancel_run(workflow_id: str) -> dict:
    _dbos().cancel_workflow(workflow_id)
    # Cancelling the workflow does NOT reap the guest session it started, and
    # parked sessions count against the live capacity cap, so a cancelled graph
    # can still deny creates cluster-wide. The compensating control-plane delete
    # is tracked in #4578. Reported honestly rather than claiming a clean stop.
    return {
        "workflow_id": workflow_id,
        "cancelled": True,
        "guest_session_reaped": False,
    }


def _status_payload(status) -> dict:
    return {
        "workflow_id": status.workflow_id,
        "status": status.status,
        "result": getattr(status, "output", None),
        "error": getattr(status, "error", None),
    }
