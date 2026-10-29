"""Reconcile an owned PR merged by another actor without paying a new planner."""

from __future__ import annotations

import json
import logging
import re

import httpx
from sqlmodel import select

from core.env import flag

logger = logging.getLogger(__name__)
_discovery_cursors: dict[str, int] = {}
_REVIEW = re.compile(r"review_merged_([1-9][0-9]*)_([0-9a-f]{40})")
_STRANDED = ("merged_review_missing", "merged_review_not_approved")


def enabled() -> bool:
    return flag("FACTORY_MERGED_VERIFY_HANDOFF_ENABLED")


def review_target(key: str) -> tuple[int, str] | None:
    match = _REVIEW.fullmatch(key)
    return (int(match[1]), match[2]) if match else None


def authenticated_review(task_id: str, run: dict) -> bool:
    """Authenticate the immutable dispatch pin against both execution ledgers."""
    from factory.execution.reconciliation import _factory_owner
    from factory.orchestration import factory_controls as controls

    pin = run.get("pin") or {}
    with controls._read_session() as db:
        start = next(
            (s for s in controls._starts(db, task_id) if s.start_key == run["dispatch_key"]),
            None,
        )
        if not (
            start
            and start.status == "succeeded"
            and start.session_id == run.get("session_id")
            and start.model == pin.get("model")
            and pin.get("task_id") == task_id
            and pin.get("workflow_id") == run["dispatch_key"]
        ):
            return False
        try:
            return _factory_owner(db, pin, run["session_id"]) is not None
        except (KeyError, ValueError):
            return False


def _authority(db, control, row, task_id: str) -> bool:
    """Mutable local authority, read again after every external observation."""
    from factory.orchestration import factory_controls as controls

    if (
        not enabled()
        or control.state not in ("enabled", "paused")
        or row is None
        or row.state not in ("admitted", "escalated")
        or row.cancellation_requested
        or controls.landing_held(row)
        or controls.review_recovery_held(task_id, session=db)
        or controls._cancellation_evidence(db, row) is not None
        or row.escalation_json  # An unanswered human decision owns its fence.
    ):
        return False
    if row.state == "escalated":
        event = db.exec(
            select(controls.FactoryAudit)
            .where(
                controls.FactoryAudit.task_id == task_id,
                controls.FactoryAudit.action == "finish_task",
            )
            .order_by(controls.FactoryAudit.id.desc())
        ).first()
        if not event or json.loads(event.detail_json).get("evidence", {}).get("state") not in _STRANDED:
            return False
    return True


def _owned_issue(task: dict) -> bool:
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_intake_loop import _label_names

    issue = conductor.github_get(task["repo"], f"issues/{task['issue_number']}")
    return not ({"human", "needs-human"} & set(_label_names(issue)))


