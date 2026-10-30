"""Exact, paused-receipt operator amendments through existing funding authority.

No resume, graph replacement, retry, or budget renewal is implicit. Preview is
read-only; application requires the same control version and graph revision.
Dollar amounts are recorded but never refuse an amendment (#7161).
"""

from datetime import timedelta, timezone
import hashlib
import json

from sqlmodel import select

from core.jsonio import dumps
from factory.execution.models import AgentSession, PendingMessage
from factory.orchestration import factory_controls as controls, graph
from factory.orchestration import factory_funding_limits as funding
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt
from factory.orchestration.models import SwarmNodeRun, SwarmPlanNode

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


def _model_restriction_preview(
    db, control, row, task, policy, removed, direction, starts, runs
):
    if (
        not isinstance(removed, list)
        or not 1 <= len(removed) <= 16
        or any(not isinstance(m, str) or m != m.strip() or not m for m in removed)
        or len(set(removed)) != len(removed)
        or not set(removed) <= set(policy["allowed_models"])
    ):
        raise ValueError("model_restriction_requires_existing_models")
    if (
        controls._quiescence_refusal(db, row.task_id)
        or db.exec(
            select(PendingMessage.id)
            .join(AgentSession)
            .where(
                AgentSession.local_session_id.startswith(
                    f"factory:{row.task_id}:", autoescape=True
                ),
            )
        ).first()
        is not None
    ):
        raise ValueError("amendment_unresolved_execution")
    nodes = db.exec(
        select(SwarmPlanNode)
        .where(SwarmPlanNode.task_id == row.task_id)
        .order_by(SwarmPlanNode.id)
    ).all()
    pending = funding.pending(db, row.task_id)
    used = {
        policy.get("conductor_model"),
        policy.get("worker_model"),
        policy.get("reviewer_model"),
        policy.get("backlog_refill", {}).get("model"),
        task.conductor_model,
        task.start_model,
        *(node["model"] for node in graph.load_graph(row.task_id, session=db)),
    }
    if policy.get("backlog_refill", {}).get("enabled"):
        from factory.orchestration.backlog_refill_policy import normalize

        used.add(normalize(policy)["model"])
    if pending:
        used.add(funding.review_model(pending))
    if set(removed) & used:
        raise ValueError("model_restriction_model_in_use")
    # Validate the restriction itself. Full policy normalization sorts the
    # allowlist and fills defaults, which would change unrelated pinned fields.
    amended = funding.restrict_models(policy, removed)
    if not amended["allowed_models"] or any(
        not pool for pool in amended.get("model_pools", {}).values()
    ):
        raise ValueError("model_restriction_empty_pool")
    prior = json.loads(row.direction_json or "{}").get("model_restriction", {})
    result = {
        "receipt_id": row.id,
        "task_id": row.task_id,
        "expected_version": control.version,
        "control_state": control.state,
        "expected_revision": graph.current_version(row.task_id, session=db),
        "restriction_only": True,
        "removed_models": sorted(set(removed) | set(prior.get("removed_models", []))),
        "previous_policy": policy,
        "policy_overlay": {
            key: amended[key]
            for key in ("allowed_models", "model_pools")
            if amended.get(key) != policy.get(key)
        },
        "direction": direction,
        "stays_paused": True,
        "accounting": controls._accounting(starts),
        "pending_funding_request": pending,
        "authority_binding": {
            action: funding.latest(db, row.task_id, action)
            for action in (
                "funding_granted",
                controls.TASK_RECOVERY_ACTION,
                "progress_continuation_authorized",
                "progress_continuation_lease",
                "landing_recovery_requested",
                "landing_recovery_started",
            )
        },
        "state_sha256": hashlib.sha256(
            dumps(
                {
                    "receipt": row.model_dump(mode="json"),
                    "task": task.model_dump(mode="json"),
                    "nodes": [node.model_dump(mode="json") for node in nodes],
                    "starts": [
                        start.model_dump(mode="json")
                        for start in sorted(starts, key=lambda r: r.id)
                    ],
                    "runs": [
                        run.model_dump(mode="json")
                        for run in sorted(runs, key=lambda r: r.id)
                    ],
                }
            ).encode()
        ).hexdigest(),
    }
    result["preview_sha256"] = hashlib.sha256(dumps(result).encode()).hexdigest()
    return result


