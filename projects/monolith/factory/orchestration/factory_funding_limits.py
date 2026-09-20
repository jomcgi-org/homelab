"""Funding authority shared by the conductor and the pruned executor."""

from datetime import datetime
import json
import os
from sqlalchemy import or_
from sqlmodel import select
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryReceipt,
    FactoryStart,
)

OBJECTIVE_CEILING_USD = 200.0
REVIEW_COST_USD = 1.0


def enabled():
    return os.getenv("FACTORY_CONDUCTOR_FUNDING_ENABLED", "false").lower() == "true"


def latest(db, task_id, action):
    row = db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.task_id == task_id, FactoryAudit.action == action)
        .order_by(FactoryAudit.id.desc())
    ).first()
    return None if row is None else {**json.loads(row.detail_json), "audit_id": row.id}


def pending(db, task_id):
    request = latest(db, task_id, "funding_review_requested")
    if request is None:
        return None
    settled = latest(db, task_id, "funding_review_settled")
    return (
        request
        if settled is None or settled["request_id"] != request["audit_id"]
        else None
    )


def review_authority(db, task_id, start_key):
    request = pending(db, task_id)
    if (
        request
        and request["start_key"] == start_key
        and controls._now() < datetime.fromisoformat(request["deadline_at"])
    ):
        return request
    return None


def amendment(db, task_id):
    return latest(db, task_id, "funding_granted")


def effective_policy(db, row):
    policy = json.loads(row.policy_json)
    grant = amendment(db, row.task_id)
    return {**policy, **grant["policy_overlay"]} if grant else policy


def objective_for_receipt(db, receipt):
    """Return cumulative accounting for the objective owning ``receipt``.

    The audit's task -> receipt association never changes; repo/issue is the
    objective identity across generations and advisory/delivery classes.
    Display-only predecessor lists are deliberately not used as the ledger.
    """
    # A receipt with NULL work_item_id sums only by issue on purpose
    # (pre-column history), so the asymmetry is documented.
    issue_identity = (FactoryReceipt.repo == receipt.repo) & (
        FactoryReceipt.issue_number == receipt.issue_number
    )
    identity = issue_identity
    if receipt.work_item_id is not None:
        identity = or_(
            FactoryReceipt.work_item_id == receipt.work_item_id, issue_identity
        )
    receipts = db.exec(select(FactoryReceipt).where(identity)).all()
    receipt_ids = {r.id for r in receipts}
    ids = {r.task_id for r in receipts if r.task_id}
    for audit in db.exec(
        select(FactoryAudit).where(FactoryAudit.action == "admit_next")
    ).all():
        if json.loads(audit.detail_json).get("receipt_id") in receipt_ids:
            if not audit.task_id:
                raise ValueError("funding history missing task identity")
            ids.add(audit.task_id)
    starts = db.exec(select(FactoryStart).where(FactoryStart.task_id.in_(ids))).all()
    budget = controls._accounting(starts)
    return {
        "repo": receipt.repo,
        "issue_number": receipt.issue_number,
        "task_ids": sorted(ids),
        "committed_cost_usd": budget["committed_cost_usd"],
        "ceiling_usd": OBJECTIVE_CEILING_USD,
    }


def objective(db, task_id):
    """Read one task's objective identity before summing its durable ledger."""
    receipt = controls._receipt(db, task_id)
    if receipt is None:
        raise ValueError("unknown funding objective")
    return objective_for_receipt(db, receipt)


def graph_budget(db, task, *, node_key=None, model=None, cost=None):
    """Only the recorded oversight node can use objective-paid headroom."""
    grant = amendment(db, task.id)
    ceiling = grant["policy_overlay"]["task_budget_usd"] if grant else task.budget_usd
    request = pending(db, task.id)
    if (
        request
        and node_key == request["node_key"]
        and model == "astra"
        and cost == REVIEW_COST_USD
        and controls._now() < datetime.fromisoformat(request["deadline_at"])
    ):
        return OBJECTIVE_CEILING_USD
    return ceiling


def oversight_node(db, task_id, node_key, model, cost):
    request = pending(db, task_id)
    return bool(
        request
        and request["node_key"] == node_key
        and model == "astra"
        and cost == REVIEW_COST_USD
        and review_authority(db, task_id, request["start_key"])
    )


def dispatch_prompt(db, task_id, node_key, prompt):
    grant = amendment(db, task_id)
    if grant and not node_key.startswith("conductor_funding_"):
        return (
            prompt
            + "\nAstra conductor direction for this allocation, within the original objective and review requirements:\n"
            + grant["next_plan"]
        )
    return prompt
