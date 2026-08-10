from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from swarm import config, runtime
from goosecracker.api import REPO_CATALOG

router = APIRouter(prefix="/api/swarm", tags=["swarm"])
logger = logging.getLogger(__name__)


class RunRequest(BaseModel):
    task: str
    repo: str
    branch: str
    idempotency_key: str | None = None
    budget_usd: float | None = None


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


def _server_app_version() -> str:
    return os.environ.get("APP_VERSION", os.environ.get("GIT_SHA", "unknown"))


def _session_rows(workflow_id: str):
    from agent_sessions import store
    from core.db import get_engine
    from sqlmodel import Session

    with Session(get_engine()) as session:
        return store.sessions_for_workflow(session, workflow_id)


@router.post("/runs")
def start_run(request: RunRequest) -> dict:
    if request.repo not in REPO_CATALOG:
        raise HTTPException(status_code=400, detail=f"unknown repo {request.repo}")
    if request.budget_usd is not None and request.budget_usd <= 0:
        raise HTTPException(status_code=400, detail="budget_usd must be positive")
    from swarm.workflows import implement_then_review

    dbos = _dbos()
    from dbos import SetWorkflowID

    context = (
        SetWorkflowID(request.idempotency_key) if request.idempotency_key else None
    )
    if context:
        with context:
            handle = dbos.start_workflow(
                implement_then_review,
                request.task,
                request.repo,
                request.branch,
                request.budget_usd,
            )
    else:
        handle = dbos.start_workflow(
            implement_then_review,
            request.task,
            request.repo,
            request.branch,
            request.budget_usd,
        )
    return {"workflow_id": handle.workflow_id}


@router.get("/runs/{workflow_id}")
def get_run(workflow_id: str) -> dict:
    from swarm.view import compose_run

    dbos = _dbos()
    try:
        result = compose_run(
            dbos, workflow_id, _session_rows(workflow_id), _server_app_version()
        )
    except Exception as exc:  # DBOS uses a runtime-specific missing-workflow error.
        if "not found" in str(exc).lower() or "non-existent" in str(exc).lower():
            raise HTTPException(status_code=404, detail="workflow not found") from exc
        raise
    if result is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return result


@router.get("/runs")
def list_runs(active: bool = True) -> dict:
    from swarm.view import compose_master

    return compose_master(_dbos(), active, {}, _server_app_version())


@router.post("/runs/{workflow_id}/cancel")
async def cancel_run(workflow_id: str, request: Request) -> dict:
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
    actor = request.headers.get("Cf-Access-Authenticated-User-Email") or "operator"
    try:
        await dbos.update_workflow_attributes_async(
            workflow_id,
            {
                "cancelled_by": {
                    "actor": actor,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
    except Exception:  # noqa: BLE001 - cancellation must not fail on metadata
        logger.warning(
            "failed to record cancellation actor for workflow %s",
            workflow_id,
            exc_info=True,
        )
    return {
        "workflow_id": workflow_id,
        "cancelled": True,
        "guest_sessions": guest_sessions,
    }
