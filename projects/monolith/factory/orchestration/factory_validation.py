"""Operator-only preparation for bounded work, without admitting or resuming it."""

import hashlib
import json
import re

from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryReceipt,
    FactoryStart,
    WorkItem,
    WorkItemEdge,
    same_work,
)

ACTION = "bounded_validation_prepared"


def _available_repo(repo):
    from goosecracker.api import REPO_CATALOG

    repo = controls.normalize_repo(repo)
    if repo not in REPO_CATALOG:
        raise ValueError("repository_unavailable")
    return repo


def locate(
    repo, *, task_id=None, issue_number=None, pr_number=None, offset=0, limit=20
):
    """Exact selectors intersect, and ambiguous history is returned, never guessed."""
    from factory.orchestration import factory_conductor as conductor

    repo = _available_repo(repo)
    if not any(value is not None for value in (task_id, issue_number, pr_number)):
        raise ValueError("receipt_selector_required")
    pull = None
    if pr_number is not None:
        pull = conductor.github_get(repo, f"pulls/{pr_number}")
        if (
            pull.get("number") != pr_number
            or pull.get("base", {}).get("repo", {}).get("full_name") != repo
            or pull.get("head", {}).get("repo", {}).get("full_name") != repo
        ):
            raise ValueError("receipt_pr_identity_changed")
    with controls._read_session() as db:
        query = select(FactoryReceipt).where(FactoryReceipt.repo == repo)
        if task_id is not None:
            query = query.where(FactoryReceipt.task_id == task_id)
        if issue_number is not None:
            query = query.where(FactoryReceipt.issue_number == issue_number)
        matches = []
        for row in db.exec(query.order_by(FactoryReceipt.id)).all():
            direction = json.loads(row.direction_json or "{}")
            branch, granted = controls.granted_delivery_surface(direction)
            if branch is None and row.task_id:
                branch = conductor.task_branch(row.task_id)
            if pull is not None and (
                branch != pull.get("head", {}).get("ref")
                or (granted is not None and granted != pr_number)
            ):
                continue
            matches.append(
                {
                    "receipt_id": row.id,
                    "task_id": row.task_id,
                    "repo": row.repo,
                    "issue_number": row.issue_number,
                    "generation": row.generation,
                    "task_class": row.task_class,
                    "state": row.state,
                    "delivery_branch": branch,
                    "recorded_pr_number": granted,
                }
            )
        return {
            "ok": True,
            "receipts": matches[offset : offset + limit],
            "total": len(matches),
            "ambiguous": len(matches) > 1,
            "next_offset": offset + limit if offset + limit < len(matches) else None,
            "observed_at": controls._now().isoformat(),
        }


