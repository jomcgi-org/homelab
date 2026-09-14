"""One exact correction/review continuation, without resetting a task's limits."""

from datetime import timedelta
import hashlib
import json
import os

from sqlmodel import select

from factory.orchestration import factory_controls as controls, graph
from factory.orchestration.factory_models import FactoryReceipt

ACTOR = "factory:continuation"


def enabled():
    return (
        os.getenv("FACTORY_AUTONOMOUS_CONTINUATION_ENABLED", "false").lower() == "true"
    )


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def _replay(grant, review_run, edits, cause):
    if grant["cause"] != cause or grant["source_review_id"] != review_run["id"]:
        return False, "continuation_exhausted"
    if grant.get("edits_sha256") != _digest(edits):
        return False, "continuation_changed"
    return True, None


def try_grant(
    task, policy, review_run, edits, cause, expected_version, *, recover_escalated=False
):
    """Bind one grant and its two nodes in the same control/graph transaction."""
    from factory.orchestration import factory_conductor as conductor

    if not enabled():
        return False, "disabled"
    existing = controls.continuation_grant(task["id"])
    if existing:
        return _replay(existing, review_run, edits, cause)
    if (
        len(edits) != 2
        or any(e["op"] != "add_node" or e["max_attempts"] != 1 for e in edits)
        or not edits[0]["node_key"].startswith("correct_")
        or not edits[1]["node_key"].startswith("review_")
        or edits[0]["deps"] != [review_run["node_key"]]
        or edits[1]["deps"] != [edits[0]["node_key"]]
    ):
        return False, "continuation_shape_refused"
    observed = conductor._review_recovery_evidence(task, review_run)
    if observed["state"] != "ready":
        return False, "continuation_waiting" if observed[
            "state"
        ] == "waiting" else "continuation_evidence_refused"
    observed_at = controls._now()
    with controls._locked_session() as (db, control):
        row = controls._receipt(db, task["id"])
        if row is None:
            return False, "continuation_control_refused"
        existing = controls.continuation_grant(task["id"], session=db)
        if existing:
            return _replay(existing, review_run, edits, cause)
        if control.state not in ("enabled", "paused") or row.cancellation_requested:
            return False, "continuation_control_refused"
        if row.state == "escalated" and recover_escalated:
            # Recovery is an explicit internal operation on the reported task,
            # never the old readmission path that discards its policy and history.
            if control.state != "enabled":
                return False, "continuation_control_refused"
            active = db.exec(
                select(FactoryReceipt).where(
                    FactoryReceipt.state.in_(["admitted", "uncertain"])
                )
            ).all()
            from factory.orchestration.factory_intake import open_lanes, lane_of

            current = json.loads(control.policy_json)
            if open_lanes(current, active).get(lane_of(row), 0) < 1:
                return False, "continuation_waiting"
        elif row.state != "admitted" or row.task_paused:
            return False, "continuation_control_refused"
        pinned = json.loads(row.policy_json)
        if (
            pinned != policy
            or graph.current_version(task["id"], session=db) != expected_version
        ):
            return False, "continuation_changed"
        runs = graph.node_runs(task["id"], session=db)
        nodes = graph.load_graph(task["id"], session=db)
        latest = conductor._pending_correction(nodes, runs)
        if latest is None or _digest(latest) != _digest(review_run):
            return False, "continuation_changed"
        starts = controls._starts(db, task["id"])
        budget = controls._accounting(starts)
        if budget["unresolved_starts"] or any(
            r["status"] in ("admitted", "dispatched", "uncertain") for r in runs
        ):
            return False, "continuation_waiting"
        stored_task = graph._lock_task(db, task["id"])
        created = conductor._aware(stored_task.created_at)
        deadline = created + timedelta(seconds=policy["task_timeout_seconds"])
        if (
            controls._now() >= deadline
            or (controls._now() - observed_at).total_seconds() > 30
        ):
            return False, "continuation_deadline"
        projected = conductor._projected_nodes(edits, nodes)
        allowance = controls.allowance_from_graph(
            projected,
            runs,
            policy,
            review_rounds_remaining=0,
            fan_ins_remaining=0,
            graph_revision=expected_version,
        )
        if allowance["turns"] <= controls.task_turn_ceiling(policy):
            return False, "continuation_not_turn_exhausted"
        terminal_keys = {
            r["node_key"] for r in runs if r["status"] in ("succeeded", "escalated")
        }
        if any(
            not n["node_key"].startswith("conductor_")
            and n["node_key"] not in terminal_keys
            and graph.attempts_spent(runs, n["node_key"]) < n["max_attempts"]
            and sum(
                r["accounted_cost_usd"] for r in runs if r["node_key"] == n["node_key"]
            )
            < n["max_cost_usd"]
            for n in nodes
        ):
            # Exclude competing work, including nodes waiting on dependencies.
            # An exhausted node budget cannot dispatch its unused attempt slot.
            return False, "continuation_scope_refused"
        if allowance["usd"] > policy["task_budget_usd"]:
            return False, "continuation_budget"
        cost = sum(e["max_cost_usd"] for e in edits)
        if any(
            e["model"] not in policy["allowed_models"]
            or e["max_cost_usd"] > policy["turn_budget_usd"]
            for e in edits
        ) or budget["committed_cost_usd"] + cost > min(
            policy["task_budget_usd"], stored_task.budget_usd
        ):
            return False, "continuation_budget"
        ceiling = budget["turns_used"] + 2
        if ceiling > controls.task_turn_ceiling(policy) + 2:
            return False, "continuation_turns"
        result = graph.apply_edits(
            task["id"],
            author_kind="engine",
            author=ACTOR,
            cause_kind="factory_loop",
            cause_ref=cause,
            expected_version=expected_version,
            edits=edits,
            session=db,
        )
        if not result.ok:
            return (
                False,
                "continuation_changed"
                if result.refusal_code == "stale_version"
                else "continuation_graph_refused",
            )
        controls._audit(
            db,
            ACTOR,
            "continuation_granted",
            task_id=task["id"],
            cause=cause,
            edits_sha256=_digest(edits),
            original_turn_ceiling=controls.task_turn_ceiling(policy),
            work_turn_ceiling=ceiling,
            starts_before=len(starts),
            graph_revision=result.version,
            source_review_id=review_run["id"],
            head_sha=observed["head_sha"],
            pr_number=observed["pr_number"],
            node_keys=[e["node_key"] for e in edits],
            deadline_at=deadline.isoformat(),
            task_budget_usd=policy["task_budget_usd"],
        )
        if row.state == "escalated":
            stored_task.start_state = "factory"
            stored_task.settled_at = None
            db.add(stored_task)
            row.state = "admitted"
            row.task_paused = False
            document = json.loads(row.escalation_json or "{}")
            document["resolved"] = {
                "actor": ACTOR,
                "effect": "autonomous_continuation",
                "at": controls._now().isoformat(),
            }
            row.escalation_json = json.dumps(document)
            db.add(row)
        controls.record_allowance(
            task["id"],
            policy,
            ACTOR,
            review_rounds_remaining=0,
            cause=cause,
            session=db,
        )
        return True, None


