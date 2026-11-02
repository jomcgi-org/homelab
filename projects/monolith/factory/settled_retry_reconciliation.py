"""Explicit recovery of a missing first response followed by an uninvoked retry.

The original session, turn, permit and receipt remain immutable. A complete CP
journal proof and durable exclusion of restoring that exact lineage are required
before the factory run/start can fail at unknown cost. An audited intent precedes
the remote fence; a lost response is replayed with the same CP idempotency key.
No inference, guest DELETE, quota mutation or task retry happens here.
"""

import asyncio
import json
import re

from sqlalchemy import text
from sqlmodel import select

from factory.execution import admission, reconciliation
from factory.execution.models import (
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
)
from factory.execution.review_leases import digest
from factory.orchestration import factory_controls as controls
from factory.orchestration import graph
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryReceipt,
    FactoryStart,
)
from factory.orchestration.models import SwarmNodeRun

INTENT = "settled_retry_recovery_intent"
APPLIED = "settled_retry_recovery_applied"
CP_REASON = "factory_unknown_dispatch_recovery"
MAX_AGE_SECONDS = 30


def _request(actor, receipt_id, session_id, permit_id, reason, request_key):
    if any(type(v) is not int or v < 1 for v in (receipt_id, session_id, permit_id)):
        raise ValueError("settled_retry_invalid_identity")
    return {
        "actor": controls._text(actor, "actor"),
        "receipt_id": receipt_id,
        "session_id": session_id,
        "permit_id": permit_id,
        "reason": controls._text(reason, "reason"),
        "request_key": controls._text(request_key, "request_key"),
    }


def _event(db, request, action):
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.action == action,
            FactoryAudit.actor == request["actor"],
        )
    ).all():
        entry = json.loads(raw)
        if entry["request"]["request_key"] == request["request_key"]:
            if entry["request"] != request:
                raise ValueError("settled_retry_request_conflict")
            return entry
    return None


