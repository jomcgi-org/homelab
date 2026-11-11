"""Operator release of text holds on one owner PR.

Owner PR discovery treats hold wording in a PR description or its discussion
as an explicit hold, deliberately without interpreting the prose. This control
records a standing operator's decision that the wording on one PR no longer
binds. It waives the exact description read at release, the comments written
before the release, and holds the PR only inherits from issues or PRs its
description mentions. An edited description, its labels and assignees, a
later comment, the closing objective issue and every review, CI and landing
gate still apply. Standing landing delegations do not read a release, so a released PR
lands on an explicit per-head grant. It starts no work.
"""

import hashlib
import json
from datetime import datetime
from typing import NamedTuple

from core.clock import aware
from core.jsonio import dumps
from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit

RELEASED = "owner_pr_text_hold_released"
REVOKED = "owner_pr_text_hold_release_revoked"
OPERATIONS = {"release": RELEASED, "revoke": REVOKED}


class Release(NamedTuple):
    at: datetime
    body_sha256: str
    audit_id: int


def body_sha256(body: str | None) -> str:
    return hashlib.sha256((body or "").encode()).hexdigest()


def _target(detail: dict) -> tuple:
    request = detail.get("request") or {}
    return request.get("repo"), request.get("pr_number")


def _latest(db, repo: str, number: int) -> FactoryAudit | None:
    events = db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action.in_((RELEASED, REVOKED)))
        .order_by(FactoryAudit.id.desc())
    ).all()
    for event in events:
        if _target(json.loads(event.detail_json)) == (repo, number):
            return event
    return None


def release(db, repo: str, number: int) -> Release | None:
    """The release in force for this PR, or None when absent or revoked."""
    event = _latest(db, repo, number)
    if event is None or event.action != RELEASED:
        return None
    proposal = json.loads(event.detail_json)["proposal"]
    return Release(aware(event.created_at), proposal["body_sha256"], event.id)


def read_release(repo: str, number: int) -> Release | None:
    with controls._read_session() as db:
        return release(db, repo, number)


def waives(found: Release | None, item: dict) -> bool:
    """True only for the exact description the operator released."""
    return found is not None and body_sha256(item.get("body")) == found.body_sha256


def binding_comments(comments: list[dict], found: Release | None) -> list[dict]:
    """Comments whose hold wording still binds: all, or those not before release."""
    if found is None:
        return comments
    # GitHub stamps whole seconds, so a comment in the release's own second
    # cannot be shown to predate it and stays binding.
    cutoff = found.at.replace(microsecond=0)
    kept = []
    for comment in comments:
        stamps = [comment.get("created_at"), comment.get("updated_at")]
        try:
            latest = max(
                aware(datetime.fromisoformat(str(stamp).replace("Z", "+00:00")))
                for stamp in stamps
                if stamp
            )
        except (ValueError, TypeError):
            # No readable time: the comment cannot be shown to predate the release.
            kept.append(comment)
            continue
        if latest >= cutoff:
            kept.append(comment)
    return kept


def _read_body(repo: str, number: int) -> str:
    from factory.orchestration import factory_conductor as conductor

    pull = conductor.github_get(repo, f"pulls/{number}")
    if not isinstance(pull, dict) or pull.get("number") != number:
        raise ValueError("owner_pr_hold_release_pr_unavailable")
    if pull.get("state") != "open":
        raise ValueError("owner_pr_hold_release_pr_not_open")
    return pull.get("body") or ""


def _plan(db, control, repo, pr_number, operation, reason, body):
    if operation not in OPERATIONS:
        raise ValueError("owner_pr_hold_release_operation_invalid")
    if type(pr_number) is not int or pr_number < 1:
        raise ValueError("owner_pr_hold_release_pr_invalid")
    controls._text(reason, "reason", 4000)
    policy = json.loads(control.policy_json)
    if repo not in controls.repos_map(policy):
        raise ValueError("owner_pr_hold_release_repo_unknown")
    current = release(db, repo, pr_number)
    sha = body_sha256(body)
    # A release of a new description replaces one that an edit left stale.
    if operation == "release" and current is not None and current.body_sha256 == sha:
        raise ValueError("owner_pr_hold_already_released")
    if operation == "revoke" and current is None:
        raise ValueError("owner_pr_hold_not_released")
    latest = _latest(db, repo, pr_number)
    proposal = {
        "repo": repo,
        "pr_number": pr_number,
        "operation": operation,
        "released_at": current.at.isoformat() if current else None,
        # Binds the digest to this PR's release history and description.
        "latest_audit_id": latest.id if latest else None,
        "body_sha256": sha,
        "reason": reason,
        "waives": [
            "this_exact_pr_description",
            "pr_comments_before_release",
            "holds_inherited_from_mentioned_items",
        ],
        "still_binding": [
            "edited_description",
            "labels",
            "assignees",
            "later_comments",
            "closing_objective_issue",
            "standing_landing_delegations",
            "review_ci_and_landing_gates",
        ],
    }
    proposal["preview_sha256"] = hashlib.sha256(dumps(proposal).encode()).hexdigest()
    return proposal


def preview(repo, pr_number, operation, reason):
    from factory.orchestration.factory_models import FactoryControl

    body = _read_body(repo, pr_number) if operation == "release" else None
    with controls._read_session() as db:
        proposal = _plan(
            db,
            db.get(FactoryControl, "factory"),
            repo,
            pr_number,
            operation,
            reason,
            body,
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
    # Read GitHub before the lock. A description edited since the preview
    # changes the digest and is refused below.
    replayed = False
    with controls._read_session() as db:
        replayed = any(
            json.loads(event.detail_json)["request"]["request_key"] == request_key
            for event in db.exec(
                select(FactoryAudit).where(FactoryAudit.action.in_((RELEASED, REVOKED)))
            ).all()
        )
    body = (
        _read_body(repo, pr_number) if operation == "release" and not replayed else None
    )
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
                latest = _latest(db, repo, pr_number)
                # An acknowledgement must not read as current state.
                if latest is None or latest.id != event.id:
                    raise ValueError("owner_pr_hold_release_superseded")
                return detail["result"]
        if operation == "release" and body is None:
            raise ValueError("owner_pr_hold_release_preview_changed")
        proposal = _plan(db, control, repo, pr_number, operation, reason, body)
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