def _refinement_state(db, row, rows, *, inherit_landing_hold=False):
    """Only an unambiguous, quiescent agent-ready result can seed delivery."""
    from factory.execution.models import AgentSession, PendingMessage
    from factory.orchestration.models import SwarmTask

    if any(other.id != row.id and same_work(other, row) for other in rows):
        raise ValueError("validation_refinement_history_ambiguous")
    if not row.task_id or row.cancellation_requested or row.routing_tier != "advisory":
        raise ValueError("validation_refinement_unverified")
    direction = json.loads(row.direction_json or "{}")
    if (
        (controls.landing_held(row) and not inherit_landing_hold)
        or controls.review_recovery_held(row.task_id, session=db)
        or controls.decision_in_flight(db, row.id)
        or "review_only_hold" in direction
        or controls.nonrenewable_recovery(row)
    ):
        raise ValueError("validation_refinement_held")
    task = db.get(SwarmTask, row.task_id)
    audits = db.exec(
        select(FactoryAudit).where(
            FactoryAudit.task_id == row.task_id,
            FactoryAudit.action == "finish_task",
        )
    ).all()
    if (
        task is None
        or task.settled_at is None
        or task.start_state != "succeeded"
        or len(audits) != 1
    ):
        raise ValueError("validation_refinement_unverified")
    audit = audits[0]
    detail = json.loads(audit.detail_json)
    if (
        audit.actor != "factory:refine"
        or detail.get("outcome") != "succeeded"
        or detail.get("evidence", {}).get("state") != "refine_agent_ready"
    ):
        raise ValueError("validation_refinement_unverified")
    if (
        controls._quiescence_refusal(db, row.task_id)
        or db.exec(
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
        raise ValueError("validation_refinement_not_quiescent")
    item = None
    if row.work_item_id:
        item = db.get(WorkItem, row.work_item_id)
        if (
            item is None
            or item.state not in ("open", "ready")
            or "human" in {label.lower() for label in item.labels}
        ):
            raise ValueError("validation_refinement_work_unavailable")
        if (
            db.exec(
                select(WorkItemEdge.id)
                .join(WorkItem, WorkItem.id == WorkItemEdge.from_id)
                .where(
                    WorkItemEdge.to_id == row.work_item_id,
                    WorkItemEdge.kind == "blocks",
                    WorkItem.state != "closed",
                )
            ).first()
            is not None
        ):
            raise ValueError("validation_refinement_work_blocked")
    return {
        "audit": audit.model_dump(mode="json"),
        "task": task.model_dump(mode="json"),
        "work_item": item.model_dump(mode="json") if item else None,
    }


def _refinement_observation(receipt_id, *, inherit_landing_hold=False):
    """Bounded GitHub reads outside the control lock, repeated before apply."""
    from factory.orchestration import factory_conductor as conductor

    with controls._locked_session() as (db, control):
        row = db.get(FactoryReceipt, receipt_id) if receipt_id is not None else None
        if row is None or row.task_class != "refine" or row.state != "succeeded":
            return None
        if control.state != "paused":
            raise ValueError("validation_requires_paused_admissions")
        rows = db.exec(select(FactoryReceipt)).all()
        _refinement_state(db, row, rows, inherit_landing_hold=inherit_landing_hold)
        repo, number = _available_repo(row.repo), row.issue_number
    issue = conductor.github_get(repo, f"issues/{number}")
    # Timeline catches linked closed/merged PRs too. Open PR text catches work
    # that mentions this issue without an explicit closing relationship.
    for endpoint in (f"issues/{number}/timeline", "pulls?state=open"):
        for page in range(1, 6):
            separator = "&" if "?" in endpoint else "?"
            batch = conductor.github_list(
                repo, f"{endpoint}{separator}per_page=100&page={page}"
            )
            if not isinstance(batch, list) or any(
                not isinstance(item, dict) for item in batch
            ):
                raise ValueError("validation_refinement_pr_lookup_incomplete")
            for item in batch:
                if endpoint.startswith("issues/"):
                    source = item.get("source", {}).get("issue", {})
                    linked = "pull_request" in source
                    pr_number = source.get("number")
                else:
                    linked = number in {
                        int(short or url)
                        for short, url in re.findall(
                            rf"(?<![\w/])#(\d+)\b|https://github\.com/{re.escape(repo)}/issues/(\d+)\b",
                            (item.get("title") or "") + " " + (item.get("body") or ""),
                        )
                    }
                    pr_number = item.get("number")
                if linked:
                    # A reference is evidence to inspect, not proof of ownership.
                    # Do not silently waive it, including closed/merged PRs.
                    identity = (
                        str(pr_number)
                        if type(pr_number) is int and pr_number > 0
                        else "unknown"
                    )
                    raise ValueError(
                        f"validation_refinement_pr_reference_ambiguous:{identity}"
                    )
            if len(batch) < 100:
                break
        else:
            raise ValueError("validation_refinement_pr_lookup_incomplete")
    return {
        "receipt_id": receipt_id,
        "repo": repo,
        "issue_number": number,
        "issue": issue,
    }


def _refinement_eligible(row, policy, observation):
    from factory.orchestration.factory_intake_loop import _label_names

    if observation is None or any(
        observation[key] != value
        for key, value in (
            ("receipt_id", row.id),
            ("repo", row.repo),
            ("issue_number", row.issue_number),
        )
    ):
        raise ValueError("validation_refinement_observation_changed")
    issue = observation["issue"]
    labels = _label_names(issue)
    entry = controls.repo_entry(policy, row.repo)
    if (
        issue.get("number") != row.issue_number
        or issue.get("html_url")
        != f"https://github.com/{row.repo}/issues/{row.issue_number}"
        or issue.get("state") != "open"
        or "pull_request" in issue
        or issue.get("assignees") != []
        or "agent-ready" not in labels
        or "human" in labels
        or labels & {label.lower() for label in entry["exclude_labels"]}
        or not labels & {label.lower() for label in entry["labels"]}
    ):
        raise ValueError("validation_refinement_issue_ineligible")


def _preview(
    db,
    control,
    receipt_id,
    reason,
    *,
    issue_number=None,
    observation=None,
    inherit_landing_hold=False,
):
    reason = controls._text(reason, "reason", 4000)
    if control.state != "paused":
        raise ValueError("validation_requires_paused_admissions")
    rows = db.exec(select(FactoryReceipt).order_by(FactoryReceipt.id)).all()
    policy = controls.validate_policy(json.loads(control.policy_json))
    if (receipt_id is None) == (issue_number is None):
        raise ValueError("validation_requires_one_selector")
    row = None
    refinement = None
    if issue_number is not None:
        if type(issue_number) is not int or not 1 <= issue_number <= 2**63 - 1:
            raise ValueError("validation_invalid_issue_number")
        repo = _available_repo(policy["repo"])
        if any(
            item.repo == repo and item.issue_number == issue_number for item in rows
        ):
            raise ValueError("validation_existing_receipt_requires_receipt_selector")
    else:
        row = next((item for item in rows if item.id == receipt_id), None)
        completed_refine = (
            row is not None and row.task_class == "refine" and row.state == "succeeded"
        )
        if row is None or (
            row.state not in ("queued", "escalated") and not completed_refine
        ):
            raise ValueError("validation_requires_waiting_receipt")
        repo = _available_repo(row.repo)
        issue_number = row.issue_number
        if repo != policy["repo"] or row.generation != policy["generation"]:
            raise ValueError("validation_requires_current_primary_receipt")
        if row.cancellation_requested:
            raise ValueError("validation_receipt_unavailable")
        if completed_refine:
            refinement = _refinement_state(
                db, row, rows, inherit_landing_hold=inherit_landing_hold
            )
            _refinement_eligible(row, policy, observation)
    if repo not in controls.enabled_repos(policy):
        raise ValueError("validation_receipt_unavailable")
    if any(
        item.state == "landing"
        or item.state in controls._ACTIVE
        or (item.state == "queued" and (row is None or item.id != row.id))
        for item in rows
    ):
        raise ValueError("validation_other_work_present")
    if (
        db.exec(
            select(FactoryStart.id).where(
                FactoryStart.status.in_(("reserved", "uncertain"))
            )
        ).first()
        is not None
    ):
        raise ValueError("validation_unresolved_execution")
    # This is configuration only. Recovery/resume still enforces live issue,
    # human ownership, decision, execution and immutable recovery-bound checks.
    proposed = json.loads(controls._json(policy))
    proposed["auto_merge"] = False
    proposed["issue_numbers"] = [issue_number]
    proposed["intake"] = {
        **controls.intake_policy(policy),
        "enabled": False,
        "refine_enabled": False,
        "close_enabled": False,
    }
    proposed["problem_issues"] = {
        **controls.problem_issues_policy(policy),
        "enabled": False,
    }
    # Disable other repositories without changing any envelope or generation.
    # Explicit primary landing overrides must not defeat the global fence.
    for entry_repo, entry in proposed.get("repos", {}).items():
        entry["auto_merge"] = False
        if entry_repo != repo:
            entry["paused"] = True
    proposed = controls.validate_policy(proposed)
    result = {
        "receipt_id": row.id if row else None,
        "task_id": row.task_id if row else None,
        "repo": repo,
        "issue_number": issue_number,
        "reason": reason,
        "expected_version": control.version,
        "policy_before": policy,
        "policy_after": proposed,
        "state_after": "paused",
        "execution_authorized": False,
        "receipt_fingerprint": hashlib.sha256(
            controls._json([item.model_dump(mode="json") for item in rows]).encode()
        ).hexdigest(),
    }
    if refinement is not None:
        result["selection_kind"] = "refined_issue"
        result["refinement_fingerprint"] = hashlib.sha256(
            controls._json({"state": refinement, "observation": observation}).encode()
        ).hexdigest()
    result["preview_sha256"] = hashlib.sha256(
        controls._json(result).encode()
    ).hexdigest()
    return result


def preview(receipt_id, reason, *, issue_number=None):
    observation = _refinement_observation(receipt_id)
    with controls._locked_session() as (db, control):
        return _preview(
            db,
            control,
            receipt_id,
            reason,
            issue_number=issue_number,
            observation=observation,
        )


def _replay(db, actor, request, *, action=ACTION):
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.action == action, FactoryAudit.actor == actor
        )
    ).all():
        previous = json.loads(raw)
        if previous["request"]["request_key"] == request["request_key"]:
            if previous["request"] != request:
                raise ValueError("conflicting_validation_request")
            return previous["result"]
    return None


