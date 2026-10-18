"""Acknowledged native usage and exact-attempt stops, using existing owners.

This is an observed-usage stop, not a pre-request spending guarantee. No policy
amendment may use it as evidence of a bounded request or complete attribution.
It never changes cash budgets, provider quotas, funding, or uncertainty fences.
"""

from collections import defaultdict
import hashlib
import hmac
import json
import math
import os

from sqlmodel import select

from factory.execution import admission, result_receipts
from factory.execution.models import AgentResultReceipt, AgentTurn
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_funding_limits import objective_task_ids
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
)
from shared.pricing import FIXED_PRICES, price_usage

BOUND = "runtime_meter_bound"
UPDATE = "runtime_meter_usage"
TRIP = "runtime_meter_tripped"
SESSION_USD = 500.0
OBJECTIVE_USD = 1000.0
COUNTERS = ("input_tokens", "output_tokens", "cached_input_tokens")
PRICE_MODEL = "gpt-6-astra"


def enabled():
    return os.getenv("FACTORY_RUNTIME_METER_ENABLED", "false").lower() == "true"


def _records(db, task_ids, actions):
    return [
        {**json.loads(row.detail_json), "audit_id": row.id, "task_id": row.task_id}
        for row in db.exec(
            select(FactoryAudit)
            .where(FactoryAudit.task_id.in_(task_ids), FactoryAudit.action.in_(actions))
            .order_by(FactoryAudit.id)
        ).all()
    ]


def _binding(db, receipt_id):
    # Durable audit identity survives native receipt/body retention.
    records = _records_for_receipt(db, receipt_id, BOUND)
    if len(records) != 1:
        raise result_receipts.ReceiptRejected(409, "meter_binding_missing")
    return records[0]


def _records_for_receipt(db, receipt_id, action):
    query = select(FactoryAudit).where(FactoryAudit.action == action)
    if action != BOUND:
        query = query.where(FactoryAudit.task_id == _binding(db, receipt_id)["task_id"])
    return [
        {**json.loads(row.detail_json), "task_id": row.task_id}
        for row in db.exec(query.order_by(FactoryAudit.id)).all()
        if json.loads(row.detail_json).get("receipt_id") == receipt_id
    ]


def bind(db, receipt, agent, request_body):
    """Called inside the control -> pool -> session lock before physical POST."""
    from factory.orchestration.factory_attempt_stop import enabled as stop_enabled

    if not stop_enabled():
        raise result_receipts.ReceiptRejected(409, "meter_requires_stop_supervision")
    start = db.exec(
        select(FactoryStart).where(FactoryStart.start_key == agent.workflow_id)
    ).one_or_none()
    if (
        start is None
        or start.status != "reserved"
        or start.session_id not in (None, agent.id)
        or start.model != agent.model
        or not agent.node_key
        or not agent.node_attempt
    ):
        raise result_receipts.ReceiptRejected(409, "meter_factory_identity_missing")
    owner = controls._receipt(db, start.task_id)
    request = json.loads(request_body)
    if (
        owner is None
        or not controls._can_start(
            db, db.get(FactoryControl, "factory"), start.task_id, start.start_key
        )["ok"]
        or agent.model != "astra"
        or agent.model not in controls._effective_policy(db, owner)["allowed_models"]
        or request.get("model") != agent.model
        or not request.get("dispatch_id")
    ):
        raise result_receipts.ReceiptRejected(
            409, "meter_adapter_or_authority_unavailable"
        )
    totals = objective_totals(db, owner)
    if totals["known_list_usd"] >= OBJECTIVE_USD:
        raise result_receipts.ReceiptRejected(409, "objective_list_backstop")
    if totals["sessions"].get(agent.id, 0) >= SESSION_USD:
        raise result_receipts.ReceiptRejected(409, "session_list_backstop")
    previous_turn = db.exec(
        select(AgentTurn.list_cost_usd).where(
            AgentTurn.session_id == agent.id, AgentTurn.seq == receipt.seq
        )
    ).first()
    previous_bindings = _records(db, objective_task_ids(db, owner), (BOUND,))
    # A drain continuation may start metering after an unmetered prefix. The
    # current AgentTurn is cumulative across those physical invokes. Preserve
    # only its uncovered prefix before adding this invoke's fresh counters.
    binding = {
        "receipt_id": receipt.id,
        "session_id": agent.id,
        "seq": receipt.seq,
        "guest_id": receipt.guest_id,
        "dispatch_id": request["dispatch_id"],
        "node_key": agent.node_key,
        "attempt": agent.node_attempt,
        "model": PRICE_MODEL,
        "prices": FIXED_PRICES[PRICE_MODEL],
        "pricing_sha256": hashlib.sha256(
            controls._json(FIXED_PRICES[PRICE_MODEL]).encode()
        ).hexdigest(),
        "objective_receipt_id": owner.id,
        "prior_turn_list_usd": previous_turn or 0,
        "prior_receipt_ids": [
            row["receipt_id"]
            for row in previous_bindings
            if row["session_id"] == agent.id and row["seq"] == receipt.seq
        ],
    }
    controls._audit(
        db, "factory:runtime-meter", BOUND, task_id=start.task_id, **binding
    )
    return True


