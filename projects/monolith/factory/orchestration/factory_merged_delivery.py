"""Reconcile an owned PR merged by another actor without paying a new planner."""

from __future__ import annotations

import json

import httpx
from sqlmodel import select


def reconcile_merged_delivery(task: dict, policy: dict, runs: list[dict]) -> bool:
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_controls as controls
    from factory.orchestration import factory_landing as landing
    from factory.orchestration import factory_refine, graph
    from factory.orchestration.factory_models import FactoryAudit

    number = conductor._latest_pr(
        [
            run
            for run in runs
            if conductor._is_implementation(run["node_key"])
            and conductor._artifact(run).get("pr_number")
        ]
    )
    granted = conductor.delivery_pr_number(task)
    if number is None or (granted is not None and granted != number):
        return False

    # An observed merge cannot remove an explicit operator or landing hold.
    with controls._read_session() as db:
        receipt = controls._receipt(db, task["id"])
        if receipt and (
            receipt.cancellation_requested or controls.landing_held(receipt)
        ):
            return False
    if task.get("landing_hold"):
        return False
    if controls.review_recovery_held(task["id"]):
        return False
    with controls._read_session() as db:
        observed = db.exec(
            select(FactoryAudit).where(
                FactoryAudit.task_id == task["id"],
                FactoryAudit.action == "owned_pr_merged",
            )
        ).first()
        prior = json.loads(observed.detail_json) if observed else None
    try:
        pr = conductor.github_get(task["repo"], f"pulls/{number}")
    except (httpx.HTTPError, ValueError):
        # Once observed, a transient read failure cannot reopen implementation.
        # Nor may unknown merge state on a paused task fall through to generic
        # issue-closed cancellation, which would lose delivery verification.
        return prior is not None or bool(receipt and receipt.task_paused)
    if not pr.get("merged") or not conductor.delivery_identity_matches(task, pr):
        return prior is not None
    landing._record(
        task["id"],
        "owned_pr_merged",
        pr_number=number,
        head_sha=pr["head"]["sha"],
        merge_commit_sha=pr.get("merge_commit_sha"),
        armed_by_factory=False,
    )
    if any(run["status"] not in graph.TERMINAL_RUN_STATUSES for run in runs):
        return True
    try:
        evidence = conductor.verify_delivery(
            task,
            number,
            runs,
            conductor.pool_for("reviewer", policy),
            judgment=factory_refine.task_class_for(task["id"])
            in conductor.JUDGMENT_CLASSES,
            issue_number=task.get("issue_number"),
            allow_merged=True,
        )
    except (httpx.HTTPError, ValueError) as exc:
        landing._record(task["id"], "merged_delivery_waiting", reason=str(exc)[:600])
        reviews = [run for run in runs if run["node_key"].startswith("review_")]
        if isinstance(exc, conductor.IndependentReviewMissing):
            controls.finish_task(
                task["id"],
                "escalated",
                conductor.ACTOR,
                evidence={
                    "state": "merged_review_not_approved"
                    if reviews
                    else "merged_review_missing",
                    "reason": "Owned PR merged outside the factory without an eligible exact-head independent approval; operator follow-up required.",
                },
            )
            return True
        return True
    controls.finish_task(task["id"], "succeeded", conductor.ACTOR, evidence=evidence)
    return True