def apply(
    receipt_id,
    reason,
    *,
    actor,
    request_key,
    expected_preview_sha256,
    issue_number=None,
):
    actor = controls._text(actor, "actor")
    request = {
        "receipt_id": receipt_id,
        "reason": reason,
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    if issue_number is not None:
        request["issue_number"] = issue_number
    with controls._locked_session() as (db, control):
        replay = _replay(db, actor, request)
        if replay is not None:
            return replay
    observation = _refinement_observation(receipt_id)
    with controls._locked_session() as (db, control):
        replay = _replay(db, actor, request)
        if replay is not None:
            return replay
        proposal = _preview(
            db,
            control,
            receipt_id,
            reason,
            issue_number=issue_number,
            observation=observation,
        )
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("validation_preview_changed")
        result = controls.set_control(
            "configure", actor, policy=proposal["policy_after"], session=db
        )
        if not result["ok"]:
            raise ValueError(result["reason"])
        result = {
            "ok": True,
            "receipt_id": receipt_id,
            "version": result["version"],
            "state": "paused",
            "execution_authorized": False,
            "preview_sha256": proposal["preview_sha256"],
        }
        if "selection_kind" in proposal:
            result["selection_kind"] = proposal["selection_kind"]
            result["delivery_receipt_created"] = False
        controls._audit(
            db,
            actor,
            ACTION,
            task_id=proposal["task_id"],
            request=request,
            result=result,
            policy_before=proposal["policy_before"],
            policy_after=proposal["policy_after"],
        )
        return result


BACKLOG_ACTION = "backlog_intake_prepared"


def _backlog_preview(db, control, reason):
    """Return to reviewed intake explicitly, never as a task-completion effect."""
    reason = controls._text(reason, "reason", 4000)
    if control.state != "paused":
        raise ValueError("validation_requires_paused_admissions")
    rows = db.exec(select(FactoryReceipt).order_by(FactoryReceipt.id)).all()
    if any(row.state in ("uncertain", "landing") for row in rows):
        raise ValueError("validation_other_work_present")
    active = any(row.state in (*controls._ACTIVE, "queued") for row in rows)
    starts = db.exec(
        select(FactoryStart)
        .where(FactoryStart.status.in_(("reserved", "uncertain")))
        .order_by(FactoryStart.id)
    ).all()
    if any(start.status == "uncertain" for start in starts) or (
        not active and any(start.status == "reserved" for start in starts)
    ):
        raise ValueError("validation_unresolved_execution")
    before = controls.validate_policy(json.loads(control.policy_json))
    repo = _available_repo(before["repo"])
    if repo not in controls.enabled_repos(before):
        raise ValueError("validation_receipt_unavailable")
    after = json.loads(controls._json(before))
    after["auto_merge"] = False
    after["issue_numbers"] = []
    intake = controls.intake_policy(before)
    # This surface cannot broaden labels, exclusions, dependency trust or caps.
    # Require the existing agent-ready filter, rather than inventing readiness.
    primary = controls.repo_entry(before, repo)
    if primary["labels"] != ["agent-ready"]:
        raise ValueError("backlog_requires_agent_ready_filter")
    if not {"needs-human", "security-finding", "wontfix"} <= set(
        primary["exclude_labels"]
    ):
        raise ValueError("backlog_requires_safe_exclusions")
    after["intake"] = {
        **intake,
        "enabled": True,
        "refine_enabled": False,
        "close_enabled": False,
    }
    after["problem_issues"] = {
        **controls.problem_issues_policy(before),
        "enabled": False,
    }
    for entry_repo, entry in after.get("repos", {}).items():
        entry["auto_merge"] = False
        if entry_repo != repo:
            entry["paused"] = True
    after = controls.validate_policy(after)
    if active:
        # Do not strand queued operator receipts by clearing their allowlist,
        # or alter the policy/landing authority of already admitted work.
        after = controls.active_intake_restoration_policy(db, control)
    result = {
        "repo": repo,
        "reason": reason,
        "expected_version": control.version,
        "policy_before": before,
        "policy_after": after,
        "state_after": "paused",
        "execution_authorized": False,
        "mode": "active_discovery_only" if active else "idle_backlog_restore",
        "eligibility": "Existing intake ownership, dependencies, linked PR, receipt history and readiness checks apply after explicit enable.",
        "receipt_fingerprint": hashlib.sha256(
            controls._json([row.model_dump(mode="json") for row in rows]).encode()
        ).hexdigest(),
        "start_fingerprint": hashlib.sha256(
            controls._json([start.model_dump(mode="json") for start in starts]).encode()
        ).hexdigest(),
    }
    result["preview_sha256"] = hashlib.sha256(
        controls._json(result).encode()
    ).hexdigest()
    return result


def preview_backlog(reason):
    with controls._locked_session() as (db, control):
        return _backlog_preview(db, control, reason)


def apply_backlog(reason, *, actor, request_key, expected_preview_sha256):
    actor = controls._text(actor, "actor")
    request = {
        "reason": reason,
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    with controls._locked_session() as (db, control):
        replay = _replay(db, actor, request, action=BACKLOG_ACTION)
        if replay is not None:
            return replay
        proposal = _backlog_preview(db, control, reason)
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("validation_preview_changed")
        if proposal["mode"] == "active_discovery_only":
            result = controls.set_control("restore_active_intake", actor, session=db)
        else:
            result = controls.set_control(
                "configure", actor, policy=proposal["policy_after"], session=db
            )
        if not result["ok"]:
            raise ValueError(result["reason"])
        result = {
            "ok": True,
            "version": result["version"],
            "state": "paused",
            "execution_authorized": False,
            "preview_sha256": proposal["preview_sha256"],
        }
        controls._audit(
            db,
            actor,
            BACKLOG_ACTION,
            request=request,
            result=result,
            policy_before=proposal["policy_before"],
            policy_after=proposal["policy_after"],
        )
        return result
