"""Exact standing-human recurrence authority, separate from natural discovery."""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy import String, cast
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement
from sqlmodel import select

from core.clock import as_utc
from core.jsonio import dumps
from factory.access import is_operator
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryReceipt,
    FactoryStart,
    WorkItem,
)

ACTION = "operator_issue_readmission"
ACTOR = "factory:operator-readmission"
LIMIT = 24
ADMISSION_READ_INTERVAL = 60
ADMISSION_READ_ACTION = "operator_readmission_check"


class IssueUnavailable(ValueError):
    """The exact issue was deleted or redirected; this recurrence must end."""


def _get(row, key):
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


class _ReceiptId(FunctionElement):
    type = String()
    inherit_cache = True


@compiles(_ReceiptId, "postgresql")
def _pg_receipt_id(element, compiler, **kw):
    return (
        "(CAST("
        + compiler.process(list(element.clauses)[0], **kw)
        + " AS JSONB)->>'receipt_id')"
    )


@compiles(_ReceiptId, "sqlite")
def _sqlite_receipt_id(element, compiler, **kw):
    return (
        "json_extract("
        + compiler.process(list(element.clauses)[0], **kw)
        + ", '$.receipt_id')"
    )


class _RecurrenceKey(FunctionElement):
    type = String()
    inherit_cache = True


@compiles(_RecurrenceKey, "postgresql")
def _pg_recurrence_key(element, compiler, **kw):
    return (
        "(CAST("
        + compiler.process(list(element.clauses)[0], **kw)
        + " AS JSONB)->>'recurrence_key')"
    )


@compiles(_RecurrenceKey, "sqlite")
def _sqlite_recurrence_key(element, compiler, **kw):
    return (
        "json_extract("
        + compiler.process(list(element.clauses)[0], **kw)
        + ", '$.recurrence_key')"
    )


def authority_clause():
    """The audit's exact receipt ID is server authority; queue markers are not."""
    return (
        select(FactoryAudit.id)
        .where(
            FactoryAudit.action == ACTION,
            cast(_ReceiptId(FactoryAudit.detail_json), String)
            == cast(FactoryReceipt.id, String),
            _RecurrenceKey(FactoryAudit.detail_json) == FactoryReceipt.recurrence_key,
            FactoryReceipt.actor == ACTOR,
            FactoryReceipt.recurrence_key != "",
        )
        .correlate(FactoryReceipt)
        .exists()
    )


def authorized(db, row):
    if row is None or _get(row, "actor") != ACTOR:
        return False
    return (
        db.exec(
            select(FactoryReceipt.id).where(
                FactoryReceipt.id == _get(row, "id"), authority_clause()
            )
        ).first()
        is not None
    )


def _issue(repo, number):
    from factory.orchestration.factory_conductor import github_get

    return github_get(repo, f"issues/{number}")


def _identity(principal):
    if not is_operator(principal) or principal.delegation_claim_present:
        raise ValueError("standing_operator_required")
    return principal.subject


def _issue_refusal(policy, repo, number, issue):
    if repo not in controls.enabled_repos(policy):
        return "repository_not_enabled"
    entry = controls.repo_entry(policy, repo)
    labels = {
        str(label["name"] if isinstance(label, dict) else label).lower()
        for label in issue.get("labels", [])
    }
    if (
        issue.get("number") != number
        or issue.get("html_url", "").lower()
        != f"https://github.com/{repo}/issues/{number}"
        or issue.get("state") != "open"
        or "pull_request" in issue
    ):
        return "issue_not_open"
    if issue.get("assignees") or issue.get("assignee"):
        return "assigned"
    if "human" in labels:
        return "human_owned"
    if labels & {s.lower() for s in entry["exclude_labels"]}:
        return "excluded_label"
    if not labels & {s.lower() for s in entry["labels"]}:
        return "intake_label_missing"
    if (
        not controls.intake_policy(policy)["enabled"]
        or controls.intake_policy(policy)["refinement_batch"]
    ):
        return "intake_not_available"
    return None


def _history(db, repo, number):
    from factory.orchestration.factory_intake import protected_work_history
    from factory.orchestration.factory_funding_limits import objective_task_ids
    from factory.orchestration.models import SwarmNodeRun

    history = protected_work_history(db, repo, number)
    task_ids = set()
    for row in history:
        task_ids.update(objective_task_ids(db, row))
    starts = db.exec(
        select(FactoryStart)
        .where(FactoryStart.task_id.in_(task_ids))
        .order_by(FactoryStart.id)
    ).all()
    runs = db.exec(
        select(SwarmNodeRun)
        .where(SwarmNodeRun.task_id.in_(task_ids))
        .order_by(SwarmNodeRun.id)
    ).all()
    events = db.exec(
        select(FactoryAudit)
        .where(
            (FactoryAudit.task_id.in_(task_ids))
            | (
                (FactoryAudit.action == "human_handoff")
                & cast(_ReceiptId(FactoryAudit.detail_json), String).in_(
                    [str(row.id) for row in history]
                )
            )
        )
        .order_by(FactoryAudit.id)
    ).all()
    return history, sorted(task_ids), starts, runs, events


