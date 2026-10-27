"""Explicit operator release of a staged delivery hold, without accepting delivery.

The preview binds the owned PR head and current receipt state. Application only
removes that hold or refreshes its audited authorization after a head change,
leaving execution, review, CI and rollout gates unchanged.
"""

import hashlib
import json
import re

from sqlmodel import select

from core.jsonio import dumps
from factory.execution.models import AgentSession, PendingMessage
from factory.orchestration import factory_controls as controls, graph
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt

ACTION = "operator_landing_hold_released"


def _pull(receipt_id, pr_number, head_sha):
    from factory.orchestration.factory_conductor import github_get

    if type(pr_number) is not int or pr_number <= 0:
        raise ValueError("invalid_pr_number")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("invalid_head_sha")
    with controls._read_session() as db:
        row = db.get(FactoryReceipt, receipt_id)
        if row is None:
            raise ValueError("unknown_receipt")
        repo = row.repo
    # Network observations precede the control lock. Identity is checked again
    # against the locked receipt, and apply obtains a fresh observation.
    return github_get(repo, f"pulls/{pr_number}")


def _refreshable(db, row, pr_number, head_sha):
    """Only refresh a prior audited release for this receipt and the same PR."""
    direction = json.loads(row.direction_json or "{}")
    authorization = direction.get("landing_authorization")
    if (
        not isinstance(authorization, dict)
        or authorization.get("pr_number") != pr_number
        or authorization.get("head_sha") == head_sha
    ):
        return False
    for event in db.exec(
        select(FactoryAudit).where(
            FactoryAudit.action == ACTION, FactoryAudit.task_id == row.task_id
        )
    ).all():
        detail = json.loads(event.detail_json)
        request = detail.get("request", {})
        if (
            event.actor == authorization.get("actor")
            and request.get("receipt_id") == row.id
            and request.get("pr_number") == pr_number
            and request.get("head_sha") == authorization.get("head_sha")
            and request.get("request_key") == authorization.get("request_key")
            and detail.get("result", {}).get("ok") is True
        ):
            return True
    return False


def _preview(db, control, receipt_id, pr_number, head_sha, reason, pull):
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_decisions import decision_in_flight

    reason = controls._text(reason, "reason", 4000)
    row = db.get(FactoryReceipt, receipt_id)
    if (
        row is None
        or not row.task_id
        or row.state not in (*controls._ACTIVE, "landing", "succeeded", "escalated")
        or row.cancellation_requested
        or control.state not in ("enabled", "paused")
        or (
            not controls.landing_held(row)
            and not _refreshable(db, row, pr_number, head_sha)
        )
    ):
        raise ValueError("landing_release_requires_held_receipt")
    if controls.review_recovery_held(row.task_id, session=db):
        raise ValueError("review_only_recovery")
    refusal = controls._quiescence_refusal(db, row.task_id)
    if refusal:
        raise ValueError("landing_release_" + refusal)
    if (
        db.exec(
            select(PendingMessage.id)
            .join(AgentSession)
            .where(
                AgentSession.local_session_id.startswith(
                    f"factory:{row.task_id}:", autoescape=True
                ),
                PendingMessage.claimed_by_replica.isnot(None),
            )
        ).first()
        is not None
    ):
        raise ValueError("landing_release_claimed_execution")
    if decision_in_flight(db, row.id):
        raise ValueError("landing_release_decision_in_flight")
    direction = json.loads(row.direction_json or "{}")
    policy = controls._effective_policy(db, row)
    identity = {
        **direction,
        "id": row.task_id,
        "repo": row.repo,
        "base_branch": policy["base_branch"],
    }
    granted = conductor.delivery_pr_number(identity)
    if (
        pull.get("number") != pr_number
        or pull.get("head", {}).get("sha") != head_sha
        or not conductor.delivery_identity_matches(identity, pull)
        or (granted is not None and granted != pr_number)
        or (pull.get("state") != "open" and pull.get("merged") is not True)
    ):
        raise ValueError("landing_release_pr_identity_changed")
    if controls.delivery_branch_owner(
        db, row.repo, conductor.delivery_branch(identity), exclude_receipt_id=row.id
    ):
        raise ValueError("landing_release_branch_owned_elsewhere")
    result = {
        "operation": "release" if controls.landing_held(row) else "refresh",
        "receipt_id": row.id,
        "task_id": row.task_id,
        "repo": row.repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "pr_merged": pull.get("merged") is True,
        "pr_draft": pull.get("draft") is True,
        "pr_body_sha256": hashlib.sha256((pull.get("body") or "").encode()).hexdigest(),
        "merge_commit_sha": pull.get("merge_commit_sha")
        if pull.get("merged")
        else None,
        "expected_version": control.version,
        "expected_revision": graph.current_version(row.task_id, session=db),
        "receipt_state": row.state,
        "receipt_updated_at": row.updated_at.isoformat(),
        "receipt_body_sha256": hashlib.sha256(row.body.encode()).hexdigest(),
        "stays_paused": row.task_paused,
        "direction": direction,
        "policy": policy,
        "reason": reason,
        "delivery_accepted": False,
    }
    result["preview_sha256"] = hashlib.sha256(dumps(result).encode()).hexdigest()
    return result