def objective_totals(db, owner):
    """Reconcile provisional dispatch usage with final turns without summing both."""
    task_ids = objective_task_ids(db, owner)
    bindings = _records(db, task_ids, (BOUND,))
    updates = _records(db, task_ids, (UPDATE,))
    latest = {row["receipt_id"]: row for row in updates}
    by_turn = defaultdict(float)
    prefixes = defaultdict(float)
    for binding in bindings:
        key = binding["session_id"], binding["seq"]
        covered = sum(
            latest.get(receipt_id, {}).get("list_cost_usd", 0)
            for receipt_id in binding["prior_receipt_ids"]
        )
        prefixes[key] = max(prefixes[key], binding["prior_turn_list_usd"] - covered)
        update = latest.get(binding["receipt_id"])
        if update:
            by_turn[key] += update["list_cost_usd"]
    for key, prefix in prefixes.items():
        by_turn[key] += prefix
    # Use the established objective collector for lost start/run associations.
    from factory.orchestration.factory_progress_shadow import snapshot

    projection = snapshot(db, owner, controls._effective_policy(db, owner))["prices"]
    session_ids = {row["session_id"] for row in projection["sessions"]}
    session_ids.update(binding["session_id"] for binding in bindings)
    for sid, seq, listed in db.exec(
        select(AgentTurn.session_id, AgentTurn.seq, AgentTurn.list_cost_usd).where(
            AgentTurn.session_id.in_(session_ids)
        )
    ).all():
        if type(listed) in (int, float) and math.isfinite(listed) and listed >= 0:
            by_turn[sid, seq] = max(by_turn[sid, seq], listed)
    sessions = defaultdict(float)
    for (sid, _seq), cost in by_turn.items():
        sessions[sid] += cost
    return {
        "sessions": dict(sessions),
        "known_list_usd": sum(sessions.values()) + projection["supervision_list_usd"],
        "unknown_or_reserved_exposure_usd": projection[
            "unknown_or_reserved_exposure_usd"
        ],
        "coverage_complete": False,
        "hard_ceiling_enforced": False,
    }


def _validate_update(value):
    if not isinstance(value, dict) or set(value) != {"sequence", "usage", "coverage"}:
        raise result_receipts.ReceiptRejected(422, "invalid_meter_update")
    if type(value["sequence"]) is not int or not 0 <= value["sequence"] <= 1000000:
        raise result_receipts.ReceiptRejected(422, "invalid_meter_sequence")
    usage = value["usage"]
    if (
        not isinstance(usage, dict)
        or set(usage) != set(COUNTERS)
        or any(type(n) is not int or not 0 <= n <= 10**12 for n in usage.values())
        or usage["cached_input_tokens"] > usage["input_tokens"]
        or value["coverage"] not in ("parent", "unknown")
    ):
        raise result_receipts.ReceiptRejected(422, "invalid_meter_usage")
    return value


