"""Fresh native refill repair, owned by producer, funding and native execution."""

import json
from datetime import timedelta

from sqlmodel import select

from factory.orchestration import backlog_refill as refill
from factory.orchestration import factory_controls as controls
from factory.orchestration import funding_profile
from factory.orchestration.factory_funding_limits import ROUTINE_HELD_LABELS
from factory.orchestration.factory_models import FactoryAudit, FactoryControl, WorkItem


def epoch(db):
    # Include disable/restore even when no refill tick observed the intermediate
    # policy. Task-local pauses are separately bound by the funding owner.
    row = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id.is_(None),
            FactoryAudit.action.in_(
                [
                    "configure",
                    "enable",
                    "pause_admissions",
                    "stop",
                    "problem_issues_control_applied",
                    "refill_control_applied",
                    refill.PREFIX + "policy_observed",
                ]
            ),
        )
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    return row.id if row else 0


def source(db, row):
    if row is None or row.actor != refill.ACTOR or row.issue_number != refill.CHARTER:
        return None
    event = refill.latest_event(db, "started", run_id=row.recurrence_key)
    control = db.get(FactoryControl, "factory")
    live = json.loads(control.policy_json or "{}") if control else {}
    start = refill._detail(event) if event else {}
    contract = start.get("repair_contract")
    item = db.get(WorkItem, row.work_item_id) if row.work_item_id else None
    if (
        type(contract) is not dict
        or type(contract.get("version")) is not int
        or contract != {"version": 1, "epoch": epoch(db)}
        or control is None
        or control.state != "enabled"
        or row.state != "admitted"
        or row.cancellation_requested
        or row.repo != start.get("repo")
        or row.id != start.get("receipt_id")
        or row.recurrence_key != start.get("run_id")
        or row.generation != live.get("generation")
        or start.get("generation") != row.generation
        or start.get("policy_digest") != controls.digest(live)
        or item is None
        or {str(x).lower() for x in item.labels} & ROUTINE_HELD_LABELS
        or item.authority != "github"
        or item.github_repo != row.repo
        or item.github_issue_number != refill.CHARTER
        or row.repo not in controls.enabled_repos(live)
        or not refill.normalize(live)["enabled"]
        or refill._ledger(db, row.repo)[3] is not None
        or refill.latest_event(db, "completed", run_id=row.recurrence_key)
    ):
        return None
    return start


def execution_failure_eligible(local_id):
    """Only fresh fixed-role attempt one trades capacity wait for terminal proof."""
    if not funding_profile.PROVIDER_ROUTE_AVAILABLE:
        return False
    fields = (local_id or "").split(":")
    if (
        len(fields) != 4
        or fields[0] != "factory"
        or fields[2] not in refill.NODES
        or fields[3] != "1"
    ):
        return False
    with controls._read_session() as db:
        return source(db, controls._receipt(db, fields[1])) is not None


def objective_open(task, issue, *, session=None):
    if session is None:
        with controls._read_session() as db:
            return objective_open(task, issue, session=db)
    row = controls._receipt(session, task["id"])
    if row is None or row.actor != refill.ACTOR:
        return issue.get("state") == "open"
    return bool(
        issue.get("state") in {"open", "closed"}
        and issue.get("number") == refill.CHARTER
        and task.get("issue_number") == refill.CHARTER
        and not issue.get("assignees")
        and not {x.get("name", "").lower() for x in issue.get("labels", [])}
        & ROUTINE_HELD_LABELS
        and source(session, row)
    )


def _failure(db, task_id, run_id):
    from factory.execution.preinvoke_failure import database_failure
    from factory.execution.reconciliation import read_not_invoked_factory_attempt
    from factory.orchestration.models import SwarmNodeRun

    run = db.get(SwarmNodeRun, run_id)
    if (
        run is None
        or run.task_id != task_id
        or run.attempt != 1
        or run.status != "failed"
    ):
        return None
    proof = read_not_invoked_factory_attempt(
        db, json.loads(run.pin_json), run.session_id
    )
    if (
        not proof
        or proof.get("capacity_denied")
        or proof != json.loads(run.outcome_json).get("not_invoked")
    ):
        return None
    return database_failure(proof.get("database_failure"))


