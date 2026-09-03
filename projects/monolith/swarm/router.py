from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from goosecracker.api import REPO_CATALOG
from framework import log_task_exception
import shared.inference
from swarm import config, runtime
from swarm.compare_router import router as compare_router
from swarm.compare_router import compare_stats
from agent_sessions.rationale import parse_rationale
from swarm.steps import merge_workflow_attributes
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
    task_id: str | None = None
    repo: str | None = None
    branch: str | None = None
    model: str
    budget_usd: float | None = None


class ClassifyAndStartAccepted(BaseModel):
    task_id: str
    kind: str


class StartStatusResponse(BaseModel):
    kind: str
    session_id: int | None = None
    run_id: str | None = None
    needs_input: dict[str, bool] | None = None
    refusal_code: str | None = None
    message: str | None = None
    login_required: bool | None = None
    verification_url: str | None = None
    user_code: str | None = None
    grant: str | None = None
    login_message: str | None = None


@dataclass(frozen=True)
class _StartContext:
    task_id: str
    task: str
    repo: str | None
    branch: str | None
    model: str
    budget_usd: float | None
    triggered_by: str | None
    record_plan: bool = True
    has_classification: bool = False
    classification: str | None = None
    classification_outcome: str | None = None
    classification_refusal_code: str | None = None
    classification_latency_ms: int = 0


_CLASSIFIER_DEADLINE_SECONDS = 60.0
_CLASSIFY_STUCK_SECONDS = 90.0
_CLASSIFY_RESOLUTION_TIMEOUT_SECONDS = 180.0
_CLASSIFY_HARD_STUCK_SECONDS = 300.0
_CLASSIFICATION_TASKS: dict[str, asyncio.Task[None]] = {}
_IN_PROGRESS_STATES = {
    "classifying",
    "starting_session",
    "starting_run",
    "settling_needs_input",
    "settling_refused",
}


class PromoteSessionRequest(BaseModel):
    session_id: int


class PromoteSessionResponse(BaseModel):
    workflow_id: str


class DecisionRequest(BaseModel):
    decision: str
    note: str | None = Field(default=None, max_length=2000)


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


def _dbos_read():
    """Accessor for the READ surfaces, which do not submit anything.

    Composing a run view calls retrieve_workflow, list_workflows and
    list_workflow_steps and nothing else, so it needs the system database, not
    the leader's launched runtime. Sharing _dbos() with the submitting paths
    made console polls fail during HPA scale-out and rolling updates: a follower
    can be Ready behind the Service while the old pod still holds leadership.

    The leader still answers reads through its launched instance, so the client
    pool only ever exists where it is needed.
    """
    if not config.enabled():
        raise HTTPException(status_code=503, detail="Swarm workflows are disabled")
    if runtime.is_launched():
        instance = runtime.init_dbos()
        if instance is not None:
            return instance
    try:
        client = runtime.read_client()
    except Exception as error:  # noqa: BLE001
        logger.warning("could not connect a follower DBOS read client", exc_info=True)
        raise HTTPException(
            status_code=503, detail="Swarm DBOS is temporarily unavailable"
        ) from error
    if client is None:
        raise HTTPException(status_code=503, detail="Swarm DBOS is not configured")
    return client


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


def _decision_rows(workflow_id: str):
    from core.db import get_engine
    from sqlmodel import Session
    from swarm.store import list_open_decisions

    with Session(get_engine()) as session:
        return list_open_decisions(session, workflow_id)


def _decision_rows_for(workflow_ids: list[str]):
    from core.db import get_engine
    from sqlmodel import Session
    from swarm.store import list_open_decisions_for

    with Session(get_engine()) as session:
        return list_open_decisions_for(session, workflow_ids)


