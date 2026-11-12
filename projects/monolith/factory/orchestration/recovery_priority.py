"""Retry durable landing requests before admission, under its control-row lock."""

import json
import re

import httpx
from sqlalchemy import func, or_
from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt, WorkItem
from factory.orchestration.github_read_cooldown import GitHubReadDeferred


def retry_pending(db, control):
    """Acquire real capacity for eligible requests; never reserve idle capacity.

    The caller holds _locked_session through this retry and its admission.
    Recovery uses the original request's authority and the existing recovery
    transition. GitHub reads recheck readiness, ownership and queue exclusion;
    unreadable or ineligible candidates do not fence unrelated admissions.
    """
    if control.state != "enabled":
        return
    policy = json.loads(control.policy_json)
    latest = (
        select(func.max(FactoryAudit.id).label("id"))
        .where(FactoryAudit.action == "landing_recovery_requested")
        .group_by(FactoryAudit.task_id)
        .subquery()
    )
    requests = db.exec(
        select(FactoryAudit)
        .join(latest, FactoryAudit.id == latest.c.id)
        .join(FactoryReceipt, FactoryReceipt.task_id == FactoryAudit.task_id)
        .where(FactoryReceipt.state.in_(("landing", "succeeded")))
        .order_by(FactoryAudit.id)
    ).all()
    if not requests:
        return
    from factory.orchestration import factory_landing as landing
    from factory.orchestration.factory_intake import lane_of
    from factory.orchestration.factory_intake_loop import _label_names
    from factory.orchestration.factory_rollback_runtime import parent_held
    from factory.orchestration import owner_prs

    for request in requests:
        row = controls._receipt(db, request.task_id)
        detail = json.loads(request.detail_json)
        if (
            detail.get("deadline_at") is not None
            or controls._landing_recovery_deadline(db, request.task_id) is not None
        ):
            continue
        barrier = controls.landing_recovery_barrier(request.task_id, session=db)
        if barrier["round_recorded"]:
            continue
        # Known terminal observations and ownership never become replay authority.
        fenced = db.exec(
            select(FactoryAudit.id).where(
                FactoryAudit.task_id == request.task_id,
                or_(
                    FactoryAudit.action.in_(("merged", "human_handoff")),
                    (FactoryAudit.action == "landing_recovery_skipped")
                    & (FactoryAudit.id > request.id),
                ),
            )
        ).first()
        item = db.get(WorkItem, row.work_item_id) if row.work_item_id else None
        entry = controls._stored_repos(policy).get(row.repo)
        if (
            fenced is not None
            or (
                item is not None and "human" in {label.lower() for label in item.labels}
            )
            or row.repo != policy["repo"]
            or lane_of(row) != "delivery"
            or not isinstance(entry, dict)
            or entry.get("enabled", True) is not True
            or entry.get("paused", False) is not False
            or row.task_paused
            or row.cancellation_requested
            or controls.landing_held(row)
            or parent_held(db, row)
            or controls.operator_landing_target(request.task_id)
            or controls.review_recovery_held(request.task_id, session=db)
            or not controls.execution_capacity_available(db, control, row)
        ):
            continue
        try:
            reason = detail["reason"]
            if reason not in controls.LANDING_RECOVERY_REASONS:
                continue
            if reason != "merge_conflict" and not controls.repo_auto_merge_enabled(
                policy, row.repo
            ):
                continue
            issue = landing.github_get(row.repo, f"issues/{row.issue_number}")
            if issue.get("state") != "open" or "human" in _label_names(issue):
                continue
            pr = landing.github_get(row.repo, f"pulls/{detail['pr_number']}")
            expected_head = (
                detail.get("current_head_sha")
                if reason == "head_moved"
                else detail["head_sha"]
            )
            if pr.get("merged"):
                continue
            # Match the landing loop's round-local skips. A closed/draft PR or
            # superseded head must not be re-read on every admission call.
            head = (pr.get("head") or {}).get("sha")
            skip_reason = None
            if pr.get("state") == "closed" or pr.get("draft") is True:
                skip_reason = "pull_request_not_ready"
            elif (
                isinstance(head, str)
                and re.fullmatch(r"[0-9a-f]{40}", head)
                and head != expected_head
            ):
                skip_reason = "head_moved"
            if skip_reason is not None:
                controls._audit(
                    db,
                    "factory:recovery-priority",
                    "landing_recovery_skipped",
                    task_id=request.task_id,
                    request_id=request.id,
                    pr_number=detail["pr_number"],
                    reason=skip_reason,
                )
                continue
            node_id = pr.get("node_id")
            if (
                pr.get("state") != "open"
                or pr.get("draft")
                or head != expected_head
                or not isinstance(node_id, str)
                or not node_id
                or pr.get("auto_merge") is not None
                or node_id in landing._queued_ids([node_id])
            ):
                continue
            direction = json.loads(row.direction_json or "{}")
            owner_prs.assert_unheld(
                {
                    "repo": row.repo,
                    "owner_pr": direction.get("owner_pr"),
                    "delivery_pr_number": detail["pr_number"],
                },
                pr,
                read_get=landing.github_get,
                read_list=landing.github_list,
            )
            controls.request_landing_recovery(
                request.task_id,
                detail["pr_number"],
                detail["head_sha"],
                detail["source"],
                "factory:recovery-priority",
                session=db,
                reason=reason,
                current_head_sha=detail.get("current_head_sha"),
            )
        except (GitHubReadDeferred, httpx.HTTPError, KeyError, TypeError, ValueError):
            # No speculative reservation: the next eligible request or ordinary
            # admission may use capacity, and the durable request stays intact.
            continue
