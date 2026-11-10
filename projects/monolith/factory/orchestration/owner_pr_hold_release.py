"""Operator release of text holds on one owner PR.

Owner PR discovery treats hold wording in a PR description or its discussion
as an explicit hold, deliberately without interpreting the prose. This control
records a standing operator's decision that the wording on one PR no longer
binds. It waives only that PR's description and the comments written before
the release. Labels, assignees, a later comment, the objective issue and every
review, CI and landing gate still apply. It starts no work.
"""

import hashlib
import json
from datetime import datetime

from core.clock import aware
from core.jsonio import dumps
from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit

RELEASED = "owner_pr_text_hold_released"
REVOKED = "owner_pr_text_hold_release_revoked"
OPERATIONS = {"release": RELEASED, "revoke": REVOKED}


def _target(detail: dict) -> tuple:
    request = detail.get("request") or {}
    return request.get("repo"), request.get("pr_number")


def released_at(db, repo: str, number: int) -> datetime | None:
    """When this PR's text holds were last released, or None if not in force."""
    events = db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action.in_((RELEASED, REVOKED)))
        .order_by(FactoryAudit.id.desc())
    ).all()
    for event in events:
        if _target(json.loads(event.detail_json)) == (repo, number):
            return aware(event.created_at) if event.action == RELEASED else None
    return None


def read_released_at(repo: str, number: int) -> datetime | None:
    with controls._read_session() as db:
        return released_at(db, repo, number)


def binding_comments(comments: list[dict], released: datetime | None) -> list[dict]:
    """Comments whose hold wording still binds: all, or those after the release."""
    if released is None:
        return comments
    kept = []
    for comment in comments:
        stamps = [comment.get("created_at"), comment.get("updated_at")]
        try:
            latest = max(
                datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                for stamp in stamps
                if stamp
            )
        except ValueError:
            # No readable time: the comment cannot be shown to predate the release.
            kept.append(comment)
            continue
        if latest > released:
            kept.append(comment)
    return kept


def _plan(db, control, repo, pr_number, operation, reason):
    if operation not in OPERATIONS:
        raise ValueError("owner_pr_hold_release_operation_invalid")
    if type(pr_number) is not int or pr_number < 1:
        raise ValueError("owner_pr_hold_release_pr_invalid")
    controls._text(reason, "reason", 4000)
    policy = json.loads(control.policy_json)
    if repo not in controls.repos_map(policy):
        raise ValueError("owner_pr_hold_release_repo_unknown")
    current = released_at(db, repo, pr_number)
    if operation == "release" and current is not None:
        raise ValueError("owner_pr_hold_already_released")
    if operation == "revoke" and current is None:
        raise ValueError("owner_pr_hold_not_released")
    proposal = {
        "repo": repo,
        "pr_number": pr_number,
        "operation": operation,
        "released_at": current.isoformat() if current else None,
        "reason": reason,
        "waives": ["pr_description", "pr_comments_before_release"],
        "still_binding": [
            "labels",
            "assignees",
            "later_comments",
            "objective_issue",
            "review_ci_and_landing_gates",
        ],
    }
    proposal["preview_sha256"] = hashlib.sha256(dumps(proposal).encode()).hexdigest()
    return proposal


def preview(repo, pr_number, operation, reason):
    from factory.orchestration.factory_models import FactoryControl

    with controls._read_session() as db:
        proposal = _plan(
            db, db.get(FactoryControl, "factory"), repo, pr_number, operation, reason
        )
        return {"ok": True, "preview": proposal}


def apply(
    actor, repo, pr_number, operation, reason, request_key, expected_preview_sha256
):
    controls._text(request_key, "request_key", 200)
    request = {
        "actor": actor,
        "repo": repo,
        "pr_number": pr_number,
        "operation": operation,
        "reason": reason,
        "request_key": request_key,
        "expected_preview_sha256": expected_preview_sha256,
    }
    with controls._locked_session() as (db, control):
        # Replay is checked before the state check, so a lost response is safe.
        events = db.exec(
            select(FactoryAudit).where(FactoryAudit.action.in_((RELEASED, REVOKED)))
        ).all()
        for event in events:
            detail = json.loads(event.detail_json)
            if detail["request"]["request_key"] == request_key:
                if detail["request"] != request:
                    raise ValueError("owner_pr_hold_release_request_conflict")
                return detail["result"]
        proposal = _plan(db, control, repo, pr_number, operation, reason)
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("owner_pr_hold_release_preview_changed")
        result = {
            "ok": True,
            "repo": repo,
            "pr_number": pr_number,
            "operation": operation,
            "dispatched": False,
        }
        controls._audit(
            db,
            actor,
            OPERATIONS[operation],
            request=request,
            proposal=proposal,
            result=result,
        )
        return result
