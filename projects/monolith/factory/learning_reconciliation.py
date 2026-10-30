"""Acknowledge one ceased learning attempt for cadence changes only.

This never changes the unknown turn, settled permit, failed session or job.
It does not make the old session sendable. Unlike the undelivered quota probe,
this attempt DID reach a guest and its output remains unknown. Positive fresh
control-plane cessation and an explicit operator decision are both required.
"""

import asyncio
import json

from core.clock import aware
from shared.invocation_outcomes import terminal_dispatch_cessation
from sqlalchemy import or_, text
from sqlmodel import select

from factory.execution import admission
from factory.execution import permit_supervision as supervision
from factory.execution.constants import DRAINER_NODE_KEY, UNKNOWN_INVOCATION
from factory.execution.models import (
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
    PendingMessage,
    ProbeObservation,
)
from factory.execution.review_leases import digest
from factory.orchestration import factory_controls as controls
from factory.orchestration import refill_control
from factory.orchestration.factory_models import FactoryAudit

ACTION = "learning_ceased_unknown_acknowledged"
EFFECT = "exclude_exact_attempt_from_hourly_cadence_guard_only"
MAX_PROOF_AGE_SECONDS = 30


def proof(db, session_id, seq, permit_id, *, lock=False):
    """Exact immutable history plus current local non-execution evidence."""
    if (
        any(type(v) is not int or v < 1 for v in (session_id, seq, permit_id))
        or seq != 1
    ):
        raise ValueError("learning_invalid_identity")

    def row(model, predicate):
        query = select(model).where(predicate)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return db.exec(query).first()

    agent = row(AgentSession, AgentSession.id == session_id)
    permit = row(AgentCapacityReservation, AgentCapacityReservation.id == permit_id)
    observed = row(ProbeObservation, ProbeObservation.permit_id == permit_id)
    if (
        agent is None
        or permit is None
        or observed is None
        or agent.status != "failed"
        or agent.admission_tier != "project"
        or not agent.workflow_id
        or agent.node_key != DRAINER_NODE_KEY
        or agent.local_session_id
        != f"{agent.workflow_id}:{DRAINER_NODE_KEY}:{refill_control.JOB}"
        or permit.session_id != session_id
        or permit.local_session_id != agent.local_session_id
        or permit.pending_seq != seq
        or permit.tier != "project"
        or permit.routine_job_name != refill_control.JOB
        or permit.state != "settled"
        or permit.outcome != "guest_cessation_confirmed"
        or permit.settled_at is None
        or observed.settled_at is None
        or observed.reason != "guest_cessation_confirmed"
        or not observed.identity_sha256
        or not observed.guest_id
        or type(observed.generation) is not int
        or observed.generation < 0
        or type(observed.invoke_started_at) is not int
        or observed.invoke_started_at < 1
        or type(observed.cp_updated_at) is not int
        or agent.ember_session_id is not None
        or agent.ember_session_token is not None
        or agent.result_receipt_fence_id is not None
        or admission.cleanup_pending(db, agent)
        or admission.guest_has_cleanup_claim(db, observed.guest_id)
        or supervision._factory_owned(db, agent)
    ):
        raise ValueError("learning_not_settled_ceased")
    # A retired worker workflow must not be able to enqueue another attempt.
    table = (
        "workflow_status"
        if db.bind.dialect.name == "sqlite"
        else "dbos.workflow_status"
    )
    suffix = " FOR UPDATE" if lock and db.bind.dialect.name != "sqlite" else ""
    workflow = (
        db.execute(
            text(
                f"SELECT workflow_uuid,status FROM {table} WHERE workflow_uuid=:id{suffix}"
            ),
            {"id": agent.workflow_id},
        )
        .mappings()
        .first()
    )
    if workflow is None or workflow["status"] not in {"SUCCESS", "ERROR", "CANCELLED"}:
        raise ValueError("learning_workflow_not_terminal")
    turns = db.exec(
        select(AgentTurn).where(AgentTurn.session_id == session_id).limit(2)
    ).all()
    permits = db.exec(
        select(AgentCapacityReservation)
        .where(
            or_(
                AgentCapacityReservation.session_id == session_id,
                AgentCapacityReservation.local_session_id == agent.local_session_id,
            )
        )
        .limit(2)
    ).all()
    if len(turns) != 1 or len(permits) != 1 or permits[0].id != permit_id:
        raise ValueError("learning_ambiguous_attempt")
    turn = turns[0]
    if (
        turn.seq != seq
        or turn.terminal_reason != "error"
        or turn.stop_reason != UNKNOWN_INVOCATION
    ):
        raise ValueError("learning_turn_changed")
    if (
        db.exec(
            select(PendingMessage.id).where(PendingMessage.session_id == session_id)
        ).first()
        is not None
        or db.exec(
            select(AgentResultReceipt.id).where(
                or_(
                    AgentResultReceipt.session_id == session_id,
                    AgentResultReceipt.local_session_id == agent.local_session_id,
                    AgentResultReceipt.guest_id == observed.guest_id,
                )
            )
        ).first()
        is not None
        or db.exec(
            select(AgentSession.id).where(
                AgentSession.ember_session_id == observed.guest_id
            )
        ).first()
        is not None
    ):
        raise ValueError("learning_delivery_or_pending_work")
    try:
        recovery = json.loads(turn.usage_json or "{}")["recovery"]
        dispatch = {
            key: recovery[key]
            for key in ("claim_owner", "dispatch_count", "last_dispatch_at")
        }
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("learning_dispatch_evidence_invalid") from exc
    return {
        "session_id": session_id,
        "seq": seq,
        "permit_id": permit_id,
        "guest_id": observed.guest_id,
        "generation": observed.generation,
        "invoke_started_at": observed.invoke_started_at,
        "cp_updated_at": observed.cp_updated_at,
        "failed_at": aware(turn.created_at).isoformat(),
        "dispatch": dispatch,
        "owner": permit.owner,
        "identity_sha256": digest(
            {
                "session": agent.model_dump(),
                "turn": turn.model_dump(),
                "permit": permit.model_dump(),
                "observation": observed.model_dump(),
                "workflow": dict(workflow),
            }
        ),
    }