def _preview(db, control, receipt_id, overlay, direction):
    if (
        not isinstance(overlay, dict)
        or not overlay
        or not set(overlay)
        <= FIELDS | {"landing_hold", "nonrenewable_recovery", "remove_models"}
        or ("remove_models" in overlay and set(overlay) != {"remove_models"})
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
    hold_only = overlay == {"landing_hold": True}
    row = db.get(FactoryReceipt, receipt_id)
    if (
        row is None
        or row.state not in controls._ACTIVE
        or (not row.task_paused and not hold_only)
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
    if "remove_models" in overlay:
        return _model_restriction_preview(
            db,
            control,
            row,
            task,
            policy,
            overlay["remove_models"],
            direction,
            starts,
            runs,
        )
    amended = controls.validate_policy(
        {**policy, **{k: v for k, v in overlay.items() if k in FIELDS}}
    )
    created = (
        task.created_at.replace(tzinfo=timezone.utc)
        if task.created_at.tzinfo is None
        else task.created_at
    )
    deadline = created + timedelta(seconds=amended["task_timeout_seconds"])
    recovery_binding = None
    recovery = controls.task_recovery_grant(row.task_id, session=db)
    if recovery is not None and not hold_only:
        # Recovery owns an absolute deadline and a separate work ceiling.
        # A policy amendment must neither renew nor replace that authority.
        if amended["task_timeout_seconds"] != policy["task_timeout_seconds"]:
            raise ValueError("amendment_recovery_deadline_immutable")
        deadline = controls._recovery_deadline(db, row.task_id)
        existing = funding.amendment(db, row.task_id)
        if existing is None:
            raise ValueError("amendment_recovery_funding_missing")
        recovery_binding = {
            "grant": funding.latest(db, row.task_id, controls.TASK_RECOVERY_ACTION),
            "funding": existing,
            "deadline_at": deadline.isoformat(),
        }
    if not hold_only and deadline <= controls._now():
        raise ValueError("amendment_task_deadline_expired")
    objective = funding.objective_for_receipt(db, row)
    spent = controls._accounting(starts)
    revision = graph.current_version(row.task_id, session=db)
    result = {
        "receipt_id": row.id,
        "task_id": row.task_id,
        "expected_version": control.version,
        "expected_revision": revision,
        "previous_policy": policy,
        "hold_only": hold_only,
        "recovery_binding": recovery_binding,
        "policy_overlay": {}
        if hold_only
        else {
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
        "stays_paused": row.task_paused,
        "auto_merge": policy.get("auto_merge", False),
        "landing_hold": controls.landing_held(row)
        or overlay.get("landing_hold", False),
        "nonrenewable_recovery": controls.nonrenewable_recovery(row)
        or overlay.get("nonrenewable_recovery", False),
        "runtime_hard_ceiling_enforced": False,
    }
    result["preview_sha256"] = hashlib.sha256(dumps(result).encode()).hexdigest()
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
            row.direction_json = dumps(previous_direction)
        # Holds and model restrictions grant no funding or execution authority.
        # Never refresh an expired lease while recording either restriction.
        if proposal.get("restriction_only"):
            previous_direction = json.loads(row.direction_json or "{}")
            previous_direction["model_restriction"] = {
                "removed_models": proposal["removed_models"],
                "actor": actor,
                "request_key": request_key,
                "reason": direction,
            }
            row.direction_json = dumps(previous_direction)
        elif not proposal["hold_only"]:
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
            binding = proposal["recovery_binding"]
            deadline_at = (
                binding["funding"]["deadline_at"]
                if binding
                else task_deadline.isoformat()
            )
            review_due_at = (
                binding["funding"]["review_due_at"] if binding else deadline_at
            )
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
                deadline_at=deadline_at,
                review_due_at=review_due_at,
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
            "stays_paused": row.task_paused,
            "preview_sha256": proposal["preview_sha256"],
        }
        controls._audit(
            db,
            actor,
            ACTION,
            task_id=row.task_id,
            request=request,
            result=result,
            **({"proposal": proposal} if proposal.get("restriction_only") else {}),
        )
        return result
