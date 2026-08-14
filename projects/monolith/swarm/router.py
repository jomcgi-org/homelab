from __future__ import annotations

import asyncio
import logging
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from goosecracker.api import REPO_CATALOG
from swarm import config, runtime
from swarm.compare_router import router as compare_router
from swarm.compare_router import compare_stats
from swarm.rationale import parse_rationale
from swarm.walkthrough_composer import compose_walkthrough

router = APIRouter(prefix="/api/swarm", tags=["swarm"])
logger = logging.getLogger(__name__)

router.include_router(compare_router)


@router.get("/walkthrough/{session_id}/{turn_seq}")
async def walkthrough(session_id: int, turn_seq: int) -> dict:
    """Return the composed session-tier walkthrough for one turn."""
    from swarm.compare_router import _decode, _turn_data

    data = await asyncio.to_thread(_turn_data, session_id, turn_seq)
    if data is None:
        raise HTTPException(status_code=404, detail="Agent turn not found")
    compare = await compare_stats(session_id, turn_seq)
    rationale = parse_rationale(data["result_text"])
    usage = _decode(data.get("usage_json"), {})
    return compose_walkthrough(session_id, turn_seq, compare, rationale, usage)


class RunRequest(BaseModel):
    task: str
    repo: str
    branch: str
    idempotency_key: str | None = None
    budget_usd: float | None = None
    model: str | None = None


class ClassifyAndStartRequest(BaseModel):
    task: str
    repo: str | None = None
    branch: str | None = None
    model: str
    budget_usd: float | None = None


class ClassifyAndStartResponse(BaseModel):
    task_id: str
    session_id: int | None = None
    workflow_id: str | None = None
    kind: str


class PromoteSessionRequest(BaseModel):
    session_id: int


class PromoteSessionResponse(BaseModel):
    workflow_id: str


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
    if request.model is not None:
        from agent_sessions import model_family

        try:
            model_family(request.model)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
                request.model,
            )
    else:
        handle = dbos.start_workflow(
            implement_then_review,
            request.task,
            request.repo,
            request.branch,
            request.budget_usd,
            request.model,
        )
    return {"workflow_id": handle.workflow_id}


def _create_task_sync(
    task_id: str,
    task: str,
    repo: str | None,
    branch: str | None,
    budget_usd: float | None,
) -> None:
    """Create task record, managing its own session."""
    from core.db import get_engine
    from sqlmodel import Session
    from swarm import models

    with Session(get_engine()) as db:
        models.create_task(
            task_id, task, repo, branch, "qwen3.6-27b", budget_usd, session=db
        )


def _record_classification_sync(
    task_id: str,
    task: str,
    classification: str,
    latency_ms: int,
    outcome: str,
    refusal_code: str | None,
    budget_usd: float | None,
) -> None:
    """Record classification and plan, managing its own session."""
    from core.db import get_engine
    from sqlmodel import Session
    from swarm import models

    with Session(get_engine()) as db:
        models.record_conductor_call(
            task_id,
            "qwen3.6-27b",
            "classify_task",
            json.dumps({"task": task}),
            outcome,
            refusal_code=refusal_code,
            version_before=None,
            version_after=None,
            latency_ms=latency_ms,
            session=db,
        )
        models.append_plan_version(
            task_id,
            1,
            "bootstrap",
            "system",
            "classifier",
            json.dumps({"classification": classification}),
            "classification",
            session=db,
        )
        if classification == "planned":
            models.upsert_plan_node(
                task_id,
                "implement",
                "research",
                task,
                "qwen3.6-27b",
                "[]",
                budget_usd if budget_usd is not None else 0,
                False,
                None,
                None,
                1,
                session=db,
            )


def _set_task_link_sync(task_id: str, **links) -> None:
    """Update task links, managing its own session."""
    from core.db import get_engine
    from sqlmodel import Session
    from swarm.models import update_task_links

    with Session(get_engine()) as db:
        update_task_links(task_id, session=db, **links)