def _expire_open_decisions_sync(workflow_id: str) -> None:
    from core.db import get_engine
    from sqlmodel import Session
    from swarm import store

    try:
        with Session(get_engine()) as session:
            rows = store.list_open_decisions(session, workflow_id)
            for row in rows:
                try:
                    store.expire_decision(session, workflow_id, row.node_key)
                except Exception:  # noqa: BLE001 - cancellation is authoritative
                    session.rollback()
                    logger.warning(
                        "failed to expire decision %s for workflow %s",
                        row.id,
                        workflow_id,
                        exc_info=True,
                    )
    except Exception:  # noqa: BLE001 - cancellation is authoritative
        logger.warning(
            "failed to list open decisions for cancelled workflow %s",
            workflow_id,
            exc_info=True,
        )


def _compose_run_view(dbos, workflow_id: str) -> dict:
    from swarm.view import compose_run

    try:
        result = compose_run(
            dbos,
            workflow_id,
            _session_rows(workflow_id),
            _server_app_version(),
            _decision_rows(workflow_id),
        )
    except Exception as exc:  # DBOS uses a runtime-specific missing-workflow error.
        if "not found" in str(exc).lower() or "non-existent" in str(exc).lower():
            raise HTTPException(status_code=404, detail="workflow not found") from exc
        raise
    if result is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return result


def _record_decision_sync(
    workflow_id: str,
    node_key: str,
    decision: str,
    note: str | None,
    actor_subject: str,
    actor_authority: str,
) -> dict:
    from core.db import get_engine
    from sqlmodel import Session
    from swarm import store

    with Session(get_engine()) as session:
        idempotent = store.get_open_decision(session, workflow_id, node_key) is None
        row = store.record_decision(
            session,
            workflow_id,
            node_key,
            decision,
            note,
            actor_subject,
            actor_authority,
        )
        return store.decision_response(row, idempotent)


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
    model: str,
    budget_usd: float | None,
    triggered_by: str | None,
) -> None:
    """Create task record, managing its own session."""
    from core.db import get_engine
    from sqlmodel import Session
    from swarm import models

    with Session(get_engine()) as db:
        models.create_task(
            task_id,
            task,
            repo,
            branch,
            shared.inference.META_SPARK_MODEL,
            budget_usd,
            start_model=model,
            start_triggered_by=triggered_by,
            session=db,
        )


def _record_classification_sync(
    task_id: str,
    task: str,
    classification: str,
    latency_ms: int,
    outcome: str,
    refusal_code: str | None,
    budget_usd: float | None,
    record_plan: bool = True,
) -> None:
    """Record classification and plan, managing its own session."""
    from core.db import get_engine
    from sqlmodel import Session
    from swarm import models

    with Session(get_engine()) as db:
        models.record_conductor_call(
            task_id,
            shared.inference.META_SPARK_MODEL,
            "classify_task",
            json.dumps({"task": task}),
            outcome,
            refusal_code=refusal_code,
            version_before=None,
            version_after=None,
            latency_ms=latency_ms,
            session=db,
        )
        if not record_plan:
            return
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
                shared.inference.META_SPARK_MODEL,
                "[]",
                budget_usd if budget_usd is not None else 0,
                False,
                None,
                None,
                1,
                session=db,
            )


