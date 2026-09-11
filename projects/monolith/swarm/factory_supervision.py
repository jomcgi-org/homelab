"""Consume durable exact Ember stop proof through the existing factory owner.

Each tick observes one guest and may issue one conditional stop. No network
call holds a database lock; an immutable audit intent survives observer loss.
Native completion is checked first by the conductor. This path preserves an
unknown turn and settles failed execution only after positive teardown proof.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import math
import os

from sqlmodel import select

from agent_sessions.api import (
    read_uncertain_factory_attempt,
    settle_uncertain_factory_attempt,
)
from swarm import graph
from swarm import factory_controls as controls
from swarm.factory_models import FactoryAudit, FactoryStart
from swarm.models import SwarmNodeRun

ACTOR = "factory:stop-supervision"
MAX_STOP_REQUESTS = 3
HTTP_SECONDS = 5
COMPLETION_ALARM_SECONDS = 120
# How long after a terminal turn the guest stop becomes due. Long enough
# for the conductor's own native completion check to settle the attempt
# first, short enough that a four-hour policy timeout never decides it.
STOP_GRACE_SECONDS = 120
_ACTIONS = (
    "stop_intent",
    "stop_request",
    "stop_accepted",
    "stop_observation",
    "stop_settled",
)
_PRECONDITION_KEYS = frozenset(
    {
        "session_id",
        "generation",
        "invoke_started_at",
        "vm_id",
        "node_id",
        "instance_id",
        "pod_uid",
        "boot_id",
    }
)


def _now():
    return datetime.now(timezone.utc)


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _records(db, pin):
    rows = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id == pin["task_id"],
            FactoryAudit.action.in_(_ACTIONS),
        )
        .order_by(FactoryAudit.id)
    ).all()
    return [
        (row.action, {**detail, "recorded_at": row.created_at.isoformat()})
        for row in rows
        if (detail := json.loads(row.detail_json)).get("workflow_id")
        == pin["workflow_id"]
    ]


def _audit(db, pin, action, **detail):
    controls._audit(
        db,
        ACTOR,
        action,
        task_id=pin["task_id"],
        workflow_id=pin["workflow_id"],
        **detail,
    )


def _stop_deadline(snapshot: dict, identity: dict, pin: dict) -> datetime:
    """When this attempt's guest stop becomes due.

    The turn timeout bounds how long the turn may run, so it is the right
    deadline only while the turn could still be running. Once the session's own
    turn is terminal there is nothing left to wait out, and a planner that left
    the policy maximum in place held one evicted guest for four hours before
    supervision confirmed the cessation it could have confirmed in minutes. A
    terminal turn is therefore due a fixed grace after the failure was
    recorded, whatever the node's timeout says. Every term is a minimum, so
    this can only bring a stop forward: an attempt whose turn is still open
    carries no failure stamp and keeps the turn-timeout deadline alone.
    """
    deadline = min(
        _timestamp(snapshot["deadline_at"]),
        _timestamp(identity["dispatched_at"])
        + timedelta(seconds=pin["turn_timeout_seconds"]),
    )
    failed_turn_at = identity.get("failed_turn_at")
    if failed_turn_at is not None:
        deadline = min(
            deadline,
            _timestamp(failed_turn_at) + timedelta(seconds=STOP_GRACE_SECONDS),
        )
    if "task_deadline_at" in pin:
        deadline = min(deadline, _timestamp(pin["task_deadline_at"]))
    return deadline


def _locked_attempt(db, control, pin, sid, *, require_stop_due=True):
    run = db.exec(
        select(SwarmNodeRun)
        .where(
            SwarmNodeRun.task_id == pin["task_id"],
            SwarmNodeRun.node_key == pin["node_key"],
            SwarmNodeRun.attempt == pin["attempt"],
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if (
        run is None
        or json.loads(run.pin_json or "null") != pin
        or run.dispatch_key != pin["workflow_id"]
        or run.session_id not in (None, sid)
        or run.status not in ("admitted", "dispatched", "uncertain")
        or db.exec(
            select(SwarmNodeRun.id).where(
                SwarmNodeRun.task_id == pin["task_id"],
                SwarmNodeRun.node_key == pin["node_key"],
                SwarmNodeRun.attempt > pin["attempt"],
            )
        ).first()
        is not None
    ):
        raise ValueError("factory_run_changed")
    start = db.exec(
        select(FactoryStart)
        .where(
            FactoryStart.task_id == pin["task_id"],
            FactoryStart.start_key == pin["workflow_id"],
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if (
        start is None
        or start.status not in ("reserved", "uncertain")
        or start.session_id not in (None, sid)
        or start.model != pin["model"]
        or start.max_cost_usd != pin["max_cost_usd"]
    ):
        raise ValueError("factory_start_changed")
    identity = read_uncertain_factory_attempt(db, pin, sid)
    snapshot = controls.task_snapshot(pin["task_id"], session=db)
    deadline = _stop_deadline(snapshot, identity, pin)
    from swarm.factory_attempt_stop import matching_request

    requested = matching_request(db, pin, identity)
    if control.state == "disabled" or (
        require_stop_due
        and requested is None
        and control.state != "stopped"
        and not snapshot["cancellation_requested"]
        and _now() < deadline
    ):
        raise ValueError("factory_stop_not_due")
    return identity, run


def _settlement_accounting(chosen: float | None, original: dict) -> dict:
    """Name the evidence behind the cost this cessation settles at.

    The supervisor charges the highest known figure. That is list-priced only
    when it is exactly the node result's own list price, so a measured cost or
    a retained reservation never inherits the estimate's provenance. The label
    is written with the basis so the pair can never disagree.
    """
    from swarm.node_workflows import ACCOUNTING_LABELS

    if chosen is None:
        basis = "unknown"
    elif original.get("cost_basis") == "list" and original.get("cost_usd") == chosen:
        basis = "list"
    else:
        basis = "provider"
    return {"cost_basis": basis, "accounting": ACCOUNTING_LABELS[basis]}


def _precondition(value, guest_id):
    if not isinstance(value, dict) or set(value) != _PRECONDITION_KEYS:
        raise ValueError("missing_stop_precondition")
    if value["session_id"] != guest_id:
        raise ValueError("wrong_stop_session")
    for field in _PRECONDITION_KEYS - {"generation", "invoke_started_at"}:
        if not isinstance(value[field], str) or not 1 <= len(value[field]) <= 256:
            raise ValueError("invalid_stop_identity")
    if value["instance_id"] != value["node_id"] + "/" + value["pod_uid"]:
        raise ValueError("invalid_stop_instance")
    if type(value["generation"]) is not int or value["generation"] < 0:
        raise ValueError("invalid_stop_generation")
    if value["invoke_started_at"] is not None and (
        type(value["invoke_started_at"]) is not int or value["invoke_started_at"] < 1
    ):
        raise ValueError("invalid_stop_invocation")
    return dict(value)


def _completion(view, expected):
    if not isinstance(view, dict) or view.get("session_id") != expected["session_id"]:
        raise ValueError("wrong_stop_observation")
    stamp = view.get("invoke_started_at")
    if (
        type(stamp) is not type(expected["invoke_started_at"])
        or stamp != expected["invoke_started_at"]
    ):
        raise ValueError("changed_stop_invocation")
    intent, proof = view.get("stop_intent"), view.get("stop_completion")
    if intent is None:
        if proof is not None:
            raise ValueError("completion_without_stop_intent")
        return None
    if not isinstance(intent, dict) or any(
        intent.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("stop_intent_changed")
    _precondition(
        {key: intent.get(key) for key in _PRECONDITION_KEYS}, expected["session_id"]
    )
    if (
        not isinstance(intent.get("operation_id"), str)
        or not 1 <= len(intent["operation_id"]) <= 256
        or type(intent.get("requested_at_unix_ms")) is not int
        or intent["requested_at_unix_ms"] < 1
    ):
        raise ValueError("invalid_stop_intent")
    if proof is None:
        return None
    keys = _PRECONDITION_KEYS | {"operation_id", "requested_at_unix_ms"}
    if (
        view.get("state") != "destroyed"
        or type(view.get("generation")) is not int
        or view["generation"] != expected["generation"]
        or not isinstance(proof, dict)
        or any(proof.get(key) != intent.get(key) for key in keys)
        or type(proof.get("requested_at_unix_ms")) is not int
        or type(proof.get("completed_at_unix_ms")) is not int
        or proof["completed_at_unix_ms"] < 1
    ):
        raise ValueError("invalid_stop_completion")
    _precondition(
        {key: proof.get(key) for key in _PRECONDITION_KEYS}, expected["session_id"]
    )
    # Clock values are attribution, not cross-host ordering evidence.
    return {key: proof[key] for key in keys | {"completed_at_unix_ms"}}


def _control_plane_cessation(view, identity, saved=None):
    """Return terminal CP evidence ordered after this factory dispatch.

    A guest that ceased without ever being sent a stop carries no
    stop_precondition: SessionStopProof.identity/2 returns nil for every
    non-running session, so the control plane can only populate that field for
    a session with a stop intent or still running. Requiring one would hold the
    permit forever in exactly the case this path exists for: spot preemption,
    idle TTL, or a sweeper eviction before the stop deadline. So the identity
    comes from the committed stop intent when one exists, and otherwise from
    the view's own invocation fields, ordered against the recorded attempt the
    way the permit loop orders its own observation.
    """
    if view.get("state") not in {"evicted", "destroyed"}:
        return None
    generation = view.get("generation")
    started = view.get("invoke_started_at")
    last_invoke = view.get("last_invoke_at")
    updated_at = view.get("updated_at")
    # Some legacy destroyed views have no terminal timestamp. They cannot prove
    # ordering, but may still carry the existing exact stop-completion proof.
    if (
        any(
            type(value) is not int or value < 1
            for value in (started, last_invoke, updated_at)
        )
        or type(generation) is not int
        or generation < 0
    ):
        return None
    recorded = view.get("stop_precondition")
    if recorded is None and saved is not None:
        recorded = saved.get("precondition")
    if recorded is not None:
        try:
            expected = _precondition(recorded, identity["guest_id"])
        except ValueError:
            return None
        if (
            generation != expected["generation"]
            or started != expected["invoke_started_at"]
        ):
            return None
    if not started <= last_invoke <= updated_at:
        return None
    # The control plane stamps these in its own milliseconds and they are
    # ordered here against monolith-side timestamps. The gaps asserted are the
    # invoke start to the recorded failure and the failure to the terminal
    # update, both of which are turn-length or eviction-length in production
    # and so far larger than plausible skew between the two clocks. _completion
    # deliberately orders nothing: the exact stop proof is matched by operation
    # identity, which needs no shared clock at all.
    dispatched_at = int(_timestamp(identity["dispatched_at"]).timestamp() * 1000)
    failed_turn_at = int(_timestamp(identity["failed_turn_at"]).timestamp() * 1000)
    # dispatched_at is the pending message's last_dispatch_at, stamped when the
    # executor CLAIMED the turn (agent_sessions/store.py
    # claim_pending_message_for_session_sync), which is before the guest is
    # invoked, so every invoked attempt starts after it. The failed turn is the
    # anchor instead: the invocation that failed had to begin before the
    # failure was recorded, and the terminal update had to land after it.
    if started > failed_turn_at or updated_at <= failed_turn_at:
        return None
    if updated_at <= dispatched_at:
        return None
    return {
        "session_id": identity["guest_id"],
        "state": view["state"],
        "generation": generation,
        "invoke_started_at": started,
        "last_invoke_at": last_invoke,
        "updated_at": updated_at,
    }


def _remember_observation(pin, sid, identity, expected, view):
    """Commit the first observed CP operation before attempting settlement.

    A later settlement rollback cannot forget that exact accepted operation.
    """
    with controls._locked_session() as (db, control):
        current, _run = _locked_attempt(db, control, pin, sid)
        if current != identity:
            raise ValueError("factory_attempt_changed")
        records = _records(db, pin)
        existing = [detail for action, detail in records if action == "stop_intent"]
        if existing and (
            len(existing) != 1
            or existing[0]["identity"] != identity
            or existing[0]["precondition"] != expected
        ):
            raise ValueError("stop_intent_changed")
        if not existing:
            _audit(db, pin, "stop_intent", identity=identity, precondition=expected)
        accepted = [detail for action, detail in records if action == "stop_accepted"]
        cp_intent = view.get("stop_intent")
        canonical = (
            None
            if cp_intent is None
            else {
                key: cp_intent[key]
                for key in _PRECONDITION_KEYS | {"operation_id", "requested_at_unix_ms"}
            }
        )
        if accepted and (len(accepted) != 1 or accepted[0]["intent"] != canonical):
            raise ValueError("accepted_stop_operation_changed")
        if not accepted and canonical is not None:
            _audit(
                db,
                pin,
                "stop_accepted",
                intent=canonical,
                session_id=sid,
                guest_id=identity["guest_id"],
            )


def _http(guest_id, precondition=None):
    from agent_sessions.transport import EmberVmShimTransport

    async def request():
        transport = EmberVmShimTransport()
        operation = (
            transport.get_session(guest_id)
            if precondition is None
            else transport.destroy_session(guest_id, stop_precondition=precondition)
        )
        return await asyncio.wait_for(operation, timeout=HTTP_SECONDS)

    return asyncio.run(request())


def _note(pin, reason):
    # Fixed reason codes, once each per attempt; error text and repeated polling
    # never create an unbounded audit stream or copy request/response bodies.
    with controls._locked_session() as (db, _control):
        previous = _records(db, pin)
        if not any(
            action == "stop_observation" and detail.get("reason") == reason
            for action, detail in previous
        ):
            _audit(
                db,
                pin,
                "stop_observation",
                reason=reason,
                intervention_required=True,
                cessation_confirmed=False,
            )


def reconcile_uncertain_attempt(pin, session_id, original_result, workflow_status):
    """One bounded supervision tick for an already terminal DBOS workflow.

    A live DBOS workflow still owns its deadline/cancellation path. No work is
    started or cancelled here. Three conditional requests maximum are retained
    across observer restart; Ember owns retrying its accepted durable intent.
    """
    if os.environ.get("FACTORY_STOP_SUPERVISION_ENABLED", "false").lower() != "true":
        return False
    if workflow_status not in {
        "SUCCESS",
        "ERROR",
        "CANCELLED",
        "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
    }:
        return False
    if original_result.get("status") != "uncertain" or type(session_id) is not int:
        return False
    cessation_enabled = (
        os.environ.get("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "false").lower()
        == "true"
    )
    try:
        with controls._locked_session() as (db, control):
            if any(action == "stop_settled" for action, _ in _records(db, pin)):
                return True
            identity, _run = _locked_attempt(
                db,
                control,
                pin,
                session_id,
                require_stop_due=True,
            )
            intents = [
                detail
                for action, detail in _records(db, pin)
                if action == "stop_intent"
            ]
            if len(intents) > 1:
                raise ValueError("conflicting_stop_intents")
            saved = intents[0] if intents else None
            if saved is not None and saved["identity"] != identity:
                raise ValueError("factory_attempt_changed")
    except ValueError as exc:
        if str(exc) != "factory_stop_not_due":
            _note(pin, "local_identity_unconfirmed")
        return False

    try:
        view = _http(identity["guest_id"])
    except Exception:
        _note(pin, "stop_observation_unavailable")
        return False
    try:
        if not isinstance(view, dict) or view.get("session_id") != identity["guest_id"]:
            raise ValueError("wrong_stop_observation")
        cessation = None
        if cessation_enabled:
            cessation = _control_plane_cessation(view, identity, saved)
        if cessation is not None:
            expected = None
            proof = cessation
        else:
            if cessation_enabled:
                with controls._locked_session() as (db, control):
                    current, _run = _locked_attempt(
                        db, control, pin, session_id, require_stop_due=True
                    )
                    if current != identity:
                        raise ValueError("factory_attempt_changed")
            if saved is None:
                value = view.get("stop_precondition")
                if value is None and isinstance(view.get("stop_intent"), dict):
                    value = {
                        key: view["stop_intent"].get(key) for key in _PRECONDITION_KEYS
                    }
                expected = _precondition(value, identity["guest_id"])
            else:
                expected = saved["precondition"]
            proof = _completion(view, expected)
            _remember_observation(pin, session_id, identity, expected, view)
        with controls._locked_session() as (db, control):
            current, run = _locked_attempt(
                db,
                control,
                pin,
                session_id,
                require_stop_due=cessation is None,
            )
            if current != identity:
                raise ValueError("factory_attempt_changed")
            records = _records(db, pin)
            existing = [detail for action, detail in records if action == "stop_intent"]
            if cessation is None:
                if existing and (
                    len(existing) != 1
                    or existing[0]["identity"] != identity
                    or existing[0]["precondition"] != expected
                ):
                    raise ValueError("stop_intent_changed")
                if not existing:
                    raise ValueError("missing_committed_stop_intent")
            if proof is not None:
                known_costs = [
                    cost
                    for cost in (
                        run.cost_usd,
                        identity["cost_usd"],
                        original_result.get("cost_usd"),
                    )
                    if cost is not None
                ]
                if any(
                    type(cost) not in (int, float)
                    or not math.isfinite(cost)
                    or cost < 0
                    for cost in known_costs
                ):
                    raise ValueError("invalid_original_cost")
                chosen = max(known_costs) if known_costs else None
                result = {
                    **original_result,
                    "status": "failed",
                    "session_id": session_id,
                    "cost_usd": chosen,
                    **_settlement_accounting(chosen, original_result),
                    "reason": "guest_cessation_confirmed: exact control-plane cessation after factory dispatch",
                    "previous_outcome": json.loads(run.outcome_json or "{}"),
                    "cessation": proof,
                }
                settle_uncertain_factory_attempt(db, pin, identity)
                if run.session_id is None:
                    bound = graph.record_dispatch(
                        pin["task_id"],
                        pin["node_key"],
                        pin["attempt"],
                        session_id,
                        run.base_sha,
                        session=db,
                    )
                    if not bound.ok:
                        raise ValueError("factory_dispatch_refused")
                settled = graph.record_outcome(
                    pin["task_id"],
                    pin["node_key"],
                    pin["attempt"],
                    "failed",
                    result["cost_usd"],
                    run.head_sha,
                    json.dumps(result),
                    session=db,
                )
                if not settled.ok:
                    raise ValueError("factory_outcome_refused")
                charged = controls.record_start_outcome(
                    pin["task_id"],
                    pin["workflow_id"],
                    "failed",
                    ACTOR,
                    cost_usd=result["cost_usd"],
                    session_id=session_id,
                    reconciled=True,
                    session=db,
                )
                if not charged["ok"]:
                    raise ValueError("factory_start_outcome_refused")
                _audit(
                    db,
                    pin,
                    "stop_settled",
                    identity=identity,
                    completion=proof,
                    cessation_confirmed=True,
                    intervention_required=False,
                )
                return True
            requests = sum(action == "stop_request" for action, _ in records)
            if view.get("stop_intent") is not None or requests >= MAX_STOP_REQUESTS:
                dispatch = False
            else:
                _audit(
                    db,
                    pin,
                    "stop_request",
                    request_number=requests + 1,
                    identity_sha256=identity["identity_sha256"],
                    precondition=expected,
                )
                dispatch = True
        if dispatch:
            # The durable local request budget was consumed before the external
            # effect. An observation timeout never creates another identity.
            try:
                _http(identity["guest_id"], expected)
            except Exception:
                _note(pin, "stop_request_unconfirmed")
        elif requests >= MAX_STOP_REQUESTS and view.get("stop_intent") is None:
            _note(pin, "stop_request_bound_reached")
        elif view.get("stop_intent") is not None:
            accepted = [
                detail for action, detail in records if action == "stop_accepted"
            ]
            if (
                accepted
                and (_now() - _timestamp(accepted[0]["recorded_at"])).total_seconds()
                >= COMPLETION_ALARM_SECONDS
            ):
                _note(pin, "node_completion_pending")
    except ValueError as exc:
        if not (cessation_enabled and str(exc) == "factory_stop_not_due"):
            _note(pin, "stop_evidence_or_ownership_changed")
    return False
