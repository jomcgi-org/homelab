"""Distinguish an exhausted node allocation from a renewable task envelope.

An operator can authorize one replacement plan through a paused-receipt
amendment. Its predecessor charges and pins remain immutable. Planner and
funding turns alone never justify another automatic funding tranche.
"""

import hashlib
import json

from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit

AUTHORIZED = "node_allocation_recovery_authorized"
APPLIED = "node_allocation_recovery_applied"


def digest(value):
    return hashlib.sha256(controls._json(value).encode()).hexdigest()


def progress(runs, through_run_id=None):
    """Include useful investigation and recovered evidence, excluding oversight."""
    return digest(
        [
            {
                key: run.get(key)
                for key in (
                    "id",
                    "node_key",
                    "status",
                    "accounted_cost_usd",
                    "head_sha",
                    "outcome_json",
                )
            }
            for run in sorted(runs, key=lambda item: item["id"])
            if not run["node_key"].startswith("conductor_")
            and (through_run_id is None or run["id"] <= through_run_id)
        ]
    )


def blockers(db, task_id):
    from factory.orchestration import graph
    from factory.orchestration.models import SwarmPlanNode

    if controls._receipt(db, task_id) is None:
        return []
    covered = set()
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.task_id == task_id, FactoryAudit.action == APPLIED
        )
    ).all():
        covered.update(json.loads(raw)["blockers"])
    runs = graph._runs(db, task_id)
    budgets = graph._node_budgets(db, task_id)
    result = []
    # Hidden nodes are intentional: discarding or renaming cannot erase a cap.
    for node in db.exec(
        select(SwarmPlanNode).where(SwarmPlanNode.task_id == task_id)
    ).all():
        attempts = [run for run in runs if run.node_key == node.node_key]
        if (
            node.node_key.startswith("conductor_")
            or not attempts
            or any(
                run.status == "succeeded"
                or run.status not in graph.TERMINAL_RUN_STATUSES
                for run in attempts
            )
        ):
            continue
        spent = sum(graph._accounted_cost(run, budgets) for run in attempts)
        if spent < node.max_cost_usd:
            continue
        item = {
            "node_key": node.node_key,
            "max_cost_usd": node.max_cost_usd,
            "accounted_cost_usd": spent,
            "run_ids": [run.id for run in attempts],
        }
        item["fingerprint"] = digest(item)
        if item["fingerprint"] not in covered:
            result.append(item)
    return sorted(result, key=lambda item: item["node_key"])


def authorization(db, task_id, blocked):
    from factory.orchestration.factory_funding_limits import latest

    grant = latest(db, task_id, AUTHORIZED)
    if not grant or not controls.nonrenewable_recovery(controls._receipt(db, task_id)):
        return None
    if grant["blockers"] != [item["fingerprint"] for item in blocked]:
        return None
    used = latest(db, task_id, APPLIED)
    return grant if not used or used["authorization_id"] != grant["audit_id"] else None


def consume(db, task_id, grant, edits):
    controls._audit(
        db,
        "factory:graph",
        APPLIED,
        task_id=task_id,
        authorization_id=grant["audit_id"],
        blockers=grant["blockers"],
        replacements=[edit["node_key"] for edit in edits if edit["op"] == "add_node"],
    )


def funding_blocker(db, task_id, runs):
    from factory.orchestration.factory_funding_limits import amendment

    exhausted = blockers(db, task_id)
    if exhausted:
        return {"code": "node_allocation_exhausted", "nodes": exhausted}
    prior = amendment(db, task_id)
    if prior and prior.get("grant_kind") != "exact_operator_amendment":
        previous = prior.get("progress_sha256")
        if previous is None and prior.get("source_run_id") is not None:
            previous = progress(runs, prior["source_run_id"])
        if previous == progress(runs):
            return {"code": "funding_without_progress", "grant_id": prior["audit_id"]}
    return None


def pause(db, task_id, blocker):
    row = controls._receipt(db, task_id)
    if row is not None and row.state in controls._ACTIVE and not row.task_paused:
        actor = "factory:allocation-recovery"
        controls.set_control("pause_task", actor, task_id=task_id, session=db)
        controls._audit(
            db, actor, "allocation_recovery_required", task_id=task_id, **blocker
        )