def _evidence(db, control, repo, number, issue, *, exclude=None):
    """Read all aliases and durable ledgers; no mirror or queue marker grants authority."""
    from factory.orchestration import graph

    if control is None:
        raise ValueError("not_initialized")
    policy = json.loads(control.policy_json)
    refusal = _issue_refusal(policy, repo, number, issue)
    if refusal:
        raise ValueError(refusal)
    history, task_ids, starts, runs, events = _history(db, repo, number)
    priors = [row for row in history if row.id != exclude]
    # Holds win across classes, generations and identity aliases.
    if any(controls.landing_held(row) for row in history):
        raise ValueError("landing_held")
    items = db.exec(
        select(WorkItem)
        .where(
            ((WorkItem.github_repo == repo) & (WorkItem.github_issue_number == number))
            | WorkItem.id.in_([row.work_item_id for row in history if row.work_item_id])
        )
        .order_by(WorkItem.id)
    ).all()
    if any(
        "human" in {s.lower() for s in item.labels} or item.authority == "local"
        for item in items
    ):
        raise ValueError("human_owned")
    if any(event.action == "human_handoff" for event in events):
        raise ValueError("human_handoff")
    if any(
        row.state not in ("cancelled", "failed")
        and not (row.state == "succeeded" and controls.is_advisory(row.task_class))
        for row in priors
    ):
        raise ValueError("existing_work_owner")
    if not any(
        row.state == "cancelled"
        or (row.state == "failed" and row.task_class == "refine")
        for row in priors
    ):
        raise ValueError("no_excluded_terminal_history")
    if any(
        row.task_paused
        or row.cancellation_requested
        or controls.nonrenewable_recovery(row)
        for row in priors
    ):
        raise ValueError("prior_recovery_held")
    if any(controls.decision_in_flight(db, row.id) for row in history):
        raise ValueError("decision_in_flight")
    if any(s.status in controls._UNRESOLVED_STARTS for s in starts) or any(
        r.status not in graph.TERMINAL_RUN_STATUSES for r in runs
    ):
        raise ValueError("unresolved_execution")
    # Recovery authority belongs to its existing owner, never to a new recurrence.
    if any(
        "recovery" in event.action
        or event.action in ("funding_granted", "funding_review_requested")
        for event in events
    ):
        raise ValueError("prior_recovery_held")
    spent_starts = {}
    for start in starts:
        spent_starts.setdefault(
            (start.task_id, controls._start_node_key(start)), []
        ).append(start)
    spent = {}
    for run in runs:
        spent.setdefault((run.task_id, run.node_key), []).append(run)
    # Conservatively charge the largest node lineage to every fresh node. A rename
    # cannot discard a failed attempt, and exhausted history cannot get a new graph.
    for rows in spent.values():
        for run in rows:
            if run.node_key.startswith("conductor_"):
                continue
            bound = json.loads(run.pin_json or "{}").get("max_attempts")
            if type(bound) is int and graph._spent_attempts(rows) >= bound:
                raise ValueError("attempts_exhausted")
    debt_by_task = {}
    for (task_id, _key), rows in spent.items():
        debt_by_task[task_id] = max(
            debt_by_task.get(task_id, 0), graph._spent_attempts(rows)
        )
    for (task_id, key), rows in spent_starts.items():
        if key is None or not key.startswith("conductor_"):
            debt_by_task[task_id] = max(
                debt_by_task.get(task_id, 0), controls._accounting(rows)["turns_used"]
            )
    debt = sum(debt_by_task.values())
    from factory.orchestration.models import SwarmTask

    tasks = db.exec(
        select(SwarmTask).where(SwarmTask.id.in_(task_ids)).order_by(SwarmTask.id)
    ).all()
    if any(
        task.settled_at is None or task.start_state not in controls._SETTLED
        for task in tasks
    ):
        raise ValueError("unresolved_execution")
    accounted = controls._accounting(starts)
    effective = controls.repo_effective_policy(policy, repo)
    pinned = [json.loads(row.policy_json) for row in priors if row.policy_json]
    attempt_cap = min([effective["max_attempts"], *[p["max_attempts"] for p in pinned]])
    turn_cap = min(
        [
            controls.task_turn_ceiling(effective),
            *[controls.task_turn_ceiling(p) for p in pinned],
        ]
    )
    planner_cap = min(
        [
            controls.planner_turn_cap(effective),
            *[controls.planner_turn_cap(p) for p in pinned],
        ]
    )
    if debt >= attempt_cap:
        raise ValueError("attempts_exhausted")
    if (
        accounted["turns_used"] >= turn_cap
        or accounted["planner_turns_used"] >= planner_cap
    ):
        raise ValueError("turns_exhausted")
    directions = [json.loads(row.direction_json or "{}") for row in priors]
    if any(
        d.get("delivery_adoption")
        or d.get("delivery_pr_number")
        or d.get("prior_pr_number")
        for d in directions
    ):
        raise ValueError("prior_delivery_owner_required")
    inherited = {}
    removed = sorted(
        {
            model
            for d in directions
            for model in d.get("model_restriction", {}).get("removed_models", [])
        }
    )
    if removed:
        inherited["model_restriction"] = {"removed_models": removed}
    gates = [gate for d in directions for gate in d.get("conductor_gates", [])]
    if gates:
        inherited["conductor_gates"] = gates
    return {
        "inherited_direction": inherited,
        "control_version": control.version,
        "control_state": control.state,
        "policy": policy,
        "issue": issue,
        "items": [item.model_dump(mode="json") for item in items],
        "history": [row.model_dump(mode="json") for row in history],
        "starts": [s.model_dump(mode="json") for s in starts],
        "runs": [r.model_dump(mode="json") for r in runs],
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "audits": [event.model_dump(mode="json") for event in events],
        "prior_task_ids": task_ids,
        "accounting": accounted,
        "attempt_debt": debt,
        "remaining": {
            "max_attempts": attempt_cap - debt,
            "max_task_turns_hard": turn_cap - accounted["turns_used"],
            "max_planner_turns": planner_cap - accounted["planner_turns_used"],
        },
    }