def resolutions(db, candidates, *, lock=False):
    """Only this cadence guard consumes the acknowledgement, never send/retry."""
    resolved = set()
    if not candidates:
        return resolved
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(FactoryAudit.action == ACTION)
    ).all():
        try:
            proposal = json.loads(raw)["proposal"]
            saved = proposal["proof"]
            if proposal["effect"] != EFFECT or saved["session_id"] not in candidates:
                continue
            if (
                proof(
                    db, saved["session_id"], saved["seq"], saved["permit_id"], lock=lock
                )
                == saved
            ):
                resolved.add(saved["session_id"])
        except (ValueError, TypeError, KeyError):
            continue
    return resolved


def _proposal(db, control, session_id, seq, permit_id, reason):
    job = refill_control.learning_job(db)
    return {
        "ok": True,
        "version": control.version,
        "policy_sha256": digest(json.loads(control.policy_json)),
        "job_sha256": digest(job),
        "job_name": refill_control.JOB,
        "reason": controls._text(reason, "reason"),
        "proof": proof(db, session_id, seq, permit_id, lock=True),
        "effect": EFFECT,
        "unknown_outcome_preserved": True,
        "dispatched": False,
        "cadence_changed": False,
    }


def _replay(db, actor, request):
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.action == ACTION,
            FactoryAudit.actor == actor,
        )
    ).all():
        old = json.loads(raw)
        if old["request"]["request_key"] == request["request_key"]:
            if old["request"] != request:
                raise ValueError("learning_request_conflict")
            return old["result"]
    return None


def _prepare(session_id, seq, permit_id, reason, actor=None, request=None):
    with controls._locked_session() as (db, control):
        admission.lock_pool(db)
        if request is not None:
            replay = _replay(db, actor, request)
            if replay is not None:
                return None, replay
        return _proposal(db, control, session_id, seq, permit_id, reason), None


