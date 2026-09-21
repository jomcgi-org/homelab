"""Consume durable exact Ember stop proof through the existing factory owner.

Each tick observes one guest and may issue one conditional stop or one destroy
for a guest stranded on a departed node. No network call holds a database lock;
an immutable audit intent survives observer loss. Native completion is checked
first by the conductor. This path preserves an unknown turn and settles failed
execution only after positive teardown proof.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import math
import os

from sqlmodel import select

from factory.execution.api import (
    read_drained_lost_factory_attempt,
    read_uncertain_factory_attempt,
    settle_drained_lost_factory_attempt,
    settle_uncertain_factory_attempt,
)
from factory.orchestration import graph
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit, FactoryStart
from factory.orchestration.models import SwarmNodeRun

ACTOR = "factory:stop-supervision"
# Preserve the existing request budget unless the staged transient behavior is
# enabled. Under the staged behavior, a conditional DELETE whose response is
# lost has an unknown external outcome, so later ticks only observe.
MAX_STOP_REQUESTS = 3
TRANSIENT_MAX_STOP_REQUESTS = 1
MAX_NODE_GONE_DESTROY_REQUESTS = 2
HTTP_SECONDS = 5
COMPLETION_ALARM_SECONDS = 120
TRANSIENT_RETRY_INTERVAL_SECONDS = 300
TRANSIENT_RETRY_WINDOW_SECONDS = 900
# How long after the attempt's failed turn the guest stop becomes due. Long
# enough for the conductor's own native completion check to settle the attempt
# first, short enough that a four-hour policy timeout never decides it.
STOP_GRACE_SECONDS = 120
NODE_GONE_GRACE_SECONDS = 600
# A guest the control plane answers 404/410 for is authoritatively absent: it
# deletes the session record only after teardown, so absence orders the guest
# after its own process the way a stop completion does. One reading is still
# not enough to release the reservation. A control-plane restore, a replica
# mid-rollout serving a stale table, or a route briefly answering for the wrong
# shard can each 404 a guest that is still running, and releasing then would
# let a second writer start against a live process. So absence settles nothing
# until it has held across this many separate observations spanning this long.
MIN_ABSENCE_OBSERVATIONS = 3
ABSENCE_CONFIRM_SECONDS = 900
# One observation per this interval, so the run actually samples the window.
# Recording on every tick instead would take the whole count in one 45 second
# burst and then wait out the rest of the span blind, which proves only that
# the guest was absent for 45 seconds.
ABSENCE_OBSERVATION_INTERVAL_SECONDS = 300
# Two intervals. A longer gap than this means the run was broken by something
# that was not absence: a live guest answering in between, or an observer that
# was not running. Evidence then starts again rather than accumulating across
# unrelated episodes, which is what makes an intermittent 404 during a rollout
# unable to add up to a release over hours.
ABSENCE_MAX_GAP_SECONDS = 600
# How long one absence run may grow before it is treated as an anomaly. A run
# normally settles at four observations, so passing this means settlement is
# being refused for some other reason, which is noted once. It bounds a run,
# not an attempt: a run broken by presence starts again.
MAX_ABSENCE_OBSERVATIONS = 8
BOUND_ZERO_TURN_REQUEST_INTERVAL_SECONDS = 300
MAX_BOUND_ZERO_TURN_DESTROY_REQUESTS = 2
_ACTIONS = (
    "bound_zero_turn_observation",
    "bound_zero_turn_reset",
    "bound_zero_turn_fence",
    "bound_zero_turn_request",
    "stop_intent",
    "stop_request",
    "stop_accepted",
    "stop_observation",
    "stop_absence",
    "stop_presence",
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

    The turn timeout bounds how long the turn may run, so it was the right
    deadline only for a turn that could still be running. Nothing supervision
    sees is such a turn: read_uncertain_factory_attempt refuses the attempt
    with factory_attempt_not_uncertain unless its turn is already terminal
    with terminal_reason "error", so a live turn is out of scope upstream and
    failed_turn_at is always present. Waiting out the turn timeout therefore
    waited for a turn that had already ended, and a planner that left the
    policy maximum in place held one evicted guest for four hours before
    supervision confirmed a cessation it could have confirmed in minutes.

    The failure stamp decides it instead, a fixed grace after the failure was
    recorded, whatever the node's timeout says. Every term is a minimum, so
    this can only bring a stop forward. The failed_turn_at guard is a total
    function's floor rather than a live branch: the identity reader has
    already guaranteed the key, and the arithmetic stays defined without it.
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


def _locked_attempt(
    db,
    control,
    pin,
    sid,
    *,
    require_stop_due=True,
    identity_reader=None,
):
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
    reader = identity_reader or read_uncertain_factory_attempt
    identity = reader(db, pin, sid)
    snapshot = controls.task_snapshot(pin["task_id"], session=db)
    deadline = _stop_deadline(snapshot, identity, pin)
    from factory.orchestration.factory_attempt_stop import matching_request

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
    from factory.orchestration.node_workflows import ACCOUNTING_LABELS

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
        any(type(value) is not int or value < 1 for value in (started, updated_at))
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
    if last_invoke is None or (type(last_invoke) is int and 0 < last_invoke < started):
        # Eviction can cause the failed turn itself, before the client records
        # its error. A missing or older completion stamp is expected when the response
        # was lost; match the exact durable dispatch instead of requiring it.
        from shared.invocation_outcomes import terminal_dispatch_cessation

        if view.get("session_id") != identity[
            "guest_id"
        ] or not terminal_dispatch_cessation(
            view,
            {
                "claim_owner": identity.get("claim_owner"),
                "dispatch_count": identity.get("dispatch_count"),
                "last_dispatch_at": identity["dispatched_at"],
            },
            identity.get("claim_owner"),
            _timestamp(identity["failed_turn_at"]),
        ):
            return None
        return {
            "session_id": identity["guest_id"],
            "state": view["state"],
            "generation": generation,
            "invoke_started_at": started,
            "last_invoke_at": last_invoke,
            "updated_at": updated_at,
            "cessation_evidence": "terminal_dispatch",
        }
    if type(last_invoke) is not int or not started <= last_invoke <= updated_at:
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


def _brick_restart_cessation(view, identity, saved=None):
    """Prove that this attempt's guest vanished with its exact brick.

    EmberVM writes ``failed`` plus terminal reason ``brick_gone`` only after its
    node registry has aged out or unregistered the owning brick instance. That
    durable transition is stronger than an absent session read: it names a
    guest that existed, an invocation that started for this dispatch, and the
    physical node whose departed instance owned it.

    The brick transition can precede the monolith's transport failure stamp, so
    unlike an ordinary later eviction it need not have ``updated_at`` after
    ``failed_turn_at``. The invoke itself still has to start after dispatch and
    no later than the recorded failure. A saved exact stop identity, when one
    exists, must also match generation, invoke stamp, and node. Missing,
    malformed, stale, or foreign evidence proves nothing.
    """
    if (
        view.get("state") != "failed"
        or view.get("terminal_reason") != "brick_gone"
        or view.get("session_id") != identity["guest_id"]
    ):
        return None
    generation = view.get("generation")
    started = view.get("invoke_started_at")
    last_invoke = view.get("last_invoke_at")
    updated_at = view.get("updated_at")
    node = view.get("node")
    node_id = node.get("node_id") if isinstance(node, dict) else None
    if (
        type(generation) is not int
        or generation < 0
        or type(started) is not int
        or started < 1
        or type(updated_at) is not int
        or updated_at < started
        or not isinstance(node_id, str)
        or not node_id
        or (
            last_invoke is not None
            and (type(last_invoke) is not int or last_invoke < 1)
        )
        or (type(last_invoke) is int and last_invoke >= started)
    ):
        return None
    dispatched_at = int(_timestamp(identity["dispatched_at"]).timestamp() * 1000)
    failed_turn_at = int(_timestamp(identity["failed_turn_at"]).timestamp() * 1000)
    if (
        started <= dispatched_at
        or started > failed_turn_at
        or updated_at <= dispatched_at
    ):
        return None
    if saved is not None:
        try:
            expected = _precondition(saved.get("precondition"), identity["guest_id"])
        except ValueError:
            return None
        if (
            generation != expected["generation"]
            or started != expected["invoke_started_at"]
            or node_id != expected["node_id"]
        ):
            return None
    return {
        "session_id": identity["guest_id"],
        "state": "failed",
        "terminal_reason": "brick_gone",
        "generation": generation,
        "invoke_started_at": started,
        "last_invoke_at": last_invoke,
        "updated_at": updated_at,
        "node_id": node_id,
        "cessation_evidence": "brick_restart",
    }


def _drained_loss_cessation(view, identity):
    """Prove the exact drained dispatch has no valid restoration path.

    ``evicted/node_gone`` is written for a dormant session only after the
    control plane confirms the owning brick instance departed and finds no
    surviving local artifact or exported bundle target that can relight it.
    The interrupted marker was committed by the guest before banking and
    carries the opaque dispatch ID, CLI transcript and sequence. Requiring both
    facts distinguishes permanent loss of this drained incarnation from mere
    cessation, a generic missing guest, a transient read failure, or a
    banked/relighting session that remains resumable.
    """
    if (
        not isinstance(view, dict)
        or view.get("session_id") != identity["guest_id"]
        or view.get("state") != "evicted"
        or view.get("terminal_reason") != "node_gone"
    ):
        return None
    generation = view.get("generation")
    started = view.get("invoke_started_at")
    completed = view.get("last_invoke_at")
    updated = view.get("updated_at")
    interrupted = view.get("interrupted_turn")
    node = view.get("node")
    node_id = node.get("node_id") if isinstance(node, dict) else None
    if (
        type(generation) is not int
        or generation < 0
        or type(started) is not int
        or started < 1
        or type(completed) is not int
        or completed < started
        or type(updated) is not int
        or updated < completed
        or not isinstance(node_id, str)
        or not node_id
        or not isinstance(interrupted, dict)
        or interrupted.get("seq") != identity["seq"]
        or interrupted.get("dispatch_id") != identity["dispatch_id"]
        or interrupted.get("cli_session_id") != identity["cli_session_id"]
        or not isinstance(interrupted.get("transcript_path"), str)
        or not interrupted["transcript_path"]
    ):
        return None
    return {
        "session_id": identity["guest_id"],
        "state": "evicted",
        "terminal_reason": "node_gone",
        "generation": generation,
        "invoke_started_at": started,
        "last_invoke_at": completed,
        "updated_at": updated,
        "node_id": node_id,
        "turn_seq": identity["seq"],
        "dispatch_id": identity["dispatch_id"],
        "cli_session_id": identity["cli_session_id"],
        "transcript_path": interrupted["transcript_path"],
        "cessation_evidence": "drained_node_gone_no_relight_target",
        "workspace_recovery": "permanently_lost",
    }


def _replacement_invocation_cessation(view, identity, saved):
    """Prove that a same-guest control-plane record replaced the old invoke.

    The saved stop intent was observed and committed after this attempt had
    already failed. A later valid precondition for the same guest proves the
    old invocation ceased only when its generation advanced, its monotonic
    invoke stamp advanced on the same VM identity, or its completion stamp
    covers the saved invoke. A restart with missing or rolled-back stamps is
    absence of evidence and deliberately proves nothing.
    """
    if (
        saved is None
        or view.get("state") in {"evicted", "destroyed"}
        or view.get("session_id") != identity["guest_id"]
        or view.get("stop_precondition") is None
    ):
        return None
    recorded = _precondition(saved.get("precondition"), identity["guest_id"])
    current = _precondition(view.get("stop_precondition"), identity["guest_id"])
    previous_generation = recorded["generation"]
    current_generation = current["generation"]
    previous_started = recorded["invoke_started_at"]
    current_started = current["invoke_started_at"]
    last_invoke = view.get("last_invoke_at")
    if last_invoke is not None and (type(last_invoke) is not int or last_invoke < 1):
        raise ValueError("invalid_replacement_completion")

    reason = None
    if current_generation > previous_generation:
        reason = "generation_advanced"
    elif current_generation == previous_generation:
        stable_keys = _PRECONDITION_KEYS - {
            "generation",
            "invoke_started_at",
            "session_id",
        }
        if any(current[key] != recorded[key] for key in stable_keys):
            raise ValueError("replacement_identity_changed")
        if (
            type(previous_started) is int
            and type(current_started) is int
            and current_started > previous_started
        ):
            reason = "invoke_advanced"
        elif (
            type(previous_started) is int
            and type(last_invoke) is int
            and last_invoke >= previous_started
            and current_started != previous_started
        ):
            reason = "invoke_completed"
    if reason is None:
        return None
    return {
        "session_id": identity["guest_id"],
        "state": view.get("state"),
        "replacement_evidence": reason,
        "previous_generation": previous_generation,
        "generation": current_generation,
        "previous_invoke_started_at": previous_started,
        "invoke_started_at": current_started,
        "last_invoke_at": last_invoke,
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
    from factory.execution.transport import EmberVmShimTransport

    async def request():
        transport = EmberVmShimTransport()
        operation = (
            transport.get_session(guest_id)
            if precondition is None
            else transport.destroy_session(guest_id, stop_precondition=precondition)
        )
        return await asyncio.wait_for(operation, timeout=HTTP_SECONDS)

    return asyncio.run(request())


def _note(pin, reason, *, error=None):
    # Fixed reason codes, once each per attempt; repeated polling never creates
    # an unbounded audit stream or copies request/response bodies. ValueError
    # text is itself a fixed refusal code and is retained for diagnosis.
    with controls._locked_session() as (db, _control):
        previous = _records(db, pin)
        if not any(
            action == "stop_observation" and detail.get("reason") == reason
            for action, detail in previous
        ):
            detail = {
                "reason": reason,
                "intervention_required": True,
                "cessation_confirmed": False,
            }
            if error is not None:
                detail["error"] = error
            _audit(db, pin, "stop_observation", **detail)


def _transient_retry_enabled():
    return (
        os.environ.get("FACTORY_TRANSIENT_STOP_RETRY_ENABLED", "false").lower()
        == "true"
    )


def _max_stop_requests():
    if _transient_retry_enabled():
        return TRANSIENT_MAX_STOP_REQUESTS
    return MAX_STOP_REQUESTS


def _retry_details(pin, identity, *, refusal, deadline, error=None):
    detail = {
        "retry_kind": "transient_stop_observation",
        "refusal": refusal,
        "retry_deadline_at": deadline.isoformat(),
        "node_key": pin.get("node_key"),
        "attempt": pin.get("attempt"),
        "session_id": identity["session_id"],
        "guest_id": identity["guest_id"],
        "identity_sha256": identity["identity_sha256"],
        "missing_proof": (
            "No positive cessation proof bound to this exact factory dispatch "
            "and guest incarnation was observed."
        ),
    }
    if error is not None:
        detail["error"] = error
    return detail


def _transient_records(records, identity):
    matching = [
        detail
        for action, detail in records
        if action == "stop_observation"
        and detail.get("retry_kind") == "transient_stop_observation"
        and detail.get("identity_sha256") == identity["identity_sha256"]
    ]
    for index in range(len(matching) - 1, -1, -1):
        if matching[index].get("retry_resolved") is True:
            return matching[index + 1 :]
    return matching


def _transient_retry_gate(db, pin, identity, records):
    """Return the durable state of one transient retry window.

    The first failed observation fixes the deadline. Audit rows are the retry
    schedule, so a process restart cannot reset it and concurrent ticks cannot
    add more than one sample per interval. Exhaustion fences further retry
    samples and notifications, but does not prevent later proof observation.
    """
    if not _transient_retry_enabled():
        return "due"
    attempts = _transient_records(records, identity)
    if any(detail.get("retry_exhausted") is True for detail in attempts):
        return "exhausted"
    samples = [detail for detail in attempts if detail.get("retry_sample") is True]
    if not samples:
        return "due"
    deadline = _timestamp(samples[0]["retry_deadline_at"])
    now = _now()
    if now >= deadline:
        last = samples[-1]
        _audit(
            db,
            pin,
            "stop_observation",
            reason="stop_supervision_retry_exhausted",
            retry_exhausted=True,
            retry_started_at=samples[0]["retry_started_at"],
            observations=len(samples),
            intervention_required=True,
            cessation_confirmed=False,
            **_retry_details(
                pin,
                identity,
                refusal=last["refusal"],
                deadline=deadline,
                error=last.get("error"),
            ),
        )
        return "newly_exhausted"
    latest = _timestamp(
        samples[-1].get("retry_observed_at", samples[-1]["recorded_at"])
    )
    if (now - latest).total_seconds() < TRANSIENT_RETRY_INTERVAL_SECONDS:
        return "waiting"
    return "due"


def _record_transient_refusal(pin, session_id, identity, refusal, *, error=None):
    """Persist one sampled refusal or the single exhaustion intervention."""
    with controls._locked_session() as (db, control):
        current, _run = _locked_attempt(
            db, control, pin, session_id, require_stop_due=True
        )
        if current != identity:
            raise ValueError("factory_attempt_changed")
        records = _records(db, pin)
        gate = _transient_retry_gate(db, pin, identity, records)
        if gate != "due":
            return gate
        samples = _transient_records(records, identity)
        started_at = _now()
        if samples:
            started_at = _timestamp(samples[0]["retry_started_at"])
        deadline = started_at + timedelta(seconds=TRANSIENT_RETRY_WINDOW_SECONDS)
        _audit(
            db,
            pin,
            "stop_observation",
            reason=refusal,
            retry_sample=True,
            retry_started_at=started_at.isoformat(),
            retry_observed_at=_now().isoformat(),
            observation=len(samples) + 1,
            intervention_required=False,
            cessation_confirmed=False,
            **_retry_details(
                pin,
                identity,
                refusal=refusal,
                deadline=deadline,
                error=error,
            ),
        )
        return "waiting"


def _resolve_transient_retry(pin, session_id, identity, resolution):
    """Close a recovered transient epoch before another proof path begins."""
    if not _transient_retry_enabled():
        return
    with controls._locked_session() as (db, control):
        current, _run = _locked_attempt(
            db, control, pin, session_id, require_stop_due=True
        )
        if current != identity:
            raise ValueError("factory_attempt_changed")
        attempts = _transient_records(_records(db, pin), identity)
        if not attempts or any(
            detail.get("retry_exhausted") is True for detail in attempts
        ):
            return
        _audit(
            db,
            pin,
            "stop_observation",
            reason="stop_supervision_retry_resolved",
            retry_kind="transient_stop_observation",
            retry_resolved=True,
            resolution=resolution,
            identity_sha256=identity["identity_sha256"],
            session_id=identity["session_id"],
            guest_id=identity["guest_id"],
            intervention_required=False,
            cessation_confirmed=False,
        )


def _defer_transient_refusal(pin, session_id, identity, refusal, *, error=None):
    if not _transient_retry_enabled():
        if error is None:
            _note(pin, refusal)
        else:
            _note(pin, refusal, error=error)
        return
    try:
        _record_transient_refusal(pin, session_id, identity, refusal, error=error)
    except ValueError as exc:
        _note(pin, "local_identity_unconfirmed", error=str(exc))


def _node_gone_note(
    pin, reason, node_id, *, exception=None, intervention_required=True
):
    """Record one bounded node-loss intervention observation."""
    with controls._locked_session() as (db, _control):
        previous = _records(db, pin)
        if any(
            action == "stop_observation" and detail.get("reason") == reason
            for action, detail in previous
        ):
            return
        detail = {
            "reason": reason,
            "node_id": node_id,
            "intervention_required": intervention_required,
            "cessation_confirmed": False,
        }
        if exception is not None:
            detail["exception"] = exception
        _audit(db, pin, "stop_observation", **detail)


def _reserve_node_gone_destroy(pin, node_id, precondition):
    """Consume one durable request slot before contacting the control plane."""
    with controls._locked_session() as (db, _control):
        previous = _records(db, pin)
        requests = sum(
            action == "stop_observation"
            and detail.get("reason") == "guest_node_gone_destroy_requested"
            for action, detail in previous
        )
        if requests >= MAX_NODE_GONE_DESTROY_REQUESTS:
            if not any(
                action == "stop_observation"
                and detail.get("reason") == "guest_node_gone_destroy_exhausted"
                for action, detail in previous
            ):
                _audit(
                    db,
                    pin,
                    "stop_observation",
                    reason="guest_node_gone_destroy_exhausted",
                    node_id=node_id,
                    destroy_requests=requests,
                    intervention_required=True,
                    cessation_confirmed=False,
                )
            return False
        _audit(
            db,
            pin,
            "stop_observation",
            reason="guest_node_gone_destroy_requested",
            node_id=node_id,
            request_number=requests + 1,
            precondition=precondition,
            intervention_required=False,
            cessation_confirmed=False,
        )
        return True


def _destroy_guest(guest_id, precondition):
    from factory.execution.transport import EmberVmShimTransport

    async def request():
        return await asyncio.wait_for(
            # The precondition is recorded on the audit for the operator; it is
            # not sent. The control plane accepts no precondition on a parked
            # or banked session (#6091), and a guest on a departed node cannot
            # be relit between the observation and this request (#5502).
            EmberVmShimTransport().destroy_session(guest_id),
            timeout=HTTP_SECONDS,
        )

    return asyncio.run(request())


def _destroy_guest_on_departed_node(pin, identity, view):
    """Request teardown for an old parked guest whose Kubernetes node is gone."""
    node = view.get("node") or {}
    node_id = node.get("node_id") if isinstance(node, dict) else None
    updated_at = view.get("updated_at")
    if (
        view.get("state") not in {"parked", "banked"}
        or not isinstance(node_id, str)
        or not node_id
        or type(updated_at) is not int
        or updated_at <= 0
        or int(_now().timestamp() * 1000) - updated_at <= NODE_GONE_GRACE_SECONDS * 1000
    ):
        return False

    from cluster.kubernetes import cluster_node_names

    node_names = asyncio.run(cluster_node_names())
    if node_names is None or node_id in node_names:
        return False
    generation = view.get("generation")
    if type(generation) is not int or generation < 0:
        _node_gone_note(
            pin,
            "guest_node_gone_destroy_failed",
            node_id,
            intervention_required=True,
        )
        return True
    precondition = {"generation": generation}
    if not _reserve_node_gone_destroy(pin, node_id, precondition):
        return True
    try:
        _destroy_guest(identity["guest_id"], precondition)
    except Exception as exc:
        _node_gone_note(
            pin,
            "guest_node_gone_destroy_failed",
            node_id,
            exception=type(exc).__name__,
            intervention_required=True,
        )
        return True
    return True


def _settle_failed_attempt(
    db,
    pin,
    session_id,
    identity,
    run,
    original_result,
    *,
    reason,
    evidence_key,
    evidence,
    settle_attempt=None,
):
    """Record one uncertain attempt as failed, under a lock the caller holds.

    Shared by the two proofs that release a guest which was actually bound:
    exact control-plane cessation, and sustained absence of the guest record.
    Both settle at the unknown cost the attempt already carries rather than
    refunding it, because a bound guest did run; only the never-bound proofs
    settle at a measured zero. The caller owns the lock, the identity
    recheck and the proof. This owns the four writes that have to land in one
    transaction, so a settlement can never leave the run and the start
    disagreeing about whether the reservation was released.
    """
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
        type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0
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
        "reason": reason,
        "previous_outcome": json.loads(run.outcome_json or "{}"),
        evidence_key: evidence,
    }
    if settle_attempt is None:
        settle_attempt = settle_uncertain_factory_attempt
    settle_attempt(db, pin, identity)
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
    return result


def _reconcile_drained_lost_attempt(pin, session_id, original_result):
    """Settle one exact orphaned drain, or report that this is another shape.

    Returns ``(applicable, settled)``. Once a drain is applicable, every
    uncertain or live observation leaves it on its ordinary resumable path and
    prevents the general UNKNOWN settlement loop from consuming its retry.
    """
    if (
        os.environ.get("FACTORY_DRAINED_LOSS_SETTLEMENT_ENABLED", "false").lower()
        != "true"
    ):
        return False, False
    try:
        with controls._locked_session() as (db, control):
            identity, _run = _locked_attempt(
                db,
                control,
                pin,
                session_id,
                require_stop_due=False,
                identity_reader=read_drained_lost_factory_attempt,
            )
    except ValueError:
        return False, False

    try:
        view = _http(identity["guest_id"])
    except Exception:
        _note(pin, "drained_loss_observation_unavailable")
        return True, False
    proof = _drained_loss_cessation(view, identity)
    if proof is None:
        if (
            isinstance(view, dict)
            and view.get("state") == "evicted"
            and view.get("terminal_reason") == "node_gone"
        ):
            _note(pin, "drained_loss_evidence_changed")
        return True, False
    try:
        with controls._locked_session() as (db, control):
            current, run = _locked_attempt(
                db,
                control,
                pin,
                session_id,
                require_stop_due=False,
                identity_reader=read_drained_lost_factory_attempt,
            )
            if current != identity:
                raise ValueError("factory_attempt_changed")
            records = _records(db, pin)
            if any(action == "stop_settled" for action, _ in records):
                return True, True
            _settle_failed_attempt(
                db,
                pin,
                session_id,
                identity,
                run,
                original_result,
                reason=(
                    "drained_guest_permanently_lost: exact interrupted dispatch "
                    "has no control-plane relight target after node departure"
                ),
                evidence_key="drained_loss",
                evidence=proof,
                settle_attempt=settle_drained_lost_factory_attempt,
            )
            _audit(
                db,
                pin,
                "stop_settled",
                identity=identity,
                drained_loss=proof,
                cessation_confirmed=True,
                intervention_required=False,
            )
            return True, True
    except ValueError as exc:
        _note(pin, "drained_loss_evidence_or_ownership_changed", error=str(exc))
        return True, False


def _absence_run(records, identity):
    """The unbroken tail of absence observations for this exact attempt.

    Walked newest first and stopped at the first thing that is not this
    attempt's absence, so only a continuous episode counts. Counting every
    absence record ever written for the identity instead would let a 404 from
    one rollout and two from another, hours apart, add up to a release: each
    reading would be real, but "absent now, and absent twice before" is not
    the same claim as "absent throughout", and only the second one orders the
    guest after its process.
    """
    run = []
    previous = None
    for action, detail in reversed(records):
        if (
            action != "stop_absence"
            or detail.get("identity_sha256") != identity["identity_sha256"]
        ):
            break
        stamp = _timestamp(detail["recorded_at"])
        if (
            previous is not None
            and (previous - stamp).total_seconds() > ABSENCE_MAX_GAP_SECONDS
        ):
            break
        run.append(stamp)
        previous = stamp
    run.reverse()
    return run


def _record_presence(pin, identity):
    """Break the absence run when the control plane answers for this guest.

    Without this the run has nothing to break on. Every other record the
    guest-visible path writes is deduplicated: _remember_observation writes
    stop_intent and stop_accepted once each, stop_request is capped at
    MAX_STOP_REQUESTS, and _note writes one row per reason code. So after the
    first few ticks a successful 200 read leaves no trace, and a control plane
    that 404s intermittently but at least once inside every gap window would
    look exactly like one that had torn the guest down. This writes the trace,
    so a single observed guest resets the evidence.

    Written only while an absence run is actually open, and capped, so a
    healthy attempt never accumulates rows.
    """
    with controls._locked_session() as (db, _control):
        records = _records(db, pin)
        if not records or records[-1][0] != "stop_absence":
            return
        # Deliberately uncapped. Presence is the only record that can break an
        # absence run once the other actions have deduplicated themselves, so a
        # cap on it would restore exactly the blindness it exists to remove: a
        # guest answering 200 would stop leaving a trace and a flapping control
        # plane could accumulate a release again. The open-run guard above is
        # the bound that matters, and it already keeps a healthy attempt at
        # zero rows.
        _audit(
            db,
            pin,
            "stop_presence",
            identity_sha256=identity["identity_sha256"],
            guest_id=identity["guest_id"],
            cessation_confirmed=False,
            intervention_required=False,
        )


def _absence_settled(pin, session_id, identity, original_result):
    """Record one authoritative absence, and settle once absence has held.

    The control plane answers 404/410 only for a session it no longer holds,
    and it removes the record after teardown, so absence orders the guest
    after its own process. What it cannot do on a single reading is
    distinguish a torn-down guest from a control plane that has briefly lost
    sight of a live one, so this samples the window: one observation per
    ABSENCE_OBSERVATION_INTERVAL_SECONDS, and settlement only once an unbroken
    run of at least MIN_ABSENCE_OBSERVATIONS of them spans
    ABSENCE_CONFIRM_SECONDS end to end.

    Every observation is pinned to identity_sha256, so an attempt that changes
    underneath supervision starts its evidence again rather than inheriting a
    previous attempt's count. The audit stream is bounded twice over: one row
    per interval, and never more than MAX_ABSENCE_OBSERVATIONS per attempt.
    The settlement itself is fenced by the stop_settled record the cessation
    path already uses.
    """
    now = _now()
    with controls._locked_session() as (db, control):
        current, run = _locked_attempt(
            db, control, pin, session_id, require_stop_due=True
        )
        if current != identity:
            raise ValueError("factory_attempt_changed")
        records = _records(db, pin)
        if any(action == "stop_settled" for action, _ in records):
            return True
        seen = _absence_run(records, identity)
        newest = seen[-1] if seen else None
        due = (
            newest is None
            or (now - newest).total_seconds() >= ABSENCE_OBSERVATION_INTERVAL_SECONDS
        )
        if due:
            # Sampling never stops while absence holds. Suppressing the write
            # past a cap would freeze the newest reading, and the freshness
            # guard below would then refuse this run forever. A run that grows
            # past the expected length is an anomaly worth seeing rather than a
            # reason to stop looking, so it is noted once and keeps sampling.
            if len(seen) >= MAX_ABSENCE_OBSERVATIONS:
                _note(pin, "absence_run_unsettled")
            _audit(
                db,
                pin,
                "stop_absence",
                identity_sha256=identity["identity_sha256"],
                observation=len(seen) + 1,
                guest_id=identity["guest_id"],
                cessation_confirmed=False,
                intervention_required=False,
            )
            return False
        if len(seen) < MIN_ABSENCE_OBSERVATIONS:
            return False
        # Below the cap this is implied, because settlement can only be reached
        # on a tick where no observation was due. At the cap the write is
        # skipped, so without this a run whose last reading is hours old would
        # settle on evidence that stopped being refreshed: the guest could have
        # been answering for all of it. The freshest reading has to be as
        # recent as the run's own gap rule demands.
        if (now - seen[-1]).total_seconds() > ABSENCE_MAX_GAP_SECONDS:
            return False
        first = seen[0]
        # Measured between observations, never up to now: the span has to be
        # one this actually watched, not one it inferred from a single old
        # reading and the clock.
        held = (newest - first).total_seconds()
        if held < ABSENCE_CONFIRM_SECONDS:
            return False
        proof = {
            "guest_id": identity["guest_id"],
            "observations": len(seen),
            "first_observed_at": first.isoformat(),
            "last_observed_at": newest.isoformat(),
            "confirmed_at": now.isoformat(),
            "held_seconds": int(held),
        }
        _settle_failed_attempt(
            db,
            pin,
            session_id,
            identity,
            run,
            original_result,
            reason=(
                "guest_absent_confirmed: the control plane has reported this "
                f"guest absent since {first.isoformat()}"
            ),
            evidence_key="absence",
            evidence=proof,
        )
        _audit(
            db,
            pin,
            "stop_settled",
            identity=identity,
            absence=proof,
            cessation_confirmed=True,
            intervention_required=False,
        )
        return True


def _bound_zero_turn_evidence(view: dict, identity: dict) -> dict:
    """Validate the additive SessionView contract emitted by EmberVM.

    These fields are produced by ``session_view/2`` in the control-plane
    router. Requiring the producer-shaped payload avoids a self-consistent test
    fixture accidentally turning an invented provider contract into authority.
    """
    if not isinstance(view, dict) or view.get("session_id") != identity["guest_id"]:
        raise ValueError("wrong_bound_zero_turn_session")
    for field in ("workload", "principal", "base_digest"):
        if not isinstance(view.get(field), str) or not view[field]:
            raise ValueError("malformed_bound_zero_turn_observation")
    for field in (
        "created_at",
        "invoke_started_at",
        "last_invoke_at",
        "expires_at",
        "updated_at",
        "turn_seq",
        "generation",
    ):
        if type(view.get(field)) is not int or view[field] < 0:
            raise ValueError("malformed_bound_zero_turn_observation")
    if (
        view["invoke_started_at"] < 1
        or view["last_invoke_at"] < view["invoke_started_at"]
        or view["updated_at"] < view["last_invoke_at"]
        or view["turn_seq"] < 1
        or view.get("state") != "running"
        or view.get("terminal_reason") is not None
    ):
        raise ValueError("bound_zero_turn_invoke_not_complete")
    node = view.get("node")
    if (
        not isinstance(node, dict)
        or not isinstance(node.get("node_id"), str)
        or not node["node_id"]
        or node.get("health") != "healthy"
        or type(node.get("draining")) is not bool
        or node["draining"]
    ):
        raise ValueError("bound_zero_turn_node_not_stable")
    precondition = _precondition(view.get("stop_precondition"), identity["guest_id"])
    if (
        precondition["generation"] != view["generation"]
        or precondition["invoke_started_at"] != view["invoke_started_at"]
        or precondition["node_id"] != node["node_id"]
    ):
        raise ValueError("bound_zero_turn_precondition_changed")
    return {
        "kind": "completed_invoke",
        "session_id": view["session_id"],
        "generation": view["generation"],
        "turn_seq": view["turn_seq"],
        "invoke_started_at": view["invoke_started_at"],
        "last_invoke_at": view["last_invoke_at"],
        "updated_at": view["updated_at"],
        "node_id": node["node_id"],
        "precondition": precondition,
    }


def _bound_zero_turn_reset(pin, session_id, identity, reason, *, release=False):
    """Break one observation run, optionally releasing its exact fence."""
    try:
        with controls._locked_session() as (db, control):
            current, _run = _locked_attempt(
                db,
                control,
                pin,
                session_id,
                require_stop_due=False,
                identity_reader=read_bound_zero_turn_factory_attempt,
            )
            if current["identity_sha256"] != identity["identity_sha256"]:
                raise ValueError("factory_attempt_changed")
            if release and current["cleanup_fenced"]:
                release_bound_zero_turn_factory_fence(db, pin, current)
                current = {**current, "cleanup_fenced": False}
            records = _records(db, pin)
            if not (
                records
                and records[-1][0] == "bound_zero_turn_reset"
                and records[-1][1].get("reason") == reason
            ):
                _audit(
                    db,
                    pin,
                    "bound_zero_turn_reset",
                    reason=reason,
                    identity_sha256=identity["identity_sha256"],
                    observed_at=_now().isoformat(),
                    intervention_required=False,
                    cessation_confirmed=False,
                )
    except ValueError:
        return False
    return True


def _bound_zero_turn_observe(pin, session_id, identity, evidence):
    """Persist two unchanged samples strictly beyond the node turn timeout."""
    now = _now()
    with controls._locked_session() as (db, control):
        current, _run = _locked_attempt(
            db,
            control,
            pin,
            session_id,
            require_stop_due=False,
            identity_reader=read_bound_zero_turn_factory_attempt,
        )
        if current != identity or current["cleanup_fenced"]:
            raise ValueError("factory_attempt_changed")
        records = _records(db, pin)
        previous = None
        if records and records[-1][0] == "bound_zero_turn_observation":
            candidate = records[-1][1]
            if (
                candidate.get("identity_sha256") == identity["identity_sha256"]
                and candidate.get("evidence") == evidence
            ):
                previous = candidate
        if previous is None:
            _audit(
                db,
                pin,
                "bound_zero_turn_observation",
                identity_sha256=identity["identity_sha256"],
                evidence=evidence,
                observed_at=now.isoformat(),
                observation=1,
                intervention_required=False,
                cessation_confirmed=False,
            )
            return "waiting", identity
        first = _timestamp(previous["observed_at"])
        if (now - first).total_seconds() <= pin["turn_timeout_seconds"]:
            return "waiting", identity
        fence_bound_zero_turn_factory_attempt(db, pin, identity)
        fenced = {**identity, "cleanup_fenced": True}
        _audit(
            db,
            pin,
            "bound_zero_turn_fence",
            identity=fenced,
            evidence=evidence,
            first_observed_at=first.isoformat(),
            observed_at=now.isoformat(),
            held_seconds=int((now - first).total_seconds()),
            intervention_required=False,
            cessation_confirmed=False,
        )
        return "fenced", fenced


def _settle_bound_zero_turn(pin, session_id, identity, original_result, proof) -> bool:
    with controls._locked_session() as (db, control):
        current, run = _locked_attempt(
            db,
            control,
            pin,
            session_id,
            require_stop_due=False,
            identity_reader=read_bound_zero_turn_factory_attempt,
        )
        if current != identity or not current["cleanup_fenced"]:
            raise ValueError("factory_attempt_changed")
        if any(action == "stop_settled" for action, _ in _records(db, pin)):
            return True
        _settle_failed_attempt(
            db,
            pin,
            session_id,
            identity,
            run,
            original_result,
            reason=(
                "bound_zero_turn_delivery_error: completed guest invocation "
                "produced no local turn"
            ),
            evidence_key="bound_zero_turn",
            evidence=proof,
            settle_attempt=settle_bound_zero_turn_factory_attempt,
        )
        _audit(
            db,
            pin,
            "stop_settled",
            identity=identity,
            bound_zero_turn=proof,
            cessation_confirmed=True,
            intervention_required=False,
        )
        return True


def _reserve_bound_zero_turn_request(db, pin, records, identity, precondition) -> bool:
    """Durably reserve one of the bounded conditional destroy attempts."""
    requests = [
        detail for action, detail in records if action == "bound_zero_turn_request"
    ]
    if len(requests) >= MAX_BOUND_ZERO_TURN_DESTROY_REQUESTS:
        if not any(
            action == "stop_observation"
            and detail.get("reason") == "bound_zero_turn_destroy_exhausted"
            for action, detail in records
        ):
            _audit(
                db,
                pin,
                "stop_observation",
                reason="bound_zero_turn_destroy_exhausted",
                identity_sha256=identity["identity_sha256"],
                destroy_requests=len(requests),
                intervention_required=True,
                cessation_confirmed=False,
            )
        return False
    if (
        requests
        and (_now() - _timestamp(requests[-1]["observed_at"])).total_seconds()
        < BOUND_ZERO_TURN_REQUEST_INTERVAL_SECONDS
    ):
        return False
    _audit(
        db,
        pin,
        "bound_zero_turn_request",
        identity_sha256=identity["identity_sha256"],
        request_number=len(requests) + 1,
        precondition=precondition,
        observed_at=_now().isoformat(),
        intervention_required=False,
        cessation_confirmed=False,
    )
    return True


def _drive_bound_zero_turn_fence(
    pin, session_id, identity, original_result, saved, view
) -> bool:
    """Observe or conditionally stop only the invocation named by the fence."""
    from factory.execution.transport import EmberSessionGone

    if isinstance(view, EmberSessionGone):
        return _settle_bound_zero_turn(
            pin,
            session_id,
            identity,
            original_result,
            {
                **saved,
                "cessation": "authoritative_absence",
                "confirmed_at": _now().isoformat(),
            },
        )
    if not isinstance(view, dict) or view.get("session_id") != identity["guest_id"]:
        raise ValueError("wrong_bound_zero_turn_session")
    proof = _completion(view, saved["evidence"]["precondition"])
    if proof is not None:
        return _settle_bound_zero_turn(
            pin,
            session_id,
            identity,
            original_result,
            {**saved, "cessation": proof, "confirmed_at": _now().isoformat()},
        )
    # A validated stop intent with no completion is an accepted in-flight
    # conditional cleanup. Keep its local fence and observe on later ticks.
    if view.get("stop_intent") is not None:
        return False
    try:
        current_evidence = _bound_zero_turn_evidence(view, identity)
    except ValueError as exc:
        _bound_zero_turn_reset(
            pin,
            session_id,
            identity,
            str(exc),
            release=True,
        )
        return False
    if current_evidence != saved["evidence"]:
        _bound_zero_turn_reset(
            pin,
            session_id,
            identity,
            "bound_zero_turn_remote_progress",
            release=True,
        )
        return False
    with controls._locked_session() as (db, control):
        current, _run = _locked_attempt(
            db,
            control,
            pin,
            session_id,
            require_stop_due=False,
            identity_reader=read_bound_zero_turn_factory_attempt,
        )
        if current != identity:
            raise ValueError("factory_attempt_changed")
        records = _records(db, pin)
        if not _reserve_bound_zero_turn_request(
            db,
            pin,
            records,
            identity,
            saved["evidence"]["precondition"],
        ):
            return False
    try:
        _http(identity["guest_id"], saved["evidence"]["precondition"])
    except EmberSessionGone:
        return _settle_bound_zero_turn(
            pin,
            session_id,
            identity,
            original_result,
            {
                **saved,
                "cessation": "authoritative_absence",
                "confirmed_at": _now().isoformat(),
            },
        )
    except Exception:
        return False
    return False


def _reconcile_bound_zero_turn_attempt(pin, session_id, original_result):
    """Return (handled, settled) for the staged #6288 proof."""
    if (
        os.environ.get("FACTORY_BOUND_ZERO_TURN_SETTLEMENT_ENABLED", "false").lower()
        != "true"
    ):
        return False, False
    try:
        with controls._locked_session() as (db, control):
            identity, _run = _locked_attempt(
                db,
                control,
                pin,
                session_id,
                require_stop_due=False,
                identity_reader=read_bound_zero_turn_factory_attempt,
            )
            records = _records(db, pin)
            fences = [
                detail
                for action, detail in records
                if action == "bound_zero_turn_fence"
                and detail.get("identity", {}).get("identity_sha256")
                == identity["identity_sha256"]
            ]
    except ValueError as exc:
        if str(exc) == "factory_bound_zero_turn_not_applicable":
            return False, False
        _note(pin, "bound_zero_turn_local_identity_unconfirmed", error=str(exc))
        return True, False
    if identity["cleanup_fenced"]:
        if len(fences) != 1:
            _note(pin, "bound_zero_turn_fence_unconfirmed")
            return True, False
        from factory.execution.transport import EmberSessionGone

        try:
            view = _http(identity["guest_id"])
        except EmberSessionGone as exc:
            view = exc
        except Exception as exc:
            held_since = _timestamp(fences[0]["observed_at"])
            if (_now() - held_since).total_seconds() >= COMPLETION_ALARM_SECONDS:
                _note(
                    pin,
                    "bound_zero_turn_fenced_lookup_unavailable",
                    error=type(exc).__name__,
                )
            return True, False
        try:
            return True, _drive_bound_zero_turn_fence(
                pin, session_id, identity, original_result, fences[0], view
            )
        except ValueError as exc:
            _note(pin, "bound_zero_turn_fenced_evidence_changed", error=str(exc))
            return True, False

    from factory.execution.transport import EmberSessionGone

    try:
        view = _http(identity["guest_id"])
    except EmberSessionGone:
        evidence = {
            "kind": "authoritative_absence",
            "session_id": identity["guest_id"],
        }
    except Exception:
        _bound_zero_turn_reset(
            pin, session_id, identity, "bound_zero_turn_lookup_unavailable"
        )
        return True, False
    else:
        try:
            evidence = _bound_zero_turn_evidence(view, identity)
        except ValueError as exc:
            _bound_zero_turn_reset(pin, session_id, identity, str(exc))
            return True, False
    try:
        state, identity = _bound_zero_turn_observe(pin, session_id, identity, evidence)
        if state != "fenced":
            return True, False
        fence = next(
            detail
            for action, detail in _records_for_pin(pin)
            if action == "bound_zero_turn_fence"
            and detail.get("identity", {}).get("identity_sha256")
            == identity["identity_sha256"]
        )
        if evidence["kind"] == "authoritative_absence":
            return True, _settle_bound_zero_turn(
                pin,
                session_id,
                identity,
                original_result,
                {
                    **fence,
                    "cessation": "authoritative_absence",
                    "confirmed_at": _now().isoformat(),
                },
            )
        return True, _drive_bound_zero_turn_fence(
            pin, session_id, identity, original_result, fence, view
        )
    except (StopIteration, ValueError) as exc:
        _note(pin, "bound_zero_turn_evidence_changed", error=str(exc))
        return True, False


