"""Digest-bound preparation of exact fresh advisory issues, inert until enable."""

import hashlib
import json

from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_validation as validation
from factory.orchestration.factory_models import (
    FactoryReceipt,
    FactoryStart,
    WorkItem,
    WorkItemEdge,
)

ACTION = "refinement_batch_prepared"


def _observations(numbers):
    with controls._locked_session() as (db, control):
        policy = controls.refinement_batch_policy(db, control, numbers)
        repo = validation._available_repo(policy["repo"])
    return {
        "repo": repo,
        "issues": [
            validation.issue_without_pr_references(repo, number) for number in numbers
        ],
    }


def _preview(db, control, numbers, reason, observations):
    reason = controls._text(reason, "reason", 4000)
    after = controls.refinement_batch_policy(db, control, numbers)
    before = controls.validate_policy(json.loads(control.policy_json))
    repo = validation._available_repo(before["repo"])
    if observations["repo"] != repo or len(observations["issues"]) != len(numbers):
        raise ValueError("refinement_batch_observation_changed")
    from factory.orchestration.factory_intake_loop import _label_names

    excluded = set(controls.repo_entry(before, repo)["exclude_labels"]) | {
        "human",
        "agent-ready",
        "needs-thought",
    }
    for number, issue in zip(numbers, observations["issues"], strict=True):
        if (
            not isinstance(issue, dict)
            or issue.get("number") != number
            or issue.get("html_url") != f"https://github.com/{repo}/issues/{number}"
            or issue.get("state") != "open"
            or "pull_request" in issue
            or issue.get("assignees") != []
            or not isinstance(issue.get("labels"), list)
            or _label_names(issue) & {label.lower() for label in excluded}
        ):
            raise ValueError(f"refinement_batch_issue_ineligible:{number}")
    rows = db.exec(select(FactoryReceipt).order_by(FactoryReceipt.id)).all()
    starts = db.exec(
        select(FactoryStart)
        .where(FactoryStart.status.in_(("reserved", "uncertain")))
        .order_by(FactoryStart.id)
    ).all()
    items = db.exec(
        select(WorkItem)
        .where(
            WorkItem.github_repo == repo,
            WorkItem.github_issue_number.in_(numbers),
        )
        .order_by(WorkItem.id)
    ).all()
    item_ids = [item.id for item in items]
    if any(row.work_item_id in item_ids for row in rows):
        raise ValueError("refinement_batch_existing_history")
    if any(
        item.authority == "local"
        or item.state not in ("open", "ready")
        or {label.lower() for label in item.labels}
        & {label.lower() for label in excluded}
        for item in items
    ):
        raise ValueError("refinement_batch_work_unavailable")
    if (
        item_ids
        and db.exec(
            select(WorkItemEdge.to_id)
            .join(WorkItem, WorkItemEdge.from_id == WorkItem.id)
            .where(
                WorkItemEdge.to_id.in_(item_ids),
                WorkItemEdge.kind == "blocks",
                WorkItem.state != "closed",
            )
        ).first()
        is not None
    ):
        raise ValueError("refinement_batch_work_blocked")
    result = {
        "repo": repo,
        "issue_numbers": numbers,
        "reason": reason,
        "expected_version": control.version,
        "policy_before": before,
        "policy_after": after,
        "state_after": "paused",
        "execution_authorized": False,
        "effects_after_enable": [
            "Natural intake may admit only the named advisory refinement issues.",
            "Refinement may post GitHub brief comments, change labels and notify human decisions.",
            "Ready issues remain excluded from automatic implementation; closure and landing remain off.",
        ],
        "evidence_sha256": hashlib.sha256(
            controls._json(
                {
                    "github": observations,
                    "receipts": [row.model_dump(mode="json") for row in rows],
                    "starts": [row.model_dump(mode="json") for row in starts],
                    "work_items": [row.model_dump(mode="json") for row in items],
                }
            ).encode()
        ).hexdigest(),
    }
    result["preview_sha256"] = hashlib.sha256(
        controls._json(result).encode()
    ).hexdigest()
    return result


def preview(issue_numbers, reason):
    numbers = controls.validate_refinement_batch(issue_numbers)
    observations = _observations(numbers)
    with controls._locked_session() as (db, control):
        return _preview(db, control, numbers, reason, observations)


def apply(issue_numbers, reason, *, actor, request_key, expected_preview_sha256):
    numbers = controls.validate_refinement_batch(issue_numbers)
    actor = controls._text(actor, "actor")
    request = {
        "issue_numbers": numbers,
        "reason": reason,
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    with controls._locked_session() as (db, _control):
        replay = validation._replay(db, actor, request, action=ACTION)
        if replay is not None:
            return replay
    observations = _observations(numbers)
    with controls._locked_session() as (db, control):
        replay = validation._replay(db, actor, request, action=ACTION)
        if replay is not None:
            return replay
        proposal = _preview(db, control, numbers, reason, observations)
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("validation_preview_changed")
        changed = controls.set_control(
            "prepare_refinement_batch",
            actor,
            refinement_issue_numbers=numbers,
            session=db,
        )
        if not changed["ok"]:
            raise ValueError(changed["reason"])
        result = {
            "ok": True,
            "version": changed["version"],
            "state": "paused",
            "execution_authorized": False,
            "issue_numbers": numbers,
            "preview_sha256": proposal["preview_sha256"],
        }
        controls._audit(
            db,
            actor,
            ACTION,
            request=request,
            result=result,
            policy_before=proposal["policy_before"],
            policy_after=proposal["policy_after"],
        )
        return result