def _cessation(saved, observed, observed_at):
    from datetime import datetime

    if (
        not 0
        <= (controls._now() - observed_at).total_seconds()
        <= MAX_PROOF_AGE_SECONDS
    ):
        raise ValueError("learning_stale_cessation")
    if not isinstance(observed, dict) or not observed:
        raise ValueError("learning_cessation_unavailable")
    if (
        observed.get("session_id") != saved["guest_id"]
        or type(observed.get("generation")) is not int
        or observed["generation"] != saved["generation"]
        or observed.get("invoke_started_at") != saved["invoke_started_at"]
        or type(observed.get("updated_at")) is not int
        or not saved["cp_updated_at"]
        <= observed["updated_at"]
        <= int(observed_at.timestamp() * 1000)
        or not terminal_dispatch_cessation(
            observed,
            saved["dispatch"],
            saved["owner"],
            datetime.fromisoformat(saved["failed_at"]),
        )
    ):
        raise ValueError("learning_cessation_mismatch")
    return {
        key: observed.get(key)
        for key in (
            "session_id",
            "generation",
            "invoke_started_at",
            "updated_at",
            "last_invoke_at",
            "state",
        )
    }


def _finish(candidate, observed, observed_at, actor=None, request=None):
    with controls._locked_session() as (db, control):
        admission.lock_pool(db)
        if request is not None:
            replay = _replay(db, actor, request)
            if replay is not None:
                return replay
        saved = candidate["proof"]
        current = _proposal(
            db,
            control,
            saved["session_id"],
            saved["seq"],
            saved["permit_id"],
            candidate["reason"],
        )
        if current != candidate:
            raise ValueError("learning_identity_changed")
        current["cessation"] = _cessation(saved, observed, observed_at)
        current["preview_sha256"] = digest(current)
        if request is None:
            return {**current, "observed_at": observed_at.isoformat()}
        if current["preview_sha256"] != request["expected_preview_sha256"]:
            raise ValueError("learning_preview_changed")
        if saved["session_id"] in resolutions(db, {saved["session_id"]}):
            raise ValueError("learning_already_acknowledged")
        control.version += 1
        control.actor, control.updated_at = actor, controls._now()
        result = {
            "ok": True,
            "version": control.version,
            "session_id": saved["session_id"],
            "seq": saved["seq"],
            "permit_id": saved["permit_id"],
            "effect": EFFECT,
            "unknown_outcome_preserved": True,
            "dispatched": False,
            "cadence_changed": False,
        }
        controls._audit(
            db,
            actor,
            ACTION,
            request=request,
            proposal=current,
            observed_at=observed_at.isoformat(),
            result=result,
        )
        return result


async def _run(session_id, seq, permit_id, reason, actor=None, request=None):
    from factory.execution.transport import EmberVmShimTransport

    candidate, replay = await asyncio.to_thread(
        _prepare, session_id, seq, permit_id, reason, actor, request
    )
    if replay is not None:
        return replay
    try:
        observed = await asyncio.wait_for(
            EmberVmShimTransport().get_session(candidate["proof"]["guest_id"]),
            timeout=5,
        )
    except Exception as exc:
        # Gone, unavailable, timeouts and permission errors are never positive
        # cessation evidence. No destroy, retry or alternate endpoint is used.
        raise ValueError("learning_cessation_unavailable") from exc
    return await asyncio.to_thread(
        _finish, candidate, observed, controls._now(), actor, request
    )


async def preview(session_id, seq, permit_id, reason):
    return await _run(session_id, seq, permit_id, reason)


async def apply(
    actor, session_id, seq, permit_id, reason, request_key, expected_preview_sha256
):
    actor = controls._text(actor, "actor")
    request = {
        "session_id": session_id,
        "seq": seq,
        "permit_id": permit_id,
        "reason": controls._text(reason, "reason"),
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    return await _run(session_id, seq, permit_id, reason, actor, request)