def _records_for_pin(pin):
    with controls._locked_session() as (db, _control):
        return _records(db, pin)


def reconcile_uncertain_attempt(pin, session_id, original_result, workflow_status):
    """One bounded supervision tick for an already terminal DBOS workflow.

    A live DBOS workflow still owns its deadline/cancellation path. No work is
    started or cancelled here. The legacy conditional request budget remains
    unless staged transient supervision is enabled; Ember owns retrying its
    accepted durable intent.
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
    drained, settled = _reconcile_drained_lost_attempt(pin, session_id, original_result)
    if drained:
        return settled
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
            retry_gate = _transient_retry_gate(db, pin, identity, _records(db, pin))
            if retry_gate in {"waiting", "newly_exhausted"}:
                return False
    except ValueError as exc:
        if str(exc) != "factory_stop_not_due":
            _note(pin, "local_identity_unconfirmed", error=str(exc))
        return False

    from factory.execution.transport import EmberSessionGone

    try:
        view = _http(identity["guest_id"])
    except EmberSessionGone:
        # Separated from the blanket failure below because the status code is
        # the whole distinction: get_session raises this only for 404/410,
        # which the control plane returns for a session it does not have.
        # 403, 500 and every timeout stay plain failures and fall through to
        # stop_observation_unavailable, so an unreachable control plane can
        # never be read as a torn-down guest.
        try:
            _resolve_transient_retry(
                pin, session_id, identity, "authoritative_guest_absence"
            )
            return _absence_settled(pin, session_id, identity, original_result)
        except ValueError as exc:
            if str(exc) != "factory_stop_not_due":
                _note(
                    pin,
                    "stop_evidence_or_ownership_changed",
                    error=str(exc),
                )
            return False
    except Exception:
        _defer_transient_refusal(
            pin,
            session_id,
            identity,
            "stop_observation_unavailable",
        )
        return False
    try:
        if not isinstance(view, dict) or view.get("session_id") != identity["guest_id"]:
            raise ValueError("wrong_stop_observation")
        # The control plane answered for this exact guest, so any absence run
        # in progress is over. Recorded before anything else acts on the view.
        _record_presence(pin, identity)
        if view.get("terminal_reason") == "interrupted_for_drain" and view.get(
            "state"
        ) in {"running", "banking", "banked", "parked", "relighting"}:
            _resolve_transient_retry(
                pin, session_id, identity, "drain_interruption_observed"
            )
            return False
        if _destroy_guest_on_departed_node(pin, identity, view):
            return False
        cessation = None
        if cessation_enabled:
            cessation = _control_plane_cessation(view, identity, saved)
            if cessation is None:
                cessation = _brick_restart_cessation(view, identity, saved)
            if cessation is None:
                cessation = _replacement_invocation_cessation(view, identity, saved)
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
                _settle_failed_attempt(
                    db,
                    pin,
                    session_id,
                    identity,
                    run,
                    original_result,
                    reason="guest_cessation_confirmed: exact control-plane cessation after factory dispatch",
                    evidence_key="cessation",
                    evidence=proof,
                )
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
            max_stop_requests = _max_stop_requests()
            if view.get("stop_intent") is not None or requests >= max_stop_requests:
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
            _resolve_transient_retry(
                pin, session_id, identity, "valid_stop_identity_observed"
            )
            # The durable local request budget was consumed before the external
            # effect. An observation timeout never creates another identity or
            # another request. Later ticks can only observe this operation.
            try:
                _http(identity["guest_id"], expected)
            except Exception:
                _defer_transient_refusal(
                    pin,
                    session_id,
                    identity,
                    "stop_request_unconfirmed",
                )
        elif requests >= max_stop_requests and view.get("stop_intent") is None:
            if _transient_retry_enabled():
                _defer_transient_refusal(
                    pin,
                    session_id,
                    identity,
                    "stop_request_unconfirmed",
                )
            else:
                _note(pin, "stop_request_bound_reached")
        elif view.get("stop_intent") is not None:
            _resolve_transient_retry(
                pin, session_id, identity, "accepted_stop_intent_observed"
            )
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
        if str(exc) == "missing_stop_precondition" and _transient_retry_enabled():
            _defer_transient_refusal(
                pin,
                session_id,
                identity,
                "missing_stop_precondition",
                error=str(exc),
            )
        elif not (cessation_enabled and str(exc) == "factory_stop_not_due"):
            _note(
                pin,
                "stop_evidence_or_ownership_changed",
                error=str(exc),
            )
    return False