def finish_exhausted(task_id, reason):
    """Keep the PR and findings, release the lane, and do not ask a human to replan."""
    return controls.finish_task(
        task_id,
        "failed",
        ACTOR,
        evidence={"state": "continuation_exhausted", "reason": reason},
    )


def terminal_grant(task, policy, runs):
    """Settle the pair deterministically, even if creation is later disabled."""
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_refine import task_class_for

    task_id = task["id"]
    grant = controls.continuation_grant(task_id)
    if not grant:
        return False
    own = [r for r in runs if r["node_key"] in grant["node_keys"]]
    if any(r["status"] in ("admitted", "dispatched", "uncertain") for r in own):
        return False
    failed = any(
        r["status"] in ("failed", "escalated")
        and graph.attempts_spent(own, r["node_key"]) >= 1
        and not any(
            s["node_key"] == r["node_key"] and s["status"] == "succeeded" for s in own
        )
        for r in own
    )
    review = next(
        (
            r
            for r in reversed(own)
            if r["node_key"] == grant["node_keys"][1] and r["status"] == "succeeded"
        ),
        None,
    )
    if failed or (review and conductor._artifact(review).get("verdict") != "approve"):
        finish_exhausted(
            task_id,
            "The one correction/review continuation did not produce approval; existing PR and evidence retained.",
        )
        return True
    if review:
        try:
            evidence = conductor.verify_delivery(
                task,
                grant["pr_number"],
                runs,
                conductor.pool_for("reviewer", policy),
                judgment=task_class_for(task_id) in conductor.JUDGMENT_CLASSES,
                issue_number=task.get("issue_number"),
            )
        except (conductor._EditRefused, ValueError):
            return True
        controls.finish_task(task_id, "succeeded", ACTOR, evidence=evidence)
        return True
    return False