@router.post("/classify-and-start", response_model=ClassifyAndStartResponse)
async def classify_and_start(request: Request, body: ClassifyAndStartRequest):
    task = body.task.strip()
    model = body.model.strip()
    if not task or not model:
        raise HTTPException(status_code=400, detail="task and model are required")
    if body.budget_usd is not None and body.budget_usd <= 0:
        raise HTTPException(status_code=400, detail="budget_usd must be positive")

    from swarm import classifier, models

    task_id = models.mint_task_id()
    await asyncio.to_thread(
        _create_task_sync, task_id, task, body.repo, body.branch, body.budget_usd
    )
    (
        classification,
        latency_ms,
        outcome,
        refusal_code,
    ) = await classifier.classify_task_with_outcome(task)
    await asyncio.to_thread(
        _record_classification_sync,
        task_id,
        task,
        classification,
        latency_ms,
        outcome,
        refusal_code,
        body.budget_usd,
    )

    if classification == "planned":
        if not body.repo or not body.branch:
            raise HTTPException(
                status_code=400, detail="planned tasks require repo and branch"
            )
        try:
            result = start_run(
                RunRequest(
                    task=task,
                    repo=body.repo,
                    branch=body.branch,
                    budget_usd=body.budget_usd,
                    model=model,
                )
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="swarm service unavailable"
            ) from exc
        await asyncio.to_thread(
            _set_task_link_sync, task_id, workflow_id=result["workflow_id"]
        )
        return ClassifyAndStartResponse(
            task_id=task_id, workflow_id=result["workflow_id"], kind="run"
        )

    from agent_sessions.router import StartRequest, start_session

    try:
        result = await start_session(
            request,
            StartRequest(
                prompt=task,
                model=model,
                repo=body.repo,
                branch=body.branch or "main",
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="session service unavailable"
        ) from exc
    if result.get("session_id") is None:
        raise HTTPException(
            status_code=503, detail=result.get("error", "session unavailable")
        )
    await asyncio.to_thread(
        _set_task_link_sync, task_id, session_id=result["session_id"]
    )
    return ClassifyAndStartResponse(
        task_id=task_id, session_id=result["session_id"], kind="session"
    )


@router.put("/promote-session", response_model=PromoteSessionResponse)
def promote_session(request: Request, body: PromoteSessionRequest):
    from agent_sessions.models import AgentSession, AgentTurn, PendingMessage
    from core.db import get_engine
    from sqlmodel import Session, select
    from swarm import models

    with Session(get_engine()) as db:
        row = db.get(AgentSession, body.session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent session not found")
        turn = db.exec(
            select(AgentTurn)
            .where(AgentTurn.session_id == row.id)
            .order_by(AgentTurn.seq)
        ).first()
        pending = db.exec(
            select(PendingMessage)
            .where(PendingMessage.session_id == row.id)
            .order_by(PendingMessage.seq)
        ).first()
        task = (
            turn.prompt if turn else pending.message_text if pending else ""
        ).strip()
        if not task:
            raise HTTPException(status_code=400, detail="session has no task text")
        repo, branch, model = row.repo, row.branch, row.model or "luna"
    if not repo or not branch:
        raise HTTPException(status_code=400, detail="session has no repo and branch")
    result = start_run(RunRequest(task=task, repo=repo, branch=branch, model=model))
    task_id = models.mint_task_id()
    models.create_task(
        task_id,
        task,
        repo,
        branch,
        "qwen3.6-27b",
        None,
        workflow_id=result["workflow_id"],
    )
    models.append_plan_version(
        task_id,
        1,
        "grow_from_session",
        "user",
        request.headers.get("Cf-Access-Authenticated-User-Email") or "operator",
        json.dumps({"from_session_id": body.session_id}),
        "promotion",
        stated_reason="user promoted session to run",
    )
    return PromoteSessionResponse(workflow_id=result["workflow_id"])


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
