"""Pause funding that would renew a task without new work evidence.

Planner and funding turns alone never justify another automatic funding
tranche. Spend no longer exhausts a node (#7161), so the only blocker left is a
renewal requested with the same work evidence as the last one.
"""

import json

from core.jsonio import dumps
from factory.orchestration import factory_controls as controls


def progress(runs, through_run_id=None):
    """Hash work evidence, never attempt bookkeeping or accounting corrections."""
    from factory.orchestration import graph

    evidence = set()
    for run in runs:
        if (
            run["node_key"].startswith("conductor_")
            or run["status"] not in graph.TERMINAL_RUN_STATUSES
            or (through_run_id is not None and run["id"] > through_run_id)
        ):
            continue
        outcome = json.loads(run.get("outcome_json") or "{}")
        if graph.settled_zero_basis(outcome) or run.get("capacity_denied"):
            continue
        artifact = outcome.get("artifact")
        value = outcome.get("value")
        if isinstance(artifact, dict) and "status" in artifact:
            value = artifact.get("value") if artifact["status"] == "ok" else None
        elif value is None:
            value = artifact
        if isinstance(value, dict) and value:
            evidence.add(controls.digest({"artifact": value}))
        head = run.get("head_sha")
        if head and head != run.get("base_sha"):
            evidence.add(controls.digest({"head_sha": head}))
    return controls.digest(sorted(evidence))


def funding_blocker(db, task_id, runs):
    from factory.orchestration.factory_funding_limits import amendment

    prior = amendment(db, task_id)
    if prior and prior.get("grant_kind") != "exact_operator_amendment":
        previous = prior.get("progress_sha256")
        if previous is None and prior.get("source_run_id") is not None:
            previous = progress(runs, prior["source_run_id"])
        if previous == progress(runs):
            return {"code": "funding_without_progress", "grant_id": prior["audit_id"]}
    return None


def pause(db, task_id, blocker):
    from factory.orchestration import factory_pause

    row = controls._receipt(db, task_id)
    actor = "factory:allocation-recovery"
    if row is not None and row.state in controls._ACTIVE:
        controls.revoke_capacity_release(db, row, actor, "pause_task")
    if factory_pause.pause_locked(db, row, actor, dumps(blocker)):
        controls._audit(
            db, actor, "allocation_recovery_required", task_id=task_id, **blocker
        )
