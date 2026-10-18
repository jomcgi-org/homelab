"""Exact, paused-receipt operator amendments through existing funding authority.

No resume, graph replacement, retry, or budget renewal is implicit. Preview is
read-only; application requires the same control version and graph revision.
The existing $200 objective allocation ceiling remains binding. Raising it
requires a proven pre-request backstop, which observed-usage stops do not supply.
"""

from datetime import timedelta, timezone
import hashlib
import json

from sqlmodel import select

from factory.execution.models import AgentSession, PendingMessage
from factory.orchestration import factory_controls as controls, graph
from factory.orchestration import factory_funding_limits as funding
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt
from factory.orchestration.models import SwarmNodeRun

FIELDS = frozenset(
    {
        "task_budget_usd",
        "turn_budget_usd",
        "max_task_turns_hard",
        "max_turns_per_task",
        "max_planner_turns",
        "max_review_rounds",
        "task_timeout_seconds",
    }
)
ACTION = "operator_policy_amendment"


def _preview(db, control, receipt_id, overlay, direction):
    if (
        not isinstance(overlay, dict)
        or not overlay
        or not set(overlay) <= FIELDS | {"landing_hold", "nonrenewable_recovery"}
    ):
        raise ValueError("unsupported_policy_amendment")
    if "landing_hold" in overlay and overlay["landing_hold"] is not True:
        raise ValueError("landing_hold_can_only_be_added")
    if (
        "nonrenewable_recovery" in overlay
        and overlay["nonrenewable_recovery"] is not True
    ):
        raise ValueError("nonrenewable_recovery_can_only_be_added")
    direction = controls._text(direction, "direction", 4000)
    row = db.get(FactoryReceipt, receipt_id)
    if (
        row is None
        or row.state not in controls._ACTIVE
        or not row.task_paused
        or row.cancellation_requested
        or control.state == "stopped"
    ):
        raise ValueError("amendment_requires_paused_active_receipt")
    task = graph._lock_task(db, row.task_id)
    starts = controls._starts(db, row.task_id)
    runs = db.exec(
        select(SwarmNodeRun).where(SwarmNodeRun.task_id == row.task_id)
    ).all()
    # Unknown starts and live native claims survive graph cleanup and must
    # independently fence amendment. A changed policy cannot legalize them.
    if (
        any(start.status in {"reserved", "uncertain"} for start in starts)
        or any(run.status in {"admitted", "dispatched", "uncertain"} for run in runs)
        or db.exec(
            select(PendingMessage.id)
            .join(AgentSession)
            .where(
                AgentSession.local_session_id.startswith(
                    f"factory:{row.task_id}:", autoescape=True
                ),
                PendingMessage.claimed_by_replica.isnot(None),
            )
        ).first()
        is not None
    ):
        raise ValueError("amendment_unresolved_execution")
    from factory.orchestration.factory_decisions import decision_in_flight

    if decision_in_flight(db, row.id):
        raise ValueError("amendment_decision_in_flight")
    policy = funding.effective_policy(db, row)
    amended = controls.validate_policy(
        {**policy, **{k: v for k, v in overlay.items() if k in FIELDS}}
    )
    created = (
        task.created_at.replace(tzinfo=timezone.utc)
        if task.created_at.tzinfo is None
        else task.created_at
    )
    if created + timedelta(seconds=amended["task_timeout_seconds"]) <= controls._now():
        raise ValueError("amendment_task_deadline_expired")
    objective = funding.objective_for_receipt(db, row)
    spent = controls._accounting(starts)
    objective_allocation = (
        objective["committed_cost_usd"]
        - spent["committed_cost_usd"]
        + amended["task_budget_usd"]
    )
    if objective_allocation > funding.OBJECTIVE_CEILING_USD:
        raise ValueError("amendment_exceeds_objective_allocation_ceiling")
    if amended["task_budget_usd"] < spent["committed_cost_usd"]:
        raise ValueError("amendment_below_committed_exposure")
    revision = graph.current_version(row.task_id, session=db)
    result = {
        "receipt_id": row.id,
        "task_id": row.task_id,
        "expected_version": control.version,
        "expected_revision": revision,
        "previous_policy": policy,
        "policy_overlay": {
            key: amended[key] for key in FIELDS if amended.get(key) != policy.get(key)
        },
        "direction": direction,
        "objective": objective,
        "committed_cost_usd": spent["committed_cost_usd"],
        "remaining_task_allocation_usd": amended["task_budget_usd"]
        - spent["committed_cost_usd"],
        "pending_funding_request_id": (funding.pending(db, row.task_id) or {}).get(
            "audit_id"
        ),
        "stays_paused": True,
        "auto_merge": policy.get("auto_merge", False),
        "landing_hold": controls.landing_held(row)
        or overlay.get("landing_hold", False),
        "nonrenewable_recovery": controls.nonrenewable_recovery(row)
        or overlay.get("nonrenewable_recovery", False),
        "runtime_hard_ceiling_enforced": False,
    }
    result["preview_sha256"] = hashlib.sha256(
        controls._json(result).encode()
    ).hexdigest()
    return result


