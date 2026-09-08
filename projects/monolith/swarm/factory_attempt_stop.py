"""Operator-requested stop of one immutable factory attempt, then normal continuation.

The existing audit ledger owns idempotency and the native UNKNOWN writer owns
execution fencing. No cancellation, lease or cleanup acknowledgement proves
cessation. The existing factory supervisor consumes exact durable Ember proof.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re

from sqlmodel import Session, select

from agent_sessions import admission
from agent_sessions.constants import INTERRUPTED_TERMINAL_REASONS, UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityReservation,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from agent_sessions.reconciliation import _factory_owner, _locked_session
from swarm import factory_controls as controls
from swarm.factory_models import FactoryAudit, FactoryStart
from swarm.models import SwarmNodeRun

REQUEST_ACTION = "attempt_stop_requested"
CANCEL_ACTION = "attempt_stop_cancel"
MAX_CANCEL_REQUESTS = 2


def enabled() -> bool:
    return os.environ.get("FACTORY_STOP_SUPERVISION_ENABLED", "false").lower() == "true"


def _stamp(value) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("missing_factory_dispatch_identity")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _digest(value) -> str:
    return hashlib.sha256(controls._json(value).encode()).hexdigest()


def _requests(db: Session, task_id: str) -> list[dict]:
    return [
        {**json.loads(row.detail_json), "actor": row.actor}
        for row in db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == task_id, FactoryAudit.action == REQUEST_ACTION
            )
            .order_by(FactoryAudit.id)
        ).all()
    ]


def _candidate(db, task_id, node_key, attempt, session_id):
    """Caller holds factory control, then this function locks pool and session."""
    run = db.exec(
        select(SwarmNodeRun)
        .where(
            SwarmNodeRun.task_id == task_id,
            SwarmNodeRun.node_key == node_key,
            SwarmNodeRun.attempt == attempt,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if run is None or run.status not in {"admitted", "dispatched", "uncertain"}:
        raise ValueError("factory_attempt_not_active")
    pin = json.loads(run.pin_json or "null")
    if (
        not isinstance(pin, dict)
        or pin.get("task_id") != task_id
        or pin.get("node_key") != node_key
        or pin.get("attempt") != attempt
        or run.dispatch_key != pin.get("workflow_id")
        or run.session_id not in (None, session_id)
        or db.exec(
            select(SwarmNodeRun.id).where(
                SwarmNodeRun.task_id == task_id,
                SwarmNodeRun.node_key == node_key,
                SwarmNodeRun.attempt > attempt,
            )
        ).first()
        is not None
    ):
        raise ValueError("factory_run_changed")
    receipt = controls._receipt(db, task_id)
    if receipt is None or receipt.state not in controls._ACTIVE:
        raise ValueError("factory_task_not_active")
    start = db.exec(
        select(FactoryStart)
        .where(
            FactoryStart.task_id == task_id,
            FactoryStart.start_key == pin["workflow_id"],
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if (
        start is None
        or start.status not in {"reserved", "uncertain"}
        or start.session_id not in (None, session_id)
        or start.model != pin["model"]
        or start.max_cost_usd != pin["max_cost_usd"]
    ):
        raise ValueError("factory_start_changed")
    admission.lock_pool(db)
    agent = _factory_owner(db, pin, session_id)
    if agent is None:
        raise ValueError("missing_factory_owner")
    agent = _locked_session(db, agent.id)
    if not agent.ember_session_id or agent.result_receipt_fence_id is not None:
        raise ValueError("pending_or_missing_factory_guest")
    owners = db.exec(
        select(AgentSession.id).where(
            AgentSession.ember_session_id == agent.ember_session_id
        )
    ).all()
    permits = db.exec(
        select(AgentCapacityReservation).where(
            AgentCapacityReservation.session_id == agent.id
        )
    ).all()
    pending = db.exec(
        select(PendingMessage).where(PendingMessage.session_id == agent.id)
    ).all()
    turns = db.exec(select(AgentTurn).where(AgentTurn.session_id == agent.id)).all()
    if owners != [agent.id] or len(permits) != 1 or len(pending) > 1 or len(turns) > 1:
        raise ValueError("ambiguous_factory_attempt")
    permit = permits[0]
    if (
        permit.pending_seq != 1
        or permit.local_session_id != agent.local_session_id
        or permit.tier != "project"
        or permit.routine_job_name is not None
        or not permit.owner
        or permit.state not in {"running", "uncertain"}
    ):
        raise ValueError("factory_attempt_not_uncertain_or_running")
    if pending:
        head = pending[0]
        if (
            head.seq != 1
            or head.claimed_by_replica != permit.owner
            or permit.state != "running"
            or (turns and turns[0].terminal_reason not in INTERRUPTED_TERMINAL_REASONS)
        ):
            raise ValueError("factory_pending_owner_changed")
        count, dispatched = head.dispatch_count, head.last_dispatch_at
    else:
        if (
            len(turns) != 1
            or turns[0].seq != 1
            or turns[0].terminal_reason != "error"
            or permit.state != "uncertain"
            or not (
                (
                    agent.status == "failed"
                    and turns[0].stop_reason == UNKNOWN_INVOCATION
                )
                or (
                    agent.status == "warn"
                    and turns[0].stop_reason is None
                    and permit.outcome == "delivery_error"
                )
            )
        ):
            raise ValueError("factory_attempt_not_uncertain")
        recovery = json.loads(turns[0].usage_json or "{}").get("recovery", {})
        if recovery.get("claim_owner") != permit.owner:
            raise ValueError("factory_pending_owner_changed")
        count, dispatched = (
            recovery.get("dispatch_count"),
            recovery.get("last_dispatch_at"),
        )
    if type(count) is not int or count < 1:
        raise ValueError("missing_factory_dispatch_identity")
    identity = {
        "task_id": task_id,
        "node_key": node_key,
        "attempt": attempt,
        "run_id": run.id,
        "start_id": start.id,
        "session_id": agent.id,
        "permit_id": permit.id,
        "guest_id": agent.ember_session_id,
        "workflow_id": pin["workflow_id"],
        "seq": 1,
        "dispatch_count": count,
        "claim_owner": permit.owner,
        "dispatched_at": _stamp(dispatched),
        "pin_sha256": _digest(pin),
    }
    identity["identity_sha256"] = _digest(identity)
    return identity, pin, bool(pending)


def read_attempt_stop(
    task_id: str, node_key: str, attempt: int, session_id: int
) -> dict:
    with controls._locked_session() as (db, _control):
        identity, _pin, _pending = _candidate(
            db, task_id, node_key, attempt, session_id
        )
        return identity


def _same_request(records, request):
    same = [row for row in records if row["request_key"] == request["request_key"]]
    if same:
        if len(same) != 1 or same[0]["request"] != request:
            raise ValueError("conflicting_attempt_stop_request")
        return same[0]["result"]
    return None


def request_attempt_stop(
    *,
    task_id: str,
    node_key: str,
    attempt: int,
    session_id: int,
    request_key: str,
    expected_identity_sha256: str,
    reason: str,
    actor: str,
) -> dict:
    """Persist authority and UNKNOWN atomically; no external cancellation here."""
    if not enabled():
        raise ValueError("stop_supervision_disabled")
    request = {
        "task_id": controls._text(task_id, "task_id"),
        "node_key": controls._text(node_key, "node_key"),
        "attempt": controls._integer(attempt, "attempt", 1, 1000),
        "session_id": controls._integer(session_id, "session_id", 1, 2**63 - 1),
        "request_key": controls._text(request_key, "request_key"),
        "expected_identity_sha256": expected_identity_sha256,
        "reason": controls._text(reason, "reason", 1000),
        "actor": controls._text(actor, "actor"),
    }
    if not isinstance(expected_identity_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_identity_sha256
    ):
        raise ValueError("invalid_attempt_identity_digest")
    with controls._locked_session() as (db, _control):
        replay = _same_request(_requests(db, task_id), request)
        if replay is not None:
            return replay
        identity, pin, _pending = _candidate(db, task_id, node_key, attempt, session_id)
        if identity["identity_sha256"] != expected_identity_sha256:
            raise ValueError("factory_attempt_changed")
    from swarm import factory_supervision as supervisor

    view = supervisor._http(identity["guest_id"])
    if not isinstance(view, dict) or view.get("session_id") != identity["guest_id"]:
        raise ValueError("wrong_stop_observation")
    value = view.get("stop_precondition")
    if value is None and isinstance(view.get("stop_intent"), dict):
        value = {
            key: view["stop_intent"].get(key) for key in supervisor._PRECONDITION_KEYS
        }
    precondition = supervisor._precondition(value, identity["guest_id"])
    if precondition["invoke_started_at"] is None:
        raise ValueError("factory_invocation_not_observed")
    supervisor._completion(view, precondition)
    with controls._locked_session() as (db, _control):
        records = _requests(db, task_id)
        replay = _same_request(records, request)
        if replay is not None:
            return replay
        current, current_pin, pending = _candidate(
            db, task_id, node_key, attempt, session_id
        )
        if current != identity or current_pin != pin:
            raise ValueError("factory_attempt_changed")
        if any(row["identity"]["workflow_id"] == pin["workflow_id"] for row in records):
            raise ValueError("attempt_stop_already_requested")
        if any(action == "stop_intent" for action, _ in supervisor._records(db, pin)):
            raise ValueError("attempt_stop_already_requested")
        if pending:
            from agent_sessions.store import finish_unknown_pending_in_session

            if not finish_unknown_pending_in_session(
                db,
                session_id,
                1,
                identity["claim_owner"],
                identity["dispatch_count"],
                "factory_attempt_stop",
                expected_guest_id=identity["guest_id"],
                expected_workflow_id=identity["workflow_id"],
            ):
                raise ValueError("factory_attempt_changed")
        from agent_sessions.api import read_uncertain_factory_attempt

        terminal = read_uncertain_factory_attempt(db, pin, session_id)
        result = {
            "ok": True,
            "state": "requested",
            "request_key": request_key,
            "task_id": task_id,
            "node_key": node_key,
            "attempt": attempt,
            "session_id": session_id,
        }
        controls._audit(
            db,
            actor,
            REQUEST_ACTION,
            task_id=task_id,
            request_key=request_key,
            workflow_id=pin["workflow_id"],
            request=request,
            identity=identity,
            pin=pin,
            precondition=precondition,
            result=result,
        )
        supervisor._audit(
            db, pin, "stop_intent", identity=terminal, precondition=precondition
        )
        return result


def matching_request(db: Session, pin: dict, identity: dict) -> dict | None:
    records = [
        row
        for row in _requests(db, pin["task_id"])
        if row["identity"]["workflow_id"] == pin["workflow_id"]
    ]
    if not records:
        return None
    if len(records) != 1 or records[0]["pin"] != pin:
        raise ValueError("conflicting_attempt_stop_request")
    saved = records[0]
    for key in (
        "session_id",
        "guest_id",
        "permit_id",
        "seq",
        "dispatch_count",
        "claim_owner",
        "dispatched_at",
    ):
        if saved["identity"][key] != identity[key]:
            raise ValueError("factory_attempt_changed")
    return saved


def executor_stop_requested(
    session_id: int, seq: int, claim_owner: str, dispatch_count: int
) -> bool:
    """Read a committed exact request; never cancel another replica's attempt."""
    from core.db import get_engine

    with Session(get_engine()) as db:
        agent = db.get(AgentSession, session_id)
        if agent is None or not agent.local_session_id.startswith("factory:"):
            return False
        fields = agent.local_session_id.split(":")
        if len(fields) != 4:
            return False
        records = [
            row
            for row in _requests(db, fields[1])
            if row["identity"]["session_id"] == session_id
            and row["identity"]["seq"] == seq
            and row["identity"]["claim_owner"] == claim_owner
            and row["identity"]["dispatch_count"] == dispatch_count
        ]
        if len(records) != 1:
            return False
        saved = records[0]["identity"]
        # The request itself and the original UNKNOWN writer committed together.
        turn = db.exec(
            select(AgentTurn).where(
                AgentTurn.session_id == session_id,
                AgentTurn.seq == seq,
            )
        ).one_or_none()
        if turn is None or turn.stop_reason != UNKNOWN_INVOCATION:
            return False
        recovery = json.loads(turn.usage_json or "{}").get("recovery", {})
        binding_matches = agent.ember_session_id == saved["guest_id"]
        if agent.ember_session_id is None:
            # The exact proof owner may finish before this heartbeat sees the
            # request. Its settlement retires the binding, not the old observer.
            from swarm.factory_supervision import _records

            binding_matches = any(
                action == "stop_settled"
                and detail.get("cessation_confirmed") is True
                and all(
                    detail.get("identity", {}).get(key) == saved[key]
                    for key in (
                        "session_id",
                        "guest_id",
                        "permit_id",
                        "seq",
                        "claim_owner",
                        "dispatch_count",
                    )
                )
                for action, detail in _records(db, records[0]["pin"])
            )
        return (
            recovery.get("claim_owner") == claim_owner
            and recovery.get("dispatch_count") == saved["dispatch_count"]
            and agent.workflow_id == saved["workflow_id"]
            and binding_matches
        )