def _claim_resolution_sync(task_id: str, state: str) -> str | None:
    """Claim the only side-effecting resolution for a classified task."""
    from core.db import get_engine
    from sqlalchemy import update
    from sqlmodel import Session
    from swarm.models import SwarmTask

    claim_token = str(uuid4())
    with Session(get_engine()) as db:
        result = db.execute(
            update(SwarmTask)
            .where(SwarmTask.id == task_id, SwarmTask.start_state == "classifying")
            .values(
                start_state=state,
                start_claim_token=claim_token,
                start_updated_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        return claim_token if result.rowcount == 1 else None


def _finish_task_sync(
    task_id: str,
    expected_state: str,
    state: str,
    *,
    claim_token: str | None = None,
    session_id: int | None = None,
    workflow_id: str | None = None,
    payload: dict | None = None,
) -> bool:
    """Persist a terminal start result if this worker still owns the task."""
    from core.db import get_engine
    from sqlalchemy import update
    from sqlmodel import Session
    from swarm.models import SwarmTask

    values = {
        "start_state": state,
        "start_payload_json": json.dumps(payload) if payload else None,
        "start_claim_token": None,
        "start_updated_at": datetime.now(timezone.utc),
        "settled_at": datetime.now(timezone.utc),
    }
    if session_id is not None:
        values["session_id"] = session_id
    if workflow_id is not None:
        values["workflow_id"] = workflow_id
    conditions = [
        SwarmTask.id == task_id,
        SwarmTask.start_state == expected_state,
    ]
    if claim_token is not None:
        conditions.append(SwarmTask.start_claim_token == claim_token)
    with Session(get_engine()) as db:
        result = db.execute(update(SwarmTask).where(*conditions).values(**values))
        db.commit()
        return result.rowcount == 1


def _record_retryable_start_error_sync(
    task_id: str,
    expected_state: str,
    claim_token: str | None,
    message: str,
) -> bool:
    """Record a retryable start error without making the task terminal."""
    from core.db import get_engine
    from sqlalchemy import update
    from sqlmodel import Session
    from swarm.models import SwarmTask

    conditions = [
        SwarmTask.id == task_id,
        SwarmTask.start_state == expected_state,
    ]
    if claim_token is not None:
        conditions.append(SwarmTask.start_claim_token == claim_token)
    with Session(get_engine()) as db:
        result = db.execute(
            update(SwarmTask)
            .where(*conditions)
            .values(start_payload_json=json.dumps({"message": message}))
        )
        db.commit()
        return result.rowcount == 1


def _task_start_snapshot_sync(task_id: str) -> dict | None:
    """Load persisted launcher state using a fresh session."""
    from core.db import get_engine
    from sqlmodel import Session, select
    from swarm.models import SwarmConductorCall, SwarmPlanVersion, SwarmTask

    with Session(get_engine()) as db:
        row = db.get(SwarmTask, task_id)
        if row is None:
            return None
        classification_call = db.exec(
            select(SwarmConductorCall)
            .where(
                SwarmConductorCall.task_id == task_id,
                SwarmConductorCall.tool == "classify_task",
            )
            .order_by(SwarmConductorCall.id.desc())
        ).first()
        classification_version = db.exec(
            select(SwarmPlanVersion)
            .where(
                SwarmPlanVersion.task_id == task_id,
                SwarmPlanVersion.cause_kind == "classification",
            )
            .order_by(SwarmPlanVersion.version.desc())
        ).first()
        classification = None
        if classification_version is not None:
            try:
                classification = json.loads(classification_version.change_json).get(
                    "classification"
                )
            except (AttributeError, TypeError, ValueError):
                logger.warning(
                    "invalid stored classification for task %s", task_id, exc_info=True
                )
        has_classification = classification_call is not None
        classification_outcome = (
            classification_call.outcome if classification_call is not None else None
        )
        classification_refusal_code = (
            classification_call.refusal_code
            if classification_call is not None
            else None
        )
        classification_latency_ms = (
            classification_call.latency_ms
            if classification_call is not None
            and classification_call.latency_ms is not None
            else 0
        )
        return {
            "task_id": row.id,
            "task": row.task_text,
            "repo": row.repo,
            "branch": row.base_branch,
            "model": row.start_model,
            "budget_usd": row.budget_usd,
            "triggered_by": row.start_triggered_by,
            "state": row.start_state,
            "payload": json.loads(row.start_payload_json)
            if row.start_payload_json
            else {},
            "session_id": row.session_id,
            "workflow_id": row.workflow_id,
            "updated_at": row.start_updated_at,
            "has_classification": has_classification,
            "classification": classification,
            "classification_outcome": classification_outcome,
            "classification_refusal_code": classification_refusal_code,
            "classification_latency_ms": classification_latency_ms,
        }


def _start_context_from_snapshot(snapshot: dict) -> _StartContext:
    has_classification = bool(snapshot.get("has_classification"))
    return _StartContext(
        task_id=snapshot["task_id"],
        task=snapshot["task"],
        repo=snapshot["repo"],
        branch=snapshot["branch"],
        model=snapshot["model"],
        budget_usd=snapshot["budget_usd"],
        triggered_by=snapshot["triggered_by"],
        record_plan=not has_classification,
        has_classification=has_classification,
        classification=snapshot.get("classification"),
        classification_outcome=snapshot.get("classification_outcome"),
        classification_refusal_code=snapshot.get("classification_refusal_code"),
        classification_latency_ms=snapshot.get("classification_latency_ms") or 0,
    )


def _reclaim_stuck_task_sync(task_id: str, cutoff: datetime) -> dict | None:
    """Atomically renew a stale task lease and return its persisted inputs."""
    from core.db import get_engine
    from sqlalchemy import update
    from sqlmodel import Session
    from swarm.models import SwarmTask

    now = datetime.now(timezone.utc)
    with Session(get_engine()) as db:
        result = db.execute(
            update(SwarmTask)
            .where(
                SwarmTask.id == task_id,
                SwarmTask.start_state.in_(_IN_PROGRESS_STATES),
                SwarmTask.start_updated_at <= cutoff,
            )
            .values(
                start_state="classifying",
                start_claim_token=None,
                start_updated_at=now,
            )
        )
        db.commit()
        if result.rowcount != 1:
            return None
    return _task_start_snapshot_sync(task_id)


def _update_task_inputs_sync(
    task_id: str,
    task: str,
    repo: str | None,
    branch: str | None,
    model: str,
    triggered_by: str | None,
) -> None:
    """Fill in the repository inputs on a task being resubmitted.

    `task_id` arrives from the client, so this narrows what a resubmission
    may touch rather than trusting the id. Only a task that is still awaiting
    inputs or ended in an error, with nothing started against it, qualifies.
    It may already have a repo when only the branch was missing. That keeps the
    fields editable for the retry round trip and makes a stale or wrong id a
    400 instead of a silent write to somebody else's row.
    """
    from core.db import get_engine
    from sqlmodel import Session
    from swarm.models import SwarmTask

    with Session(get_engine()) as db:
        row = db.get(SwarmTask, task_id)
        if row is None:
            raise ValueError(f"Unknown swarm task {task_id}")
        if (
            row.start_state not in {"needs_input", "error"}
            or row.workflow_id
            or row.session_id
        ):
            raise ValueError(f"Swarm task {task_id} is not awaiting inputs")
        row.task_text = task
        row.repo = repo
        row.base_branch = branch
        row.start_model = model
        row.start_triggered_by = triggered_by
        row.start_state = "classifying"
        row.start_payload_json = None
        row.start_claim_token = None
        row.start_updated_at = datetime.now(timezone.utc)
        row.settled_at = None
        db.add(row)
        db.commit()


def _start_status_response(snapshot: dict) -> StartStatusResponse:
    state = snapshot["state"]
    payload = snapshot["payload"]
    if state in _IN_PROGRESS_STATES:
        return StartStatusResponse(kind="classifying")
    return StartStatusResponse(
        kind=state,
        session_id=snapshot["session_id"],
        run_id=snapshot["workflow_id"],
        needs_input=payload.get("needs_input"),
        refusal_code=payload.get("refusal_code"),
        message=payload.get("message"),
        login_required=True if payload.get("login_required") else None,
        verification_url=payload.get("verification_url"),
        user_code=payload.get("user_code"),
        grant=payload.get("grant"),
        login_message=payload.get("login_message"),
    )


async def _classify_and_resolve_body(context: _StartContext, progress: dict) -> None:
    """Classify and start one persisted launcher task."""
    if context.has_classification:
        classification = context.classification or "one_shot"
        latency_ms = context.classification_latency_ms
        outcome = context.classification_outcome or "error_fallback"
        refusal_code = context.classification_refusal_code
        record_classification = False
    else:
        from swarm import classifier

        started = time.monotonic()
        try:
            classification, latency_ms, outcome, refusal_code = await asyncio.wait_for(
                classifier.classify_task_with_outcome(context.task),
                timeout=_CLASSIFIER_DEADLINE_SECONDS,
            )
        except TimeoutError:
            classification = "one_shot"
            latency_ms = round((time.monotonic() - started) * 1000)
            outcome = "timeout"
            refusal_code = "classifier deadline exceeded"
        except Exception as exc:  # noqa: BLE001 - transport failure falls back
            classification = "one_shot"
            latency_ms = round((time.monotonic() - started) * 1000)
            outcome = "error"
            refusal_code = str(exc) or "classifier transport error"
        record_classification = True

    fallback = outcome != "success"
    if fallback:
        classification = "one_shot"
    recorded_outcome = (
        outcome
        if outcome == "success" or outcome.endswith("_fallback")
        else f"{outcome}_fallback"
    )
    if outcome == "success" and refusal_code:
        progress["resolution_state"] = "settling_refused"
    elif classification == "planned" and (not context.repo or not context.branch):
        progress["resolution_state"] = "settling_needs_input"
    elif classification == "planned":
        progress["resolution_state"] = "starting_run"
    else:
        progress["resolution_state"] = "starting_session"

    progress["claim_token"] = await asyncio.to_thread(
        _claim_resolution_sync, context.task_id, progress["resolution_state"]
    )
    if progress["claim_token"] is None:
        return
    if record_classification:
        await asyncio.to_thread(
            _record_classification_sync,
            context.task_id,
            context.task,
            classification,
            latency_ms,
            recorded_outcome,
            refusal_code,
            context.budget_usd,
            context.record_plan,
        )

    if outcome == "success" and refusal_code:
        await asyncio.to_thread(
            _finish_task_sync,
            context.task_id,
            "settling_refused",
            "refused",
            claim_token=progress["claim_token"],
            payload={"refusal_code": refusal_code, "message": refusal_code},
        )
        return

    if classification == "planned":
        if not context.repo or not context.branch:
            await asyncio.to_thread(
                _finish_task_sync,
                context.task_id,
                "settling_needs_input",
                "needs_input",
                claim_token=progress["claim_token"],
                payload={
                    "needs_input": {
                        "repo": not bool(context.repo),
                        "branch": not bool(context.branch),
                    }
                },
            )
            return
        result = await asyncio.to_thread(
            start_run,
            RunRequest(
                task=context.task,
                repo=context.repo,
                branch=context.branch,
                idempotency_key=context.task_id,
                budget_usd=context.budget_usd,
                model=context.model,
            ),
        )
        await asyncio.to_thread(
            _finish_task_sync,
            context.task_id,
            "starting_run",
            "run",
            claim_token=progress["claim_token"],
            workflow_id=result["workflow_id"],
        )
        return

    from agent_sessions.router import StartRequest, start_session_for_task

    result = await start_session_for_task(
        context.triggered_by,
        context.task_id,
        StartRequest(
            prompt=context.task,
            model=context.model,
            repo=context.repo,
            branch=context.branch or "main",
        ),
    )
    if result.get("session_id") is None:
        raise RuntimeError(result.get("error", "session unavailable"))
    payload = {}
    if result.get("login_required"):
        payload = {
            "login_required": True,
            "verification_url": result.get("verification_url"),
            "user_code": result.get("user_code"),
            "grant": result.get("grant"),
            "login_message": result.get("message"),
        }
    await asyncio.to_thread(
        _finish_task_sync,
        context.task_id,
        "starting_session",
        "session",
        claim_token=progress["claim_token"],
        session_id=result["session_id"],
        payload=payload,
    )


def _is_retryable_dbos_replica_error(exc: HTTPException) -> bool:
    return (
        exc.status_code == 503
        and isinstance(exc.detail, str)
        and "DBOS is not launched on this replica" in exc.detail
    )


async def _classify_and_resolve(context: _StartContext) -> None:
    """Resolve a persisted launcher task within one bounded background task."""
    progress = {"resolution_state": "classifying", "claim_token": None}
    try:
        await asyncio.wait_for(
            _classify_and_resolve_body(context, progress),
            timeout=_CLASSIFY_RESOLUTION_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        logger.warning(
            "async classify-and-start cancelled for task %s", context.task_id
        )
        raise
    except TimeoutError:
        logger.error("async classify-and-start timed out for task %s", context.task_id)
        await asyncio.to_thread(
            _finish_task_sync,
            context.task_id,
            progress["resolution_state"],
            "error",
            claim_token=progress["claim_token"],
            payload={"message": "task start timed out"},
        )
    except HTTPException as exc:
        if _is_retryable_dbos_replica_error(exc):
            logger.warning(
                "async classify-and-start reached a DBOS follower for task %s",
                context.task_id,
            )
            await asyncio.to_thread(
                _record_retryable_start_error_sync,
                context.task_id,
                progress["resolution_state"],
                progress["claim_token"],
                str(exc.detail),
            )
            return
        logger.exception("async classify-and-start failed for task %s", context.task_id)
        await asyncio.to_thread(
            _finish_task_sync,
            context.task_id,
            progress["resolution_state"],
            "error",
            claim_token=progress["claim_token"],
            payload={"message": str(exc.detail) or "task could not be started"},
        )
    except Exception as exc:  # noqa: BLE001 - background failures become status
        logger.exception("async classify-and-start failed for task %s", context.task_id)
        await asyncio.to_thread(
            _finish_task_sync,
            context.task_id,
            progress["resolution_state"],
            "error",
            claim_token=progress["claim_token"],
            payload={"message": str(exc) or "task could not be started"},
        )


def _schedule_classification(context: _StartContext) -> None:
    current = _CLASSIFICATION_TASKS.get(context.task_id)
    if current is not None and not current.done():
        return
    task = asyncio.create_task(
        _classify_and_resolve(context),
        name=f"classify-and-start:{context.task_id}",
    )
    _CLASSIFICATION_TASKS[context.task_id] = task

    def forget(completed: asyncio.Task[None]) -> None:
        if _CLASSIFICATION_TASKS.get(context.task_id) is completed:
            _CLASSIFICATION_TASKS.pop(context.task_id, None)

    task.add_done_callback(forget)
    task.add_done_callback(log_task_exception)


@router.post("/classify-and-start", response_model=ClassifyAndStartAccepted)
async def classify_and_start(request: Request, body: ClassifyAndStartRequest):
    task = body.task.strip()
    model = body.model.strip()
    if not task or not model:
        raise HTTPException(status_code=400, detail="task and model are required")
    if body.budget_usd is not None and body.budget_usd <= 0:
        raise HTTPException(status_code=400, detail="budget_usd must be positive")
    if body.repo is not None and body.repo not in REPO_CATALOG:
        raise HTTPException(status_code=400, detail=f"unknown repo {body.repo}")

    from agent_sessions import model_family, normalize_model

    try:
        model = normalize_model(model)
        model_family(model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from swarm import models

    is_resubmission = body.task_id is not None
    task_id = body.task_id or models.mint_task_id()
    triggered_by = request.headers.get("x-auth-email")
    triggered_by = triggered_by.strip().lower() or None if triggered_by else None
    if is_resubmission:
        try:
            await asyncio.to_thread(
                _update_task_inputs_sync,
                task_id,
                task,
                body.repo,
                body.branch,
                model,
                triggered_by,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        snapshot = await asyncio.to_thread(_task_start_snapshot_sync, task_id)
        assert snapshot is not None
        context = _start_context_from_snapshot(snapshot)
    else:
        await asyncio.to_thread(
            _create_task_sync,
            task_id,
            task,
            body.repo,
            body.branch,
            model,
            body.budget_usd,
            triggered_by,
        )
        context = _StartContext(
            task_id=task_id,
            task=task,
            repo=body.repo,
            branch=body.branch,
            model=model,
            budget_usd=body.budget_usd,
            triggered_by=triggered_by,
        )
    _schedule_classification(context)
    return ClassifyAndStartAccepted(task_id=task_id, kind="classifying")


@router.get(
    "/tasks/{task_id}/start-status",
    response_model=StartStatusResponse,
    response_model_exclude_none=True,
)
async def task_start_status(task_id: str) -> StartStatusResponse:
    snapshot = await asyncio.to_thread(_task_start_snapshot_sync, task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Swarm task not found")

    state = snapshot["state"]
    task = _CLASSIFICATION_TASKS.get(task_id)
    has_live_task = task is not None and not task.done()
    updated_at = snapshot["updated_at"]
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    soft_cutoff = now - timedelta(seconds=_CLASSIFY_STUCK_SECONDS)
    hard_cutoff = now - timedelta(seconds=_CLASSIFY_HARD_STUCK_SECONDS)
    hard_stuck = state in _IN_PROGRESS_STATES and updated_at <= hard_cutoff
    if hard_stuck and has_live_task:
        task.cancel()
        _CLASSIFICATION_TASKS.pop(task_id, None)
        has_live_task = False
    if (
        state in _IN_PROGRESS_STATES
        and updated_at <= soft_cutoff
        and (not has_live_task or hard_stuck)
    ):
        reclaim_cutoff = hard_cutoff if hard_stuck else soft_cutoff
        reclaimed = await asyncio.to_thread(
            _reclaim_stuck_task_sync, task_id, reclaim_cutoff
        )
        if reclaimed is not None:
            if not reclaimed["model"]:
                await asyncio.to_thread(
                    _finish_task_sync,
                    task_id,
                    "classifying",
                    "error",
                    payload={"message": "task is missing its requested model"},
                )
            else:
                _schedule_classification(_start_context_from_snapshot(reclaimed))
            snapshot = await asyncio.to_thread(_task_start_snapshot_sync, task_id)
            assert snapshot is not None
    return _start_status_response(snapshot)


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
        shared.inference.META_SPARK_MODEL,
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
    return _compose_run_view(_dbos_read(), workflow_id)


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
        _dbos_read(),
        active,
        session_costs,
        _server_app_version(),
        limit=limit,
        decision_loader=_decision_rows_for,
    )


@router.post("/runs/{workflow_id}/nodes/{node_key}/decision")
async def decide_run(
    workflow_id: str, node_key: str, body: DecisionRequest, request: Request
) -> dict:
    dbos = _dbos()
    run = await asyncio.to_thread(_compose_run_view, dbos, workflow_id)
    if run.get("dbos_status") not in ("PENDING", "ENQUEUED"):
        raise HTTPException(
            status_code=409, detail="workflow is not awaiting a decision"
        )
    header_actor = request.headers.get("Cf-Access-Authenticated-User-Email")
    actor_subject = header_actor or "operator"
    actor_authority = "cloudflare-access" if header_actor else "anonymous"
    from swarm.store import InvalidDecision, NoOpenDecision

    try:
        result = await asyncio.to_thread(
            _record_decision_sync,
            workflow_id,
            node_key,
            body.decision,
            body.note,
            actor_subject,
            actor_authority,
        )
    except NoOpenDecision as exc:
        raise HTTPException(
            status_code=409, detail="no open decision for this node"
        ) from exc
    except InvalidDecision as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        # MERGE, never replace: a blind write here destroyed the pinned plan
        # that view.py reads, so approving a gate unpinned the run (#5417).
        await merge_workflow_attributes(
            dbos,
            workflow_id,
            {
                "decided_by": {
                    "actor": result["actor_subject"],
                    "at": result["decided_at"],
                }
            },
        )
    except Exception:  # noqa: BLE001 - the decision must not fail on metadata
        logger.warning(
            "failed to record decision actor for workflow %s",
            workflow_id,
            exc_info=True,
        )
    return result


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
    await asyncio.to_thread(_expire_open_decisions_sync, workflow_id)
    # Cancel first so no new turn is scheduled while guest sessions are reaped.
    from agent_sessions.api import reap_sessions_for_workflow

    guest_sessions = await reap_sessions_for_workflow(workflow_id)
    actor = request.headers.get("Cf-Access-Authenticated-User-Email") or "operator"
    try:
        await merge_workflow_attributes(
            dbos,
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