def _snapshot(db, control, request):
    """Called under control, pool and session locks, before and after remote I/O."""
    admission.lock_pool(db)
    receipt = db.get(FactoryReceipt, request["receipt_id"], populate_existing=True)
    if receipt is None or receipt.state != "uncertain" or not receipt.task_id:
        raise ValueError("settled_retry_receipt_changed")
    if db.get(AgentSession, request["session_id"]) is None:
        raise ValueError("settled_retry_session_missing")
    agent = reconciliation._locked_session(db, request["session_id"])
    if not agent.node_key or type(agent.node_attempt) is not int:
        raise ValueError("settled_retry_owner_changed")
    runs = db.exec(
        select(SwarmNodeRun)
        .where(
            SwarmNodeRun.task_id == receipt.task_id,
            SwarmNodeRun.node_key == agent.node_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if not runs or any(row.attempt > agent.node_attempt for row in runs):
        raise ValueError("settled_retry_newer_attempt")
    run = next((row for row in runs if row.attempt == agent.node_attempt), None)
    if (
        run is None
        or run.status != "uncertain"
        or run.session_id != agent.id
        or run.cost_usd is not None
    ):
        raise ValueError("settled_retry_run_changed")
    pin = json.loads(run.pin_json or "null")
    if (
        not isinstance(pin, dict)
        or run.dispatch_key != pin.get("workflow_id")
        or pin.get("task_id") != receipt.task_id
        or pin.get("node_key") != run.node_key
        or pin.get("attempt") != run.attempt
    ):
        raise ValueError("settled_retry_pin_changed")
    start = db.exec(
        select(FactoryStart)
        .where(
            FactoryStart.task_id == receipt.task_id,
            FactoryStart.start_key == run.dispatch_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if (
        start is None
        or start.status != "uncertain"
        or start.session_id != agent.id
        or start.cost_usd is not None
        or start.accounting_basis is not None
        or start.model != pin["model"]
        or start.max_cost_usd != pin["max_cost_usd"]
    ):
        raise ValueError("settled_retry_start_changed")
    local = reconciliation.read_unknown_retry_factory_attempt(db, pin, agent.id)
    if local is None or local["permit_id"] != request["permit_id"]:
        raise ValueError("settled_retry_identity_unconfirmed")
    table = (
        "workflow_status"
        if db.bind.dialect.name == "sqlite"
        else "dbos.workflow_status"
    )
    suffix = " FOR UPDATE" if db.bind.dialect.name != "sqlite" else ""
    workflow = (
        db.execute(
            text(
                f"SELECT workflow_uuid,status FROM {table} WHERE workflow_uuid=:id{suffix}"
            ),
            {"id": run.dispatch_key},
        )
        .mappings()
        .first()
    )
    if workflow is None or workflow["status"] not in {"SUCCESS", "ERROR", "CANCELLED"}:
        raise ValueError("settled_retry_workflow_not_terminal")
    # Only hashes leave the lock for private fields. The immutable records are
    # re-read rather than replaced or decorated with invented result evidence.
    native = {
        "session": agent.model_dump(),
        "turn": db.get(AgentTurn, local["turn_id"]).model_dump(),
        "permit": db.get(AgentCapacityReservation, local["permit_id"]).model_dump(),
        "result_receipt": db.get(
            AgentResultReceipt, local["unknown_dispatch"]["receipt_id"]
        ).model_dump(),
    }
    return {
        "version": control.version,
        "policy_sha256": digest(control.policy_json),
        "receipt_id": receipt.id,
        "task_id": receipt.task_id,
        "node_key": run.node_key,
        "attempt": run.attempt,
        "workflow_id": run.dispatch_key,
        "session_id": agent.id,
        "local": local,
        "native_sha256": digest(native),
        "factory_sha256": digest(
            {
                "receipt": receipt.model_dump(),
                "run": run.model_dump(),
                "start": start.model_dump(),
                "workflow": dict(workflow),
            }
        ),
    }


def _same_local(current, saved):
    # Once intent is admitted, unrelated global policy changes do not revoke the
    # exact physical fence operation. No policy is written here; the receipt and
    # its holds are still part of the CAS, and current policy governs any retry.
    return {
        k: v for k, v in current.items() if k not in {"version", "policy_sha256"}
    } == {k: v for k, v in saved.items() if k not in {"version", "policy_sha256"}}


def _prepare(request, expected_sha=None):
    with controls._locked_session() as (db, control):
        for action in (APPLIED,):
            done = _event(db, request, action)
            if done:
                if expected_sha != done["proposal"]["preview_sha256"]:
                    raise ValueError("settled_retry_preview_changed")
                return None, done["result"], None
        pending = _event(db, request, INTENT)
        local = _snapshot(db, control, request)
        if pending:
            proposal = pending["proposal"]
            if expected_sha != proposal["preview_sha256"] or not _same_local(
                local, proposal["local"]
            ):
                raise ValueError("settled_retry_pending_identity_changed")
            return local, None, proposal
        return local, None, None


def _cp_identity(local, view):
    first = local["local"]["unknown_dispatch"]
    if (
        not isinstance(view, dict)
        or view.get("session_id") != first["guest_id"]
        or view.get("lineage_id") != first["guest_id"]
        or view.get("workload") != "claude-runtime"
        or type(view.get("generation")) is not int
        or view["generation"] != 0
        or view.get("state") != "evicted"
        or view.get("terminal_reason") != "node_gone"
        or type(view.get("turn_seq")) is not int
        or view["turn_seq"] != 1
        or view.get("inflight_dispatch_id") != first["dispatch_id"]
        or any(
            type(view.get(k)) is not int or view[k] < 1
            for k in ("invoke_started_at", "last_invoke_at", "updated_at")
        )
        or not view["invoke_started_at"] <= view["last_invoke_at"] <= view["updated_at"]
        or not isinstance(view.get("principal"), str)
        or not view["principal"]
    ):
        raise ValueError("settled_retry_guest_identity_changed")
    # Dispatch identity, not clocks across hosts, binds the original invocation.
    return {k: view[k] for k in ("generation", "invoke_started_at", "updated_at")}


def _cp_proof(local, payload, view, *, fenced):
    view = view if isinstance(view, dict) else {}
    stamp = view.get("observed_at_unix_ms")
    fence = view.get("recovery_fence")
    fence = fence if isinstance(fence, dict) else {}
    first = local["local"]["unknown_dispatch"]
    if (
        type(stamp) is not int
        or not 0
        <= int(controls._now().timestamp() * 1000) - stamp
        <= MAX_AGE_SECONDS * 1000
        or type(view.get("schema")) is not int
        or view["schema"] != 1
        or view.get("session_id") != first["guest_id"]
        or view.get("lineage_id") != first["guest_id"]
        or view.get("workload") != "claude-runtime"
        or view.get("state") != "evicted"
        or type(view.get("turn_seq")) is not int
        or view["turn_seq"] != 1
        or type(view.get("generation")) is not int
        or view.get("dispatch_id") != first["dispatch_id"]
        or any(
            view.get(k) != payload[k]
            for k in ("generation", "invoke_started_at", "updated_at")
        )
        or view.get("fenced") is not fenced
        or fence.get("restore_excluded") is not fenced
        or type(fence.get("schema")) is not int
        or fence["schema"] != 1
        or fence.get("source") != "retained_control_plane_journal"
        or fence.get("authority_scope") != "single_writer_dispatch"
        or fence.get("lineage_id") != first["guest_id"]
        or fence.get("terminal_reason") != "node_gone"
        or fence.get("reason") != CP_REASON
        or fence.get("request_key") != payload["request_key"]
        or any(
            fence.get(k) is not True
            for k in ("complete", "no_successor", "no_pending_restore")
        )
        or not isinstance(view.get("principal"), str)
        or not view["principal"]
        or type(view.get("last_invoke_at")) is not int
        or not payload["invoke_started_at"]
        <= view["last_invoke_at"]
        <= payload["updated_at"]
        or any(
            not isinstance(view.get(k), str)
            or re.fullmatch(r"[0-9a-f]{64}", view[k]) is None
            for k in ("preview_sha256", "evidence_sha256")
        )
        or any(
            not isinstance(fence.get(k), str)
            or re.fullmatch(r"[A-Za-z0-9_./:-]{1,512}", fence[k]) is None
            for k in ("node_id", "vm_id")
        )
    ):
        raise ValueError("settled_retry_cessation_invalid")
    return {
        **{
            k: view[k]
            for k in (
                "preview_sha256",
                "evidence_sha256",
                "session_id",
                "lineage_id",
                "principal",
                "last_invoke_at",
                "generation",
                "invoke_started_at",
                "updated_at",
                "dispatch_id",
            )
        },
        "node_id": fence["node_id"],
        "vm_id": fence["vm_id"],
    }


def _proposal(request, local, payload, proof):
    result = {
        "request": request,
        "local": local,
        "cp_payload": payload,
        "cp_proof": proof,
        "effects": {
            "retire_exact_lineage_restore_authority": True,
            "factory_attempt_outcome": "failed",
            "cost": "reserved_unknown_cost",
            "historical_result_preserved": True,
            "native_accounting_preserved": True,
            "dispatch": False,
            "other_holds_preserved": True,
            "continuation": "existing_factory_policy_after_reconciliation",
        },
    }
    return {**result, "preview_sha256": digest(result)}


def _admit_call(db, request, proposal, expected_sha, delegation):
    from factory.orchestration.recovery_delegation import (
        conflicting_retry_intent,
        guard,
    )

    if conflicting_retry_intent(db, proposal["local"]["task_id"], request):
        raise ValueError("settled_retry_other_owner_intent")
    guard(db, request["actor"], "settled_retry", request, expected_sha, delegation)


def _admit_replay(request, proposal, expected_sha, delegation):
    with controls._locked_session() as (db, control):
        done = _event(db, request, APPLIED)
        if done:
            return done["result"]
        intent = _event(db, request, INTENT)
        if (
            intent is None
            or intent["proposal"] != proposal
            or not _same_local(_snapshot(db, control, request), proposal["local"])
        ):
            raise ValueError("settled_retry_pending_identity_changed")
        _admit_call(db, request, proposal, expected_sha, delegation)


def _record_intent(request, proposal, expected_sha, delegation=None):
    with controls._locked_session() as (db, control):
        done = _event(db, request, APPLIED)
        if done:
            if done["proposal"]["preview_sha256"] != expected_sha:
                raise ValueError("settled_retry_preview_changed")
            return done["result"]
        if (
            _snapshot(db, control, request) != proposal["local"]
            or proposal["preview_sha256"] != expected_sha
        ):
            raise ValueError("settled_retry_preview_changed")
        previous = _event(db, request, INTENT)
        if previous:
            if previous["proposal"] != proposal:
                raise ValueError("settled_retry_intent_conflict")
            _admit_call(db, request, proposal, expected_sha, delegation)
            return
        _admit_call(db, request, proposal, expected_sha, delegation)
        controls._audit(
            db,
            request["actor"],
            INTENT,
            task_id=proposal["local"]["task_id"],
            request=request,
            proposal=proposal,
        )


def _finish(request, proposal, remote):
    with controls._locked_session() as (db, control):
        done = _event(db, request, APPLIED)
        if done:
            return done["result"]
        intent = _event(db, request, INTENT)
        if intent is None or intent["proposal"] != proposal:
            raise ValueError("settled_retry_intent_changed")
        if not _same_local(_snapshot(db, control, request), proposal["local"]):
            raise ValueError("settled_retry_identity_changed_after_fence")
        proof = _cp_proof(
            proposal["local"], proposal["cp_payload"], remote, fenced=True
        )
        if proof != proposal["cp_proof"]:
            raise ValueError("settled_retry_fence_changed")
        local = proposal["local"]
        run = db.exec(
            select(SwarmNodeRun).where(
                SwarmNodeRun.task_id == local["task_id"],
                SwarmNodeRun.node_key == local["node_key"],
                SwarmNodeRun.attempt == local["attempt"],
            )
        ).one()
        outcome = {
            "status": "failed",
            "cost_usd": None,
            "cost_basis": "unknown",
            "invocation_phase": "unknown_dispatch_ceased_then_not_invoked",
            "capacity_denied": False,
            "session_id": local["session_id"],
            "reason": "exact earlier dispatch ceased and its lineage cannot restore; retry was not invoked",
            "previous_outcome": json.loads(run.outcome_json or "{}"),
            "settled_retry_cessation": proof,
        }
        settled = graph.record_outcome(
            local["task_id"],
            local["node_key"],
            local["attempt"],
            "failed",
            None,
            run.head_sha,
            json.dumps(outcome),
            session=db,
        )
        if not settled.ok:
            raise ValueError("settled_retry_graph_refused")
        charged = controls.record_start_outcome(
            local["task_id"],
            local["workflow_id"],
            "failed",
            request["actor"],
            cost_usd=None,
            session_id=local["session_id"],
            reconciled=True,
            session=db,
        )
        if not charged["ok"]:
            raise ValueError("settled_retry_start_refused")
        control.version += 1
        control.actor, control.updated_at = request["actor"], controls._now()
        result = {
            "ok": True,
            "state": "applied",
            "version": control.version,
            "receipt_id": request["receipt_id"],
            "session_id": request["session_id"],
            "permit_id": request["permit_id"],
            "request_key": request["request_key"],
            "preview_sha256": proposal["preview_sha256"],
            "effects": proposal["effects"],
        }
        controls._audit(
            db,
            request["actor"],
            APPLIED,
            task_id=local["task_id"],
            request=request,
            proposal=proposal,
            authority_observed_at_unix_ms=remote["observed_at_unix_ms"],
            result=result,
        )
        return result


async def run(
    operation,
    actor,
    receipt_id,
    session_id,
    permit_id,
    reason,
    request_key,
    expected_preview_sha256=None,
    delegation=None,
):
    from factory.execution.transport import EmberVmShimTransport, EmberVMTransportError

    request = _request(actor, receipt_id, session_id, permit_id, reason, request_key)
    if operation not in ("preview", "apply") or (
        operation == "apply"
        and not re.fullmatch(r"[0-9a-f]{64}", expected_preview_sha256 or "")
    ):
        raise ValueError("settled_retry_preview_required")
    local, replay, proposal = await asyncio.to_thread(
        _prepare, request, expected_preview_sha256
    )
    if replay:
        return replay
    admitted_here = False
    transport = EmberVmShimTransport()
    guest = local["local"]["unknown_dispatch"]["guest_id"]
    if proposal is None:
        view = await asyncio.wait_for(transport.get_session(guest), timeout=5)
        payload = {
            **_cp_identity(local, view),
            "dispatch_id": local["local"]["unknown_dispatch"]["dispatch_id"],
            "reason": CP_REASON,
            "request_key": "factory-retry-" + digest(request),
        }
        remote = await transport.session_recovery_fence(
            guest, {**payload, "operation": "preview"}
        )
        if operation == "apply":
            # Another identical caller may finish (or commit its intent) while
            # this CP preview is in flight. A real CP preview then reports the
            # existing fence. Reconcile our durable owner before interpreting it.
            _, replay, pending = await asyncio.to_thread(
                _prepare, request, expected_preview_sha256
            )
            if replay is not None:
                return replay
            if pending is not None:
                proposal = pending
        if proposal is None:
            proof = _cp_proof(local, payload, remote, fenced=False)
            if any(proof[k] != view[k] for k in ("principal", "last_invoke_at")):
                raise ValueError("settled_retry_guest_identity_changed")
            proposal = _proposal(request, local, payload, proof)
            if operation == "preview":
                # Recheck the local shape after the remote read even for previews.
                current, _, _ = await asyncio.to_thread(_prepare, request)
                if current != local:
                    raise ValueError("settled_retry_identity_changed")
                return {"ok": True, "state": "preview", **proposal}
            replay = await asyncio.to_thread(
                _record_intent, request, proposal, expected_preview_sha256, delegation
            )
            if replay is not None:
                return replay
            admitted_here = True
    elif operation != "apply":
        raise ValueError("settled_retry_pending_apply_requires_same_request")
    if not admitted_here:
        replay = await asyncio.to_thread(
            _admit_replay, request, proposal, expected_preview_sha256, delegation
        )
        if replay is not None:
            return replay
    try:
        remote = await transport.session_recovery_fence(
            guest,
            {
                **proposal["cp_payload"],
                "operation": "apply",
                "expected_preview_sha256": proposal["cp_proof"]["preview_sha256"],
            },
        )
    except (EmberVMTransportError, TimeoutError, ValueError):
        # The fence may have committed. Do not infer failure or start another
        # request: the persisted intent pins replay to the original exact CAS.
        return {
            "ok": False,
            "state": "outcome_unknown",
            "phase": "lineage_fence",
            "request_key": request_key,
            "factory_settlement": "not_applied",
            "reason": "replay_identical_apply_to_reconcile",
        }
    try:
        return await asyncio.to_thread(_finish, request, proposal, remote)
    except ValueError as exc:
        # A fence response can precede proof validation or a local CAS refusal.
        # Preserve both histories; receipt of a response alone is not success.
        return {
            "ok": False,
            "state": "blocked",
            "phase": "lineage_fence_response_received",
            "request_key": request_key,
            "factory_settlement": "not_applied",
            "reason": str(exc),
        }