def failed(task, key):
    from factory.orchestration import factory_funding as funding
    from factory.orchestration import graph

    if not funding_profile.PROVIDER_ROUTE_AVAILABLE or funding.routine_labels_held(
        task
    ):
        return False
    with controls._locked_session() as (db, _control):
        row = controls._receipt(db, task["id"])
        start = source(db, row)
        if (
            start is None
            or row.escalation_json
            or funding.routine_hold(db, row, task["id"])
            or funding._recovery_held(db, task["id"])
            or controls.decision_in_flight(db, row.id)
            or funding.latest(db, task["id"], "repair_allocation_wait")
            or funding.amendment(db, task["id"])
            or controls._accounting(controls._starts(db, task["id"]))[
                "unresolved_starts"
            ]
        ):
            return False
        graph._lock_task(db, task["id"])
        retry = funding._retry_target(db, task["id"])
        if (
            not retry
            or key not in refill.NODES
            or retry["node_key"] != key
            or retry["node"]["max_attempts"] != 1
        ):
            return False
        failure = _failure(db, task["id"], retry["source_run_id"])
        if failure is None:
            return False
        signature = controls.digest({"key": key, "failure": failure})
        detail = {
            "signature": signature,
            "repair_contract": start["repair_contract"],
            "policy_digest": start["policy_digest"],
        }
        if not funding.latest(db, task["id"], "refill_infrastructure_failure"):
            funding.record_audit(
                db,
                funding.ACTOR,
                "refill_infrastructure_failure",
                task_id=task["id"],
                source_run_id=retry["source_run_id"],
                **detail,
            )
        prior = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.action == "refill_infrastructure_failure",
                FactoryAudit.actor == funding.ACTOR,
                FactoryAudit.task_id != task["id"],
                FactoryAudit.created_at >= controls._now() - timedelta(hours=24),
            )
            .order_by(FactoryAudit.id.desc())
            .limit(50)
        ).all()
        if not any(
            all(json.loads(p.detail_json).get(k) == v for k, v in detail.items())
            and _failure(db, p.task_id, json.loads(p.detail_json)["source_run_id"])
            == failure
            for p in prior
        ):
            return False
        blocker = {"code": "refill_preinvoke:" + start["run_id"], "grant_id": None}
        funding._record_blocker(db, row, task["id"], blocker, retry=retry)
        funding.pause_allocation(db, task["id"], blocker, retry=retry)
        return funding.latest(db, task["id"], "repair_allocation_wait") is not None


def public_failure(db, task_id, run_id):
    """Reauthenticate native evidence before exporting a closed public subset."""
    from factory.execution.preinvoke_failure import repair_context
    from factory.orchestration.models import SwarmNodeRun

    row = controls._receipt(db, task_id)
    if source(db, row) is None:
        return None
    failure = _failure(db, task_id, run_id)
    run = db.get(SwarmNodeRun, run_id)
    return repair_context(failure, run.node_key) if failure and run else None


def attempt_allowed(db, row, key, attempt):
    if type(attempt) is not int:
        return False
    if attempt == 1:
        return True
    if attempt != 2 or key not in refill.NODES or source(db, row) is None:
        return False
    from factory.orchestration import factory_funding as funding

    grant = funding.amendment(db, row.task_id)
    request = funding.latest(db, row.task_id, "funding_review_requested")
    retry = (grant or {}).get("node_retry") or {}
    return bool(
        grant
        and request
        and grant.get("request_id") == request["audit_id"]
        and retry.get("node_key") == key
        and retry.get("before") == 1
        and retry.get("after") == 2
        and request.get("repair_recovery")
        and funding._repair_review_valid(db, row.task_id, request["repair_recovery"])
        and not funding.routine_hold(db, row, row.task_id)
    )


def reconcile(task, policy, runs, permission):
    from factory.orchestration import factory_funding as funding

    with controls._read_session() as db:
        wait = funding.latest(db, task["id"], "repair_allocation_wait")
        request = funding.latest(db, task["id"], "funding_review_requested")
        grant = funding.amendment(db, task["id"])
    if not wait:
        return False
    if task.get("task_paused") or not request or not request.get("repair_recovery"):
        return True
    if not grant or grant.get("request_id") != request["audit_id"]:
        funding.reconcile(task, policy, runs, permission)
        return True
    return False


def useful_delivery(db, row):
    if row.actor != refill.ACTOR:
        return True
    done = refill.latest_event(db, "completed", run_id=row.recurrence_key)
    reviewed = refill.latest_event(db, "reviewed", run_id=row.recurrence_key)
    return bool(
        row.state == "succeeded"
        and reviewed
        and done
        and refill._detail(done).get("success") is True
        and refill._detail(done).get("new_issues", 0) > 0
    )