def _handoff(task: dict, policy: dict, pr: dict, dbos=None) -> None:
    """Append one read-only review under existing identity, bounds and locks.

    A pause permits recording the handoff. Only the ordinary start owner may
    dispatch it, after pause release and fresh authority/capacity checks.
    """
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_controls as controls, graph
    from factory.orchestration.models import SwarmTask

    number, head = pr["number"], pr["head"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", head) or not _owned_issue(task):
        return
    key = f"review_merged_{number}_{head}"
    snapshot = conductor._decision_snapshot(task["id"], policy)
    existing = next((n for n in snapshot.nodes if n["node_key"] == key), None)
    node = existing or {
        "op": "add_node", "node_key": key, "kind": "work",
        "prompt": (
            f"Independently inspect merged PR #{number} at immutable head {head}. "
            "Read-only review: do not reopen implementation, modify source, commit, "
            "push, merge, deploy or close issues. Return approve, changes_requested "
            "or escalate with exact PR/head and evidence. Merge, PR prose and pause "
            "release are not approval. Preserve outstanding operational acceptance."
        ),
        "model": policy.get("reviewer_model", task["conductor_model"]),
        "deps": [], "max_cost_usd": policy["turn_budget_usd"],
        "max_attempts": 1, "side_effects": False,
        "turn_timeout_seconds": policy["turn_timeout_seconds"],
    }
    projection = conductor.project_decision(
        snapshot.task, snapshot.policy, None, snapshot,
        projected_nodes=snapshot.nodes if existing else snapshot.nodes + [node],
        review_rounds_remaining=0, fan_ins_remaining=0,
    )
    if projection.refusal is not None:
        return
    with controls._locked_session() as (db, control):
        row = controls._receipt(db, task["id"])
        if not _authority(db, control, row, task["id"]):
            return
        live_task = conductor._task(task["id"], session=db)
        if (
            not conductor.delivery_identity_matches(live_task, pr)
            or conductor.delivery_pr_number(live_task) not in (None, number)
            or controls._effective_policy(db, row) != snapshot.policy
            or graph.current_version(task["id"], session=db) != snapshot.revision
            or graph.node_runs(task["id"], session=db) != snapshot.runs
            or controls._quiescence_refusal(db, task["id"])
        ):
            return
        limits = controls._snapshot(db, row)["limits"]
        if limits["deadline_expired"]:
            return
        if not existing:
            result = graph.apply_edits(
                task["id"], author_kind="engine", author=conductor.ACTOR,
                cause_kind="factory_loop", cause_ref=key,
                expected_version=snapshot.revision, edits=[node], session=db,
            )
            if not result.ok:
                return
            controls.record_allowance(
                task["id"], policy, conductor.ACTOR,
                review_rounds_remaining=0, cause=key, session=db,
            )
            controls._audit(
                db, conductor.ACTOR, "merged_verify_handoff", task_id=task["id"],
                pr_number=number, head_sha=head, node_key=key,
            )
        # Recovery retains the receipt, task, allowance, runs and original
        # absolute deadline. It never manufactures capacity or releases pauses.
        if row.state == "escalated" and not row.task_paused:
            from factory.orchestration.factory_models import FactoryReceipt, same_work

            others = db.exec(select(FactoryReceipt).where(
                FactoryReceipt.state.in_(("admitted", "uncertain"))
            )).all()
            if control.state != "enabled" or any(
                other.task_id != task["id"] and same_work(other, row) for other in others
            ) or not controls.execution_capacity_available(db, control, row):
                return
            row.state = "admitted"
            row.updated_at = controls._now()
            owned = db.get(SwarmTask, task["id"])
            owned.settled_at = None
            owned.start_state = "factory"
            owned.start_updated_at = controls._now()


def _drive(task: dict, policy: dict, runs: list[dict], pr: dict) -> None:
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_controls as controls, graph, runtime

    key = f"review_merged_{pr['number']}_{pr['head']['sha']}"
    review_runs = [r for r in runs if r["node_key"] == key]
    if review_runs:
        # No correction/re-review loop on an immutable merged branch. A
        # rejected or ambiguous terminal review stays visible for its owner.
        if any(r["status"] not in graph.TERMINAL_RUN_STATUSES for r in review_runs):
            if not _owned_issue(task):
                return
            dbos = runtime.init_dbos()
            for run in review_runs:
                if run["status"] not in graph.TERMINAL_RUN_STATUSES:
                    conductor._submit_or_reconcile(conductor._task(task["id"]), run, dbos)
        return
    _handoff(task, policy, pr)
    if not _owned_issue(task):
        return
    # reserve_node and the physical start guard recheck authority. Only this
    # node is offered: stale implementation/planning nodes cannot be reopened.
    if not controls.can_start(task["id"])["ok"]:
        return
    fresh = conductor._task(task["id"])
    nodes = [n for n in graph.load_graph(task["id"]) if n["node_key"] == key]
    conductor._dispatch_ready(
        fresh, nodes, graph.node_runs(task["id"]), 1,
        fan_out=False, parallel=1, policy=policy,
    )


def recover_stranded(policy: dict) -> None:
    """Landing's rotating discovery pass, including old terminal escalations."""
    if not enabled():
        return
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_controls as controls, factory_landing
    from factory.orchestration.factory_models import FactoryReceipt, FactoryAudit

    repo = policy["repo"]
    with controls._read_session() as db:
        observed = select(FactoryAudit.task_id).where(FactoryAudit.action == "owned_pr_merged")
        query = select(FactoryReceipt).where(
            FactoryReceipt.repo == repo, FactoryReceipt.state == "escalated",
            FactoryReceipt.task_id.in_(observed),
        ).order_by(FactoryReceipt.id)
        rows = db.exec(query.where(
            FactoryReceipt.id > _discovery_cursors.get(repo, 0)
        ).limit(factory_landing.OBSERVATION_BATCH)).all()
        if not rows:
            rows = db.exec(query.limit(factory_landing.OBSERVATION_BATCH)).all()
        _discovery_cursors[repo] = rows[-1].id if rows else 0
        candidates = [(r.task_id, controls._effective_policy(db, r)) for r in rows]
    for task_id, pinned_policy in candidates:
        try:
            with controls._locked_session() as (db, control):
                if not _authority(db, control, controls._receipt(db, task_id), task_id):
                    continue
            task = conductor._task(task_id)
            if _owned_issue(task):
                reconcile_merged_delivery(task, pinned_policy, conductor.graph.node_runs(task_id))
        except (httpx.HTTPError, ValueError):
            logger.exception("Merged verification discovery failed for %s", task_id)


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
    if number is None:
        number = granted
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
    except (httpx.HTTPError, ValueError) as exc:
        # Once observed, a transient read failure cannot reopen implementation.
        # Nor may unknown merge state on a paused task fall through to generic
        # issue-closed cancellation, which would lose delivery verification.
        if prior is not None:
            conductor._settle_paused_task(task, policy, runs, merge_unknown=True)
            return True
        if receipt and receipt.task_paused:
            if isinstance(exc, ValueError) or (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in (404, 410)
            ):
                # Retrying an absent or unusable PR forever leaks the paused
                # slot. Permit only deadline settlement, not objective-closed
                # cancellation, and do not recurse into another merge read.
                conductor._settle_paused_task(task, policy, runs, merge_unknown=True)
            return True
        return False
    if not pr.get("merged") or not conductor.delivery_identity_matches(task, pr):
        if prior is not None:
            conductor._settle_paused_task(task, policy, runs, merge_unknown=True)
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
        if enabled():
            _drive(task, policy, runs, pr)
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
            if enabled():
                _drive(task, policy, runs, pr)
                return True
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
        # An observed merge must not suppress the paused deadline forever when
        # required checks or delivery evidence cannot verify. Keep both ledgers
        # fenced and allow only deadline escalation, never issue-closed or
        # legacy pause cancellation. The locked helper is inert when unpaused.
        conductor._settle_paused_task(task, policy, runs, merge_unknown=True)
        return True
    controls.finish_task(task["id"], "succeeded", conductor.ACTOR, evidence=evidence)
    return True
