"""Operator-only preparation for bounded work, without admitting or resuming it."""

import hashlib
import json

from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryReceipt,
    FactoryStart,
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


def _preview(db, control, receipt_id, reason):
    reason = controls._text(reason, "reason", 4000)
    if control.state != "paused":
        raise ValueError("validation_requires_paused_admissions")
    rows = db.exec(select(FactoryReceipt).order_by(FactoryReceipt.id)).all()
    row = next((item for item in rows if item.id == receipt_id), None)
    if row is None or row.state not in ("queued", "escalated"):
        raise ValueError("validation_requires_waiting_receipt")
    policy = controls.validate_policy(json.loads(control.policy_json))
    _available_repo(row.repo)
    if row.repo != policy["repo"] or row.generation != policy["generation"]:
        raise ValueError("validation_requires_current_primary_receipt")
    if row.repo not in controls.enabled_repos(policy) or row.cancellation_requested:
        raise ValueError("validation_receipt_unavailable")
    if any(
        item.state in (*controls._ACTIVE, "landing")
        or (item.state == "queued" and item.id != row.id)
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
    proposed["issue_numbers"] = [row.issue_number]
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
    for repo, entry in proposed.get("repos", {}).items():
        entry["auto_merge"] = False
        if repo != row.repo:
            entry["paused"] = True
    proposed = controls.validate_policy(proposed)
    result = {
        "receipt_id": row.id,
        "task_id": row.task_id,
        "repo": row.repo,
        "issue_number": row.issue_number,
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
    result["preview_sha256"] = hashlib.sha256(
        controls._json(result).encode()
    ).hexdigest()
    return result


def preview(receipt_id, reason):
    with controls._locked_session() as (db, control):
        return _preview(db, control, receipt_id, reason)


def _replay(db, actor, request):
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.action == ACTION, FactoryAudit.actor == actor
        )
    ).all():
        previous = json.loads(raw)
        if previous["request"]["request_key"] == request["request_key"]:
            if previous["request"] != request:
                raise ValueError("conflicting_validation_request")
            return previous["result"]
    return None


def apply(receipt_id, reason, *, actor, request_key, expected_preview_sha256):
    actor = controls._text(actor, "actor")
    request = {
        "receipt_id": receipt_id,
        "reason": reason,
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    with controls._locked_session() as (db, control):
        replay = _replay(db, actor, request)
        if replay is not None:
            return replay
        proposal = _preview(db, control, receipt_id, reason)
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
