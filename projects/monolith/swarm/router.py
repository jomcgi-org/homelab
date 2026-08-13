from __future__ import annotations

import logging
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
    """The app version DBOS is actually running, asked of DBOS itself.

    A workflow's `app_version` is a value DBOS computes (a hash over registered
    workflow source) unless `DBOS__APPVERSION` overrides it, and it is stored on
    `GlobalParams` at launch. It is not a chart version and not a git sha, so
    comparing it against an environment variable compares two different
    namespaces and can only ever be unequal.

    This previously read `APP_VERSION` then `GIT_SHA`, neither of which this
    chart sets, so it returned the literal "unknown" on every call. No real DBOS
    version equals "unknown", which made `compose_run` mark every PENDING or
    ENQUEUED run stranded, banner and all, while the run was still running.

    Returns "" when the version cannot be resolved, which the caller treats as
    "cannot tell" rather than as evidence of stranding. `GlobalParams` is a
    private module, hence the guarded import: there is no public accessor in
    dbos 2.29.0, and a restructure upstream must degrade to "cannot tell"
    instead of resurrecting the false positive.
    """
    try:
        from dbos._utils import GlobalParams

        return GlobalParams.app_version or ""
    except Exception:
        logger.warning("could not read the DBOS app version", exc_info=True)
        return ""


def _session_rows(workflow_id: str):
    from core.db import get_engine
    from sqlmodel import Session
    from swarm.rows import swarm_session_views

    with Session(get_engine()) as session:
        return swarm_session_views(session, workflow_id).get(workflow_id, [])


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
def list_runs(request: Request, active: bool = True, limit: int = 50) -> dict:
    from swarm.view import compose_master
    from core.db import get_engine
    from sqlmodel import Session
    from swarm.rows import swarm_session_views

    with Session(get_engine()) as session:
        session_costs = swarm_session_views(session)
    try:
        requested_limit = int(request.query_params.get("limit", limit))
    except (TypeError, ValueError):
        requested_limit = 50
    limit = max(1, min(50, requested_limit))
    return compose_master(
        _dbos(), active, session_costs, _server_app_version(), limit=limit
    )


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