def preview(receipt_id, overlay, direction):
    with controls._locked_session() as (db, control):
        return _preview(db, control, receipt_id, overlay, direction)


def apply(
    receipt_id, overlay, direction, *, actor, request_key, expected_preview_sha256
):
    request = {
        "receipt_id": receipt_id,
        "overlay": overlay,
        "direction": direction,
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    actor = controls._text(actor, "actor")
    with controls._locked_session() as (db, control):
        for raw in db.exec(
            select(FactoryAudit.detail_json).where(
                FactoryAudit.action == ACTION, FactoryAudit.actor == actor
            )
        ).all():
            previous = json.loads(raw)
            if previous["request"]["request_key"] == request_key:
                if previous["request"] != request:
                    raise ValueError("conflicting_policy_amendment_request")
                return previous["result"]
        proposal = _preview(db, control, receipt_id, overlay, direction)
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("policy_amendment_preview_changed")
        row = db.get(FactoryReceipt, receipt_id)
        task = graph._lock_task(db, row.task_id)
        for hold in ("landing_hold", "nonrenewable_recovery"):
            if not overlay.get(hold):
                continue
            previous_direction = json.loads(row.direction_json or "{}")
            previous_direction.setdefault(
                hold,
                {
                    "actor": actor,
                    "request_key": request_key,
                    "reason": direction,
                },
            )
            row.direction_json = controls._json(previous_direction)
        policy = {**proposal["previous_policy"], **proposal["policy_overlay"]}
        # Preserve any earlier funding overlay when writing its next revision.
        existing = funding.amendment(db, row.task_id)
        combined = {
            **(existing or {}).get("policy_overlay", {}),
            "task_budget_usd": policy["task_budget_usd"],
            **proposal["policy_overlay"],
        }
        created = (
            task.created_at.replace(tzinfo=timezone.utc)
            if task.created_at.tzinfo is None
            else task.created_at
        )
        task_deadline = created + timedelta(seconds=policy["task_timeout_seconds"])
        if proposal["pending_funding_request_id"] is not None:
            controls._audit(
                db,
                actor,
                "funding_review_settled",
                task_id=row.task_id,
                request_id=proposal["pending_funding_request_id"],
                outcome="superseded_by_operator_amendment",
            )
        controls._audit(
            db,
            actor,
            "funding_granted",
            task_id=row.task_id,
            grant_kind="exact_operator_amendment",
            source_receipt_id=row.id,
            policy_overlay=combined,
            reason="Operator applied the reviewed exact-receipt policy preview.",
            next_plan=direction,
            deadline_at=task_deadline.isoformat(),
            review_due_at=task_deadline.isoformat(),
            objective=proposal["objective"],
        )
        # Graph budgeting already consumes funding_granted. Keep the task's
        # displayed envelope coherent without changing any existing node pin.
        task.budget_usd = policy["task_budget_usd"]
        control.version += 1
        control.updated_at = controls._now()
        control.actor = actor
        result = {
            "ok": True,
            "receipt_id": row.id,
            "task_id": row.task_id,
            "version": control.version,
            "stays_paused": True,
            "preview_sha256": proposal["preview_sha256"],
        }
        controls._audit(
            db, actor, ACTION, task_id=row.task_id, request=request, result=result
        )
        return result