def preview(receipt_id, pr_number, head_sha, reason):
    pull = _pull(receipt_id, pr_number, head_sha)
    with controls._locked_session() as (db, control):
        return _preview(db, control, receipt_id, pr_number, head_sha, reason, pull)


def _replay(db, actor, request):
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.action == ACTION, FactoryAudit.actor == actor
        )
    ).all():
        previous = json.loads(raw)
        if previous["request"]["request_key"] == request["request_key"]:
            if previous["request"] != request:
                raise ValueError("conflicting_landing_release_request")
            if previous["result"].get("pending"):
                for applied_raw in db.exec(
                    select(FactoryAudit.detail_json).where(
                        FactoryAudit.action.in_(
                            (
                                "settled_landing_hold_released",
                                "execution_capacity_reacquired",
                            )
                        ),
                        FactoryAudit.task_id == previous["result"]["task_id"],
                    )
                ).all():
                    applied = json.loads(applied_raw)
                    authorization = applied.get("landing_authorization") or {}
                    if (
                        "landing_hold" in applied.get("intent", {})
                        and authorization.get("actor") == actor
                        and authorization.get("request_key") == request["request_key"]
                    ):
                        return {**previous["result"], "pending": False, "reason": None}
            return previous["result"]
    return None


def apply(
    receipt_id,
    pr_number,
    head_sha,
    reason,
    *,
    actor,
    request_key,
    expected_preview_sha256,
):
    actor = controls._text(actor, "actor")
    request = {
        "receipt_id": receipt_id,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "reason": reason,
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    with controls._read_session() as db:
        replay = _replay(db, actor, request)
        if replay is not None:
            return replay
    pull = _pull(receipt_id, pr_number, head_sha)
    with controls._locked_session() as (db, control):
        replay = _replay(db, actor, request)
        if replay is not None:
            return replay
        proposal = _preview(db, control, receipt_id, pr_number, head_sha, reason, pull)
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("landing_release_preview_changed")
        row = db.get(FactoryReceipt, receipt_id)
        direction = json.loads(row.direction_json)
        prior_hold = direction.get("landing_hold")
        prior_authorization = direction.get("landing_authorization")
        direction["landing_authorization"] = {
            "actor": actor,
            "request_key": request_key,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "reason": proposal["reason"],
            "authorized_at": controls._now().isoformat(),
        }
        row.direction_json = dumps(direction)
        release = {"ok": True, "pending": False, "reason": None}
        if proposal["operation"] == "release":
            if row.state in controls._ACTIVE:
                release = controls.request_capacity_release(
                    db, control, row, actor, release_hold=True
                )
                if not release["ok"]:
                    raise ValueError(release["reason"])
            else:
                direction.pop("landing_hold")
                row.direction_json = dumps(direction)
        row.updated_at = controls._now()
        control.version += 1
        control.actor = actor
        control.updated_at = controls._now()
        result = {
            **release,
            "operation": proposal["operation"],
            "receipt_id": row.id,
            "task_id": row.task_id,
            "version": control.version,
            "stays_paused": row.task_paused,
            "delivery_accepted": False,
            "preview_sha256": proposal["preview_sha256"],
        }
        controls._audit(
            db,
            actor,
            ACTION,
            task_id=row.task_id,
            request=request,
            result=result,
            prior_hold=prior_hold,
            prior_authorization=prior_authorization,
        )
        return result
