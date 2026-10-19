"""One explicit, bounded recovery of an expired task, retaining its ledger."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlmodel import select

from factory.execution.models import AgentSession, PendingMessage
from factory.orchestration import factory_controls as controls, graph
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryReceipt,
    WorkItem,
    WorkItemEdge,
    same_work,
)

ACTION = controls.TASK_RECOVERY_ACTION
MAX_RECOVERY_SECONDS = 6 * 3600


def grant(db, task_id):
    return controls.task_recovery_grant(task_id, session=db)


def _issue(receipt_id):
    from factory.orchestration.factory_conductor import github_get

    with controls._read_session() as db:
        row = db.get(FactoryReceipt, receipt_id)
        if row is None:
            raise ValueError("unknown_receipt")
        repo, number = row.repo, row.issue_number
    return github_get(repo, f"issues/{number}")


def _capacity(db, control, row):
    from factory.orchestration.factory_intake import lane_of

    policy = json.loads(control.policy_json)
    if (
        control.state != "enabled"
        or row.generation != policy.get("generation", 0)
        or row.repo not in controls.enabled_repos(policy)
        or not controls.admission_eligible(policy, row.issue_number, row.actor)
    ):
        raise ValueError("recovery_admission_disabled")
    owned = db.exec(
        select(FactoryReceipt).where(
            FactoryReceipt.state.in_((*controls._ACTIVE, "landing"))
        )
    ).all()
    if any(same_work(other, row) for other in owned):
        raise ValueError("recovery_work_owned_elsewhere")
    active = [other for other in owned if other.state in controls._ACTIVE]
    unresolved = controls.unresolved_start_task_ids(db)
    occupying = controls.execution_slot_holders(
        policy, active, unresolved_task_ids=unresolved
    )
    lane = lane_of(row)
    if (
        not controls.execution_capacity_available(
            db, control, row, active=active, unresolved=unresolved
        )
        or sum(other.repo == row.repo and lane_of(other) == lane for other in occupying)
        >= controls.repo_lane_limits(policy, row.repo)[lane]
    ):
        raise ValueError("recovery_capacity_unavailable")
    if row.work_item_id:
        item = db.get(WorkItem, row.work_item_id)
        if item and (
            item.state == "closed" or "human" in {s.lower() for s in item.labels}
        ):
            raise ValueError("recovery_work_item_unavailable")
        blocker = db.exec(
            select(WorkItemEdge.id)
            .join(WorkItem, WorkItem.id == WorkItemEdge.from_id)
            .where(
                WorkItemEdge.to_id == row.work_item_id,
                WorkItemEdge.kind == "blocks",
                WorkItem.state != "closed",
            )
        ).first()
        if blocker is not None:
            raise ValueError("recovery_work_blocked")


def _preview(db, control, receipt_id, deadline_at, max_work_turns, direction, issue):
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_decisions import decision_in_flight

    direction = controls._text(direction, "direction", 4000)
    deadline = datetime.fromisoformat(deadline_at)
    if deadline.tzinfo is None or not (
        controls._now()
        < deadline
        <= controls._now() + timedelta(seconds=MAX_RECOVERY_SECONDS)
    ):
        raise ValueError("recovery_deadline_outside_window")
    row = db.get(FactoryReceipt, receipt_id)
    if (
        row is None
        or row.state != "escalated"
        or not row.task_id
        or row.cancellation_requested
    ):
        raise ValueError("recovery_requires_escalated_task")
    task = graph._lock_task(db, row.task_id)
    snapshot = controls._snapshot(db, row)
    if task.settled_at is None or not snapshot["limits"]["deadline_expired"]:
        raise ValueError("recovery_requires_expired_settlement")
    if grant(db, row.task_id) or controls.review_recovery_held(row.task_id, session=db):
        raise ValueError("recovery_already_authorized")
    landing_deadline = controls._landing_recovery_deadline(db, row.task_id)
    if landing_deadline is not None and deadline > landing_deadline:
        raise ValueError("recovery_exceeds_landing_deadline")
    refusal = controls._quiescence_refusal(db, row.task_id)
    if refusal:
        raise ValueError("recovery_" + refusal)
    # Any queued message can become claimed after this transaction. Recovery
    # therefore fences unclaimed messages too, not only executing sessions.
    if (
        db.exec(
            select(PendingMessage.id)
            .join(AgentSession)
            .where(
                AgentSession.local_session_id.startswith(
                    f"factory:{row.task_id}:", autoescape=True
                )
            )
        ).first()
        is not None
    ):
        raise ValueError("recovery_pending_execution")
    if decision_in_flight(db, row.id):
        raise ValueError("recovery_decision_in_flight")
    labels = {label["name"].lower() for label in issue.get("labels", [])}
    if (
        issue.get("number") != row.issue_number
        or issue.get("html_url")
        != f"https://github.com/{row.repo}/issues/{row.issue_number}"
        or issue.get("state") != "open"
        or "human" in labels
        or "pull_request" in issue
    ):
        raise ValueError("recovery_issue_unavailable")
    _capacity(db, control, row)
    stored_direction = json.loads(row.direction_json or "{}")
    identity = {**stored_direction, "id": row.task_id}
    if controls.delivery_branch_owner(
        db, row.repo, conductor.delivery_branch(identity), exclude_receipt_id=row.id
    ):
        raise ValueError("recovery_branch_owned_elsewhere")
    policy = snapshot["policy"]
    if (
        type(max_work_turns) is not int
        or not snapshot["turns_used"]
        < max_work_turns
        <= controls.task_turn_ceiling(policy)
        or snapshot["planner_turns_used"] + 2 > controls.planner_turn_cap(policy)
    ):
        raise ValueError("recovery_turn_bounds_unavailable")
    runs = graph.node_runs(row.task_id, session=db)
    nodes = graph.load_graph(row.task_id, session=db)
    proposal = {
        "receipt_id": row.id,
        "task_id": row.task_id,
        "expected_version": control.version,
        "expected_revision": graph.current_version(row.task_id, session=db),
        "receipt_updated_at": row.updated_at.isoformat(),
        "receipt_body_sha256": hashlib.sha256(row.body.encode()).hexdigest(),
        "issue_sha256": hashlib.sha256(controls._json(issue).encode()).hexdigest(),
        "policy": policy,
        "prior_direction": stored_direction,
        "prior_escalation": json.loads(row.escalation_json or "{}"),
        "prior_settled_at": task.settled_at.isoformat(),
        "prior_paused": row.task_paused,
        "accounting": controls._accounting(controls._starts(db, row.task_id)),
        "deadline_at": deadline.astimezone(timezone.utc).isoformat(),
        "work_turn_ceiling": max_work_turns,
        # One replacement plan and one final acceptance, each with one attempt.
        "planner_turn_ceiling": snapshot["planner_turns_used"] + 2,
        "run_id_floor": max((run["id"] for run in runs), default=0),
        "planner_node_key": f"conductor_{sum(n['node_key'].startswith('conductor_') for n in nodes) + 1}",
        "direction": direction,
        "stays_paused": True,
    }
    proposal["preview_sha256"] = hashlib.sha256(
        controls._json(proposal).encode()
    ).hexdigest()
    return proposal


def preview(receipt_id, deadline_at, max_work_turns, direction):
    issue = _issue(receipt_id)
    with controls._locked_session() as (db, control):
        return _preview(
            db, control, receipt_id, deadline_at, max_work_turns, direction, issue
        )


def _replay(db, actor, request):
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.action == ACTION, FactoryAudit.actor == actor
        )
    ).all():
        previous = json.loads(raw)
        if previous["request"]["request_key"] == request["request_key"]:
            if previous["request"] != request:
                raise ValueError("conflicting_task_recovery_request")
            return previous["result"]
    return None


def apply(
    receipt_id,
    deadline_at,
    max_work_turns,
    direction,
    *,
    actor,
    request_key,
    expected_preview_sha256,
):
    from factory.orchestration import factory_funding_limits as funding

    actor = controls._text(actor, "actor")
    request = {
        "receipt_id": receipt_id,
        "deadline_at": deadline_at,
        "max_work_turns": max_work_turns,
        "direction": direction,
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    with controls._read_session() as db:
        replay = _replay(db, actor, request)
        if replay is not None:
            return replay
    issue = _issue(receipt_id)
    with controls._locked_session() as (db, control):
        replay = _replay(db, actor, request)
        if replay is not None:
            return replay
        proposal = _preview(
            db, control, receipt_id, deadline_at, max_work_turns, direction, issue
        )
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("task_recovery_preview_changed")
        row = db.get(FactoryReceipt, receipt_id)
        task = graph._lock_task(db, row.task_id)
        authority = {"actor": actor, "request_key": request_key, "reason": direction}
        stored_direction = proposal["prior_direction"].copy()
        for hold in ("landing_hold", "nonrenewable_recovery"):
            stored_direction.setdefault(hold, authority)
        stored_direction["task_recovery"] = authority
        row.direction_json = controls._json(stored_direction)
        escalation = proposal["prior_escalation"].copy()
        escalation["resolved"] = {
            **authority,
            "effect": ACTION,
            "at": controls._now().isoformat(),
        }
        row.escalation_json = controls._json(escalation)
        row.state, row.task_paused, row.updated_at = "admitted", True, controls._now()
        # This is a new operator-owned pause. Retaining only an older conductor
        # pause would let its legacy expiry cancel recovery before explicit resume.
        controls._audit(
            db,
            actor,
            "pause_task",
            task_id=row.task_id,
            ok=True,
            reason="Operator recovery awaits explicit resume.",
            recovery_request_key=request_key,
        )
        task.start_state, task.settled_at = "factory", None
        task.start_updated_at = controls._now()
        pending = funding.pending(db, row.task_id)
        if pending:
            controls._audit(
                db,
                actor,
                "funding_review_settled",
                task_id=row.task_id,
                request_id=pending["audit_id"],
                outcome="superseded_by_operator_recovery",
            )
        prior = funding.amendment(db, row.task_id) or {}
        # Reuse the existing policy-overlay and dispatch-direction owner. No
        # dollar field changes, and both cumulative turn caps can only tighten.
        controls._audit(
            db,
            actor,
            "funding_granted",
            task_id=row.task_id,
            grant_kind=ACTION,
            policy_overlay={
                **prior.get("policy_overlay", {}),
                "task_budget_usd": proposal["policy"]["task_budget_usd"],
                "max_task_turns_hard": max_work_turns,
                "max_planner_turns": proposal["planner_turn_ceiling"],
            },
            reason="Operator authorized one bounded recovery on the original task.",
            next_plan=direction,
            deadline_at=proposal["deadline_at"],
            review_due_at=proposal["deadline_at"],
        )
        control.version += 1
        control.actor, control.updated_at = actor, controls._now()
        result = {
            "ok": True,
            "receipt_id": row.id,
            "task_id": row.task_id,
            "version": control.version,
            "stays_paused": True,
            "preview_sha256": proposal["preview_sha256"],
        }
        controls._audit(
            db,
            actor,
            ACTION,
            task_id=row.task_id,
            request=request,
            proposal=proposal,
            result=result,
        )
        return result
