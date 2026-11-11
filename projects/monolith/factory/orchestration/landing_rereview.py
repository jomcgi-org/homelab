"""Operator request for a fresh native review after an authorized head move.

Landing refuses ``head_moved`` when a delivery's PR head differs from the head
its independent review approved, for example after an operator merged the base
branch into the PR to resolve conflicts. Nothing automatic recovers that state.
This control reopens one such delivery through the existing bounded landing
recovery with reason ``head_moved``: one correction round verifies the moved
head without rewriting history, then a fresh native review of the exact current
head must approve before landing can re-arm. It grants no landing authority,
waives no review or CI and releases no hold.
"""

import hashlib
import json
import re

from core.jsonio import dumps
from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt

ACTION = "landing_rereview_requested"
_SHA = re.compile(r"[0-9a-f]{40}")


def _receipt(db, receipt_id):
    if type(receipt_id) is not int or receipt_id < 1:
        raise ValueError("landing_rereview_receipt_invalid")
    row = db.get(FactoryReceipt, receipt_id)
    if row is None or not row.task_id:
        raise ValueError("landing_rereview_receipt_unknown")
    return row


def _read_pull(receipt_id, pr_number) -> dict:
    from factory.orchestration import factory_conductor as conductor

    if type(pr_number) is not int or pr_number < 1:
        raise ValueError("landing_rereview_pr_invalid")
    with controls._read_session() as db:
        repo = _receipt(db, receipt_id).repo
    pull = conductor.github_get(repo, f"pulls/{pr_number}")
    if not isinstance(pull, dict) or pull.get("number") != pr_number:
        raise ValueError("landing_rereview_pr_unavailable")
    return pull


def _plan(db, control, receipt_id, pr_number, head_sha, reason, pull):
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_landing import _delivery_prs
    from factory.orchestration.models import SwarmTask

    if not isinstance(head_sha, str) or not _SHA.fullmatch(head_sha):
        raise ValueError("landing_rereview_head_invalid")
    controls._text(reason, "reason", 4000)
    row = _receipt(db, receipt_id)
    if row.state not in ("succeeded", "landing"):
        raise ValueError("landing_rereview_receipt_not_settled")
    if controls.landing_held(row):
        # Holds are released separately with factory_release_landing_hold.
        raise ValueError("landing_rereview_landing_held")
    number, approved = _delivery_prs(db, [row.task_id]).get(row.task_id, (None, None))
    if number is None:
        raise ValueError("landing_rereview_delivery_missing")
    if number != pr_number:
        raise ValueError("landing_rereview_pr_mismatch")
    if not isinstance(approved, str) or not _SHA.fullmatch(approved):
        raise ValueError("landing_rereview_approved_head_missing")
    if pull.get("merged") or pull.get("state") != "open":
        raise ValueError("landing_rereview_pr_not_open")
    if (pull.get("head") or {}).get("sha") != head_sha:
        raise ValueError("landing_rereview_head_changed")
    if head_sha == approved:
        raise ValueError("landing_rereview_head_not_moved")
    task = db.get(SwarmTask, row.task_id)
    if task is None:
        raise ValueError("landing_rereview_task_missing")
    direction = json.loads(row.direction_json or "{}")
    branch, granted_pr = controls.granted_delivery_surface(direction)
    identity = {
        "id": row.task_id,
        "repo": row.repo,
        "base_branch": task.base_branch,
        "delivery_branch": branch or conductor.task_branch(row.task_id),
        "delivery_adoption": direction.get("delivery_adoption", False),
    }
    if (granted_pr is not None and granted_pr != pr_number) or not (
        conductor.delivery_identity_matches(identity, pull)
    ):
        raise ValueError("landing_rereview_branch_mismatch")
    # Settlement and recovery requests only: landing ticks also audit refusals.
    latest = db.exec(
        select(FactoryAudit.id)
        .where(
            FactoryAudit.task_id == row.task_id,
            FactoryAudit.action.in_(
                ("finish_task", "delivery_ready", "landing_recovery_requested")
            ),
        )
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    proposal = {
        "receipt_id": row.id,
        "task_id": row.task_id,
        "repo": row.repo,
        "pr_number": pr_number,
        "branch": conductor.delivery_branch(identity),
        "state": row.state,
        "approved_head_sha": approved,
        "head_sha": head_sha,
        "reason": reason,
        # Binds the digest to this delivery's settlement and recovery history.
        "latest_audit_id": latest,
        "control_version": control.version,
        "requests": "landing_recovery reason head_moved",
        "still_binding": [
            "fresh_native_review_at_exact_current_head",
            "required_ci",
            "landing_authority_and_holds",
            "recovery_limit_capacity_and_deadline",
            "merge_queue",
        ],
    }
    proposal["preview_sha256"] = hashlib.sha256(dumps(proposal).encode()).hexdigest()
    return proposal


def preview(receipt_id, pr_number, head_sha, reason):
    from factory.orchestration.factory_models import FactoryControl

    pull = _read_pull(receipt_id, pr_number)
    with controls._read_session() as db:
        proposal = _plan(
            db,
            db.get(FactoryControl, "factory"),
            receipt_id,
            pr_number,
            head_sha,
            reason,
            pull,
        )
        return {"ok": True, "preview": proposal}


def _replay(db, request):
    for event in db.exec(select(FactoryAudit).where(FactoryAudit.action == ACTION)):
        detail = json.loads(event.detail_json)
        if detail["request"]["request_key"] == request["request_key"]:
            if detail["request"] != request:
                raise ValueError("landing_rereview_request_conflict")
            return detail["result"]
    return None


def apply(
    actor,
    receipt_id,
    pr_number,
    head_sha,
    reason,
    request_key,
    expected_preview_sha256,
):
    controls._text(request_key, "request_key", 200)
    request = {
        "actor": actor,
        "receipt_id": receipt_id,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "reason": reason,
        "request_key": request_key,
        "expected_preview_sha256": expected_preview_sha256,
    }
    with controls._read_session() as db:
        replayed = _replay(db, request)
    if replayed is not None:
        return replayed
    # Read GitHub before the lock. A head moved since preview changes the
    # digest and is refused below.
    pull = _read_pull(receipt_id, pr_number)
    with controls._locked_session() as (db, control):
        # Replay is checked before state, so a lost response is safe.
        replayed = _replay(db, request)
        if replayed is not None:
            return replayed
        proposal = _plan(db, control, receipt_id, pr_number, head_sha, reason, pull)
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("landing_rereview_preview_changed")
        recovery = controls.request_landing_recovery(
            proposal["task_id"],
            pr_number,
            proposal["approved_head_sha"],
            "delivered_pr",
            actor,
            session=db,
            reason="head_moved",
            current_head_sha=head_sha,
        )
        result = {
            "ok": bool(recovery.get("ok")),
            "receipt_id": receipt_id,
            "pr_number": pr_number,
            "approved_head_sha": proposal["approved_head_sha"],
            "head_sha": head_sha,
            "recovery": recovery,
        }
        controls._audit(
            db,
            actor,
            ACTION,
            task_id=proposal["task_id"],
            request=request,
            proposal=proposal,
            result=result,
        )
        return result