def status(db, repo):
    events = db.exec(
        select(FactoryAudit).where(
            FactoryAudit.action == ACTION,
            FactoryAudit.created_at > controls._now() - timedelta(hours=24),
        )
    ).all()
    used = sum(
        json.loads(event.detail_json)["request"]["repo"] == repo for event in events
    )
    return {
        "successful_applications_last_24h": used,
        "successful_applications_limit": LIMIT,
        "remaining_applications": max(0, LIMIT - used),
    }


def _plan(db, control, actor, repo, number, reason, request_key):
    evidence = _evidence(db, control, repo, number, _issue(repo, number))
    payload = {
        "operator": actor,
        "repo": repo,
        "issue_number": number,
        "reason": reason,
        "request_key": request_key,
        "evidence": evidence,
    }
    digest = hashlib.sha256(dumps(payload).encode()).hexdigest()
    quota = status(db, repo)
    return payload, {
        "ok": True,
        "eligible": quota["remaining_applications"] > 0,
        "preview_sha256": digest,
        "repo": repo,
        "issue_number": number,
        "prior_task_ids": evidence["prior_task_ids"],
        "attempt_debt": evidence["attempt_debt"],
        "accounting": evidence["accounting"],
        "remaining": evidence["remaining"],
        **quota,
    }