def ingest(receipt_id, token, value):
    """Acknowledge committed evidence first, then request the existing stop owner."""
    value = _validate_update(value)
    result_receipts.authenticate_receipt(receipt_id, token)
    with controls._locked_session() as (db, _control):
        native = db.get(AgentResultReceipt, receipt_id)
        if native is None or not hmac.compare_digest(
            native.token_sha256, hashlib.sha256(token.encode()).hexdigest()
        ):
            raise result_receipts.ReceiptRejected(401, "invalid_receipt_token")
        binding = _binding(db, receipt_id)
        previous = _records_for_receipt(db, receipt_id, UPDATE)
        last = previous[-1] if previous else None
        reason = None
        if last and value["sequence"] <= last["sequence"]:
            replay = next(
                (row for row in previous if row["sequence"] == value["sequence"]), None
            )
            if replay is None or any(replay[key] != value[key] for key in value):
                reason = "conflicting_meter_replay"
        elif (last is None and value["sequence"] != 0) or (
            last and value["sequence"] != last["sequence"] + 1
        ):
            reason = "meter_sequence_gap"
        elif last and any(value["usage"][key] < last["usage"][key] for key in COUNTERS):
            reason = "meter_counter_regression"
        else:
            priced = price_usage(binding["model"], value["usage"])
            listed = (
                0.0
                if not any(value["usage"].values())
                else priced.cost_usd
                if priced
                else None
            )
            if listed is None or binding["prices"] != FIXED_PRICES[PRICE_MODEL]:
                reason = "meter_pricing_unknown"
            elif last and listed < last["list_cost_usd"]:
                reason = "meter_price_regression"
            else:
                controls._audit(
                    db,
                    "factory:runtime-meter",
                    UPDATE,
                    task_id=binding["task_id"],
                    receipt_id=receipt_id,
                    list_cost_usd=listed,
                    **value,
                )
                db.flush()
        if value["coverage"] == "unknown":
            reason = reason or "meter_descendant_or_reset_unknown"
        owner = db.get(FactoryReceipt, binding["objective_receipt_id"])
        totals = objective_totals(db, owner)
        if totals["sessions"].get(binding["session_id"], 0) >= SESSION_USD:
            reason = reason or "session_list_backstop"
        if totals["known_list_usd"] >= OBJECTIVE_USD:
            reason = reason or "objective_list_backstop"
        admission.lock_pool(db)
        try:
            active = result_receipts._active_owner(
                db, result_receipts._receipt_metadata(db, receipt_id)
            )
        except result_receipts.ReceiptRejected:
            active = None
        trips = _records_for_receipt(db, receipt_id, TRIP)
        if reason and not trips:
            controls._audit(
                db,
                "factory:runtime-meter",
                TRIP,
                task_id=binding["task_id"],
                receipt_id=receipt_id,
                reason=reason,
            )
            owner.task_paused = True
            owner.updated_at = controls._now()
        stop = bool(reason or trips or active is None)
    # A failed external observation does not erase the committed trip. The
    # regular conductor tick retries through the same attempt-stop owner.
    if stop:
        try:
            stop_bound_attempt(receipt_id)
        except (ValueError, RuntimeError):
            pass
    return {
        "receipt_id": receipt_id,
        "sequence": value["sequence"],
        "stop": stop,
        "hard_ceiling_enforced": False,
    }


def stop_bound_attempt(receipt_id):
    from factory.orchestration import factory_attempt_stop as stops

    def authorize(db):
        admission.lock_pool(db)
        binding = _binding(db, receipt_id)
        if not _records_for_receipt(db, receipt_id, TRIP):
            raise ValueError("meter_not_tripped")
        native = result_receipts._receipt_metadata(db, receipt_id)
        if native is None or result_receipts._active_owner(db, native) is None:
            raise ValueError("meter_invocation_no_longer_active")
        return binding

    with controls._locked_session() as (db, _control):
        admission.lock_pool(db)
        binding = authorize(db)
    identity = stops.read_attempt_stop(
        binding["task_id"],
        binding["node_key"],
        binding["attempt"],
        binding["session_id"],
    )
    if identity["guest_id"] != binding["guest_id"]:
        raise ValueError("meter_guest_changed")
    return stops.request_attempt_stop(
        task_id=binding["task_id"],
        node_key=binding["node_key"],
        attempt=binding["attempt"],
        session_id=binding["session_id"],
        request_key="runtime-meter:" + receipt_id,
        expected_identity_sha256=identity["identity_sha256"],
        reason="native runtime usage backstop or incomplete metering",
        actor="factory:runtime-meter",
        authorization_check=authorize,
    )


def reconcile(task_id):
    """Retry durable trips on the existing tick, even after the feature is off."""
    with controls._read_session() as db:
        trips = _records(db, [task_id], (TRIP,))
    for trip in trips:
        try:
            stop_bound_attempt(trip["receipt_id"])
        except (ValueError, RuntimeError):
            continue
