"""Fixed read-only graph on the ordinary bounded advisory executor."""

from __future__ import annotations

from sqlmodel import select

from factory.orchestration import backlog_refill as refill
from factory.orchestration import backlog_refill_producer as producer
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryReceipt


def receipt(task_id):
    with controls._read_session() as db:
        row = db.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task_id)
        ).first()
        if row is None or row.actor != refill.ACTOR:
            return None
        event = refill.latest_event(db, "started", run_id=row.recurrence_key)
        start = refill._detail(event) if event else None
        return {"run_id": row.recurrence_key, "start": start, "state": row.state}


def reconcile(task, policy, nodes, runs, expected_version):
    from factory.orchestration import factory_conductor as conductor

    owned = receipt(task["id"])
    if owned is None:
        return
    start = owned["start"]
    if start is None:
        return  # Missing intent is a refusal fence, never generic planner work.
    completed = {run["node_key"]: run for run in runs if run["status"] == "succeeded"}
    collector = completed.get(refill.NODE)
    reviewing = start.get("review_contract") is not None
    key = refill.NODE
    try:
        if collector is not None:
            value = conductor._artifact(collector)
            producer.collection(
                owned["run_id"], value, collector_run_id=collector.get("id")
            )
            if reviewing:
                key = refill.REVIEW_NODE
                if key in completed:
                    producer.review(owned["run_id"], completed[key].get("id"))
            if not reviewing or refill.REVIEW_NODE in completed:
                outcome = producer.advance(start)
                if outcome is not None:
                    result = controls.finish_task(
                        task["id"],
                        "succeeded" if outcome["success"] else "failed",
                        refill.ACTOR,
                        evidence={
                            "reason": "reviewed_refill_published"
                            if reviewing
                            else "refill_published"
                        },
                    )
                    if result["ok"]:
                        refill.settle(owned["run_id"], **outcome)
                return
        if any(
            run["node_key"] == key
            and run["status"] in conductor.graph.TERMINAL_RUN_STATUSES
            for run in runs
        ):
            controls.finish_task(
                task["id"],
                "failed",
                refill.ACTOR,
                evidence={"reason": f"refill_attempt_failed: {key}"},
            )
            return
        if any(node["node_key"] == key for node in nodes):
            return
        if not refill.dispatch_allowed(task["id"], key):
            return
        chosen = refill.role(start, key)
        prompt = (
            refill.review_brief(value)
            if key == refill.REVIEW_NODE
            else refill.inventory_brief(
                owned["run_id"],
                max_findings=start["policy"].get("max_findings", 100),
                reviewed=reviewing,
            )
        )
        result = conductor._add(
            task,
            policy,
            key,
            prompt,
            [refill.NODE] if key == refill.REVIEW_NODE else [],
            chosen["model"],
            "factory-backlog-refill",
            "Independent challenge and executable planning"
            if key == refill.REVIEW_NODE
            else "Bounded report-only inventory",
            refine=True,
            max_attempts=1,
            effort=chosen["effort"],
            turn_timeout_seconds=policy["turn_timeout_seconds"],
            expected_version=expected_version,
        )
        if result.ok:
            conductor._record_allowance(task["id"], policy, "factory-backlog-refill")
    except ValueError:
        controls.finish_task(
            task["id"],
            "failed",
            refill.ACTOR,
            evidence={
                "reason": "invalid_refill_review"
                if reviewing
                else "invalid_refill_collection"
            },
        )


def tick(policy):
    """Reconcile settled/cancelled runs and breaker writes even while disabled."""
    producer.reconcile_quarantined()
    with controls._read_session() as db:
        _starts, _done, pending, _opened, _recent, _daily = refill._ledger(
            db, policy.get("repo", "")
        )
        items = []
        for event in pending:
            detail = refill._detail(event)
            row = db.get(FactoryReceipt, detail["receipt_id"])
            if (
                row is not None
                and row.state in refill.TERMINAL
                and row.task_id not in controls.unresolved_start_task_ids(db)
            ):
                items.append((detail, row.state, row.task_paused))
        # An old opening retains its issue intent across an explicit reset.
        openings = [
            refill._detail(event) for event in refill._events(db, "breaker_open")
        ]
    for start, status, paused in items:
        outcome = producer.advance(start, allow_new=False)
        with controls._read_session() as db:
            has_collection = (
                refill.latest_event(
                    db,
                    "reviewed"
                    if start.get("review_contract") is not None
                    else "collection",
                    run_id=start["run_id"],
                )
                is not None
            )
        if has_collection and outcome is None:
            continue
        refill.settle(
            start["run_id"],
            **(
                outcome
                or {
                    "success": False,
                    "new_issues": 0,
                    "summary": f"Receipt settled {status}",
                    "inconclusive": status == "cancelled"
                    or paused
                    or start["policy_digest"] != refill.digest(policy),
                }
            ),
        )
    # One bounded publication/reconciliation per tick. Terminal openings do no I/O.
    for opening in reversed(openings):
        result = producer.breaker(opening)
        if result is None or result["status"] not in {
            "created",
            "existing",
            "rejected",
            "quarantined",
        }:
            break