def control(
    principal,
    operation,
    repo,
    issue_number,
    reason,
    request_key,
    expected_preview_sha256=None,
):
    """Private control entry point. Authentication precedes network or database I/O."""
    actor = _identity(principal)
    repo = controls.normalize_repo(repo)
    number = controls._integer(issue_number, "issue_number", 1, 2**31 - 1)
    reason = controls._text(reason, "reason", 2000)
    request_key = controls._text(request_key, "request_key", 200)
    request = {
        "actor": actor,
        "repo": repo,
        "issue_number": number,
        "reason": reason,
        "request_key": request_key,
        "preview_sha256": expected_preview_sha256,
    }
    if operation == "preview":
        with controls._read_session() as db:
            from factory.orchestration.factory_models import FactoryControl

            _, preview = _plan(
                db,
                db.get(FactoryControl, "factory"),
                actor,
                repo,
                number,
                reason,
                request_key,
            )
            return preview
    if operation != "apply" or not expected_preview_sha256:
        raise ValueError("readmission_preview_required")
    with controls._locked_session() as (db, current):
        events = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == ACTION)
        ).all()
        for event in events:
            detail = json.loads(event.detail_json)
            if event.actor == actor and detail["request"]["request_key"] == request_key:
                if detail["request"] != request:
                    raise ValueError("readmission_request_conflict")
                return detail["result"]
        payload, preview = _plan(db, current, actor, repo, number, reason, request_key)
        if preview["preview_sha256"] != expected_preview_sha256:
            raise ValueError("readmission_preview_changed")
        now = controls._now()
        if (
            sum(
                as_utc(event.created_at) > now - timedelta(hours=24)
                and json.loads(event.detail_json)["request"]["repo"] == repo
                for event in events
            )
            >= LIMIT
        ):
            raise ValueError("readmission_rolling_limit")
        from factory.orchestration.work_items import (
            mint_or_sync_from_github,
            derive_task_class,
        )

        issue = payload["evidence"]["issue"]
        item, _ = mint_or_sync_from_github(
            db, repo, issue, actor=actor, backfill_receipts=False
        )
        labels = {s["name"] if isinstance(s, dict) else s for s in issue["labels"]}
        task_class, _ = derive_task_class(labels, refine=False)
        row = FactoryReceipt(
            repo=repo,
            issue_number=number,
            generation=payload["evidence"]["policy"]["generation"],
            title=issue["title"],
            body=issue.get("body") or "",
            url=issue["html_url"],
            actor=ACTOR,
            task_class=task_class,
            recurrence_key=f"operator:{uuid4().hex}",
            work_item_id=item.id,
            direction_json=dumps(
                {
                    **payload["evidence"]["inherited_direction"],
                    "previous_task_ids": payload["evidence"]["prior_task_ids"],
                    "operator_readmission": {"reason": reason, "operator": actor},
                }
            ),
        )
        db.add(row)
        db.flush()
        result = {
            "ok": True,
            "receipt_id": row.id,
            "recurrence_key": row.recurrence_key,
            "generation": row.generation,
            "state": "queued",
            "dispatched": False,
        }
        db.add(
            FactoryAudit(
                actor=actor,
                action=ACTION,
                created_at=now,
                detail_json=dumps(
                    {
                        "request": request,
                        "receipt_id": row.id,
                        "recurrence_key": row.recurrence_key,
                        "proposal": payload,
                        "result": result,
                    }
                ),
            )
        )
        return result


def admission_read_due(db, row):
    """A failed or unused observation cannot cause a read on every admission pass."""
    return (
        db.exec(
            select(FactoryAudit.id).where(
                FactoryAudit.action == ADMISSION_READ_ACTION,
                cast(_ReceiptId(FactoryAudit.detail_json), String) == str(row.id),
                FactoryAudit.created_at
                > controls._now() - timedelta(seconds=ADMISSION_READ_INTERVAL),
            )
        ).first()
        is None
    )


def claim_admission_read(db, actor, row):
    db.add(
        FactoryAudit(
            actor=actor,
            action=ADMISSION_READ_ACTION,
            created_at=controls._now(),
            detail_json=dumps({"receipt_id": row.id}),
        )
    )


def admission_issue(repo, number):
    """Read once outside admission's control lock; failures grant no authority."""
    from factory.orchestration.github_read_cooldown import GitHubReadDeferred

    try:
        return _issue(repo, number)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (301, 302, 307, 308, 404, 410):
            return IssueUnavailable("issue_unavailable")
        return exc
    except (GitHubReadDeferred, httpx.HTTPError, ValueError) as exc:
        return exc


def admission_policy(db, control, row, issue):
    """Recheck current holds against this admission call's fresh issue observation."""
    if not authorized(db, row):
        raise ValueError("readmission_authority_missing")
    evidence = _evidence(
        db,
        control,
        row.repo,
        row.issue_number,
        issue,
        exclude=row.id,
    )
    policy = controls.repo_effective_policy(json.loads(control.policy_json), row.repo)
    # Only this receipt's pinned envelope is reduced. The standing policy is untouched.
    removed = (
        evidence["inherited_direction"]
        .get("model_restriction", {})
        .get("removed_models")
    )
    if removed:
        from factory.orchestration.factory_funding_limits import restrict_models

        policy = restrict_models(policy, removed)
    return {**policy, **evidence["remaining"]}


def pin_bounds(db, task_id, policy):
    from factory.orchestration.models import SwarmPlanVersion

    db.add(
        SwarmPlanVersion(
            task_id=task_id,
            version=1,
            op="operator_readmission_bounds",
            author_kind="system",
            author=ACTOR,
            cause_kind=ACTION,
            stated_reason="Retain prior attempt usage across the exact recurrence",
            change_json=dumps({"max_attempts": policy["max_attempts"]}),
        )
    )