def process_attempt_stop(
    pin: dict, session_id: int | None, dbos
) -> tuple[bool, int | None]:
    """Return (waiting, exact session); terminal proof uses the supervisor.

    The original workflow may never have returned its session ID. The durable
    request supplies that same identity without recreating or replaying work.
    """
    if not enabled():
        return False, None
    with controls._locked_session() as (db, _control):
        records = [
            row
            for row in _requests(db, pin["task_id"])
            if row["identity"]["workflow_id"] == pin["workflow_id"]
        ]
        if not records:
            return False, None
        saved = records[0]
        identity, current_pin, _pending = _candidate(
            db,
            pin["task_id"],
            pin["node_key"],
            pin["attempt"],
            saved["identity"]["session_id"],
        )
        if (
            len(records) != 1
            or current_pin != pin
            or identity != saved["identity"]
            or session_id not in (None, identity["session_id"])
        ):
            raise ValueError("factory_attempt_changed")
    state = dbos.get_workflow_status(pin["workflow_id"])
    if state is not None and state.status not in {"PENDING", "ENQUEUED"}:
        return False, identity["session_id"]
    if state is None:
        from swarm.factory_supervision import _note

        _note(pin, "stop_workflow_unavailable")
        return True, identity["session_id"]
    with controls._locked_session() as (db, _control):
        current, _pin, _pending = _candidate(
            db,
            pin["task_id"],
            pin["node_key"],
            pin["attempt"],
            identity["session_id"],
        )
        if current != identity:
            raise ValueError("factory_attempt_changed")
        cancellations = [
            row
            for row in db.exec(
                select(FactoryAudit).where(
                    FactoryAudit.task_id == pin["task_id"],
                    FactoryAudit.action == CANCEL_ACTION,
                )
            ).all()
            if json.loads(row.detail_json).get("workflow_id") == pin["workflow_id"]
        ]
        if len(cancellations) >= MAX_CANCEL_REQUESTS:
            from swarm.factory_supervision import _audit, _records

            if not any(
                action == "stop_observation"
                and detail.get("reason") == "stop_cancel_bound_reached"
                for action, detail in _records(db, pin)
            ):
                _audit(
                    db,
                    pin,
                    "stop_observation",
                    reason="stop_cancel_bound_reached",
                    intervention_required=True,
                    cessation_confirmed=False,
                )
            return True, identity["session_id"]
        controls._audit(
            db,
            controls._text(saved["actor"], "actor"),
            CANCEL_ACTION,
            task_id=pin["task_id"],
            workflow_id=pin["workflow_id"],
            request_key=saved["request_key"],
            session_id=identity["session_id"],
            request_number=len(cancellations) + 1,
        )
    # No generic cancellation route: that route also invokes legacy guest reap.
    dbos.cancel_workflow(pin["workflow_id"], cancel_children=False)
    return True, identity["session_id"]
