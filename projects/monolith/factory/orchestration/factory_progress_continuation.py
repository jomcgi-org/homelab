"""Explicit same-artifact continuation; finite leases, never reset accounting."""

from datetime import timedelta
import hashlib
import json

from sqlmodel import select

from core.jsonio import dumps
from factory.execution import provider_quota
from factory.orchestration import factory_controls as controls, graph, model_pool
from factory.orchestration import factory_funding_limits as funding
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt, WorkItem
from factory.execution.models import AgentSession, PendingMessage

ACTION = "progress_continuation_authorized"
LEASE = "progress_continuation_lease"
LEASE_SECONDS = 7200


def _digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def _review(db, task_id):
    from factory.orchestration import factory_conductor as conductor

    runs = graph.node_runs(task_id, session=db)
    reviews = [
        r
        for r in runs
        if r["node_key"].startswith("review_") and r["status"] == "succeeded"
    ]
    if not reviews:
        raise ValueError("progress_review_missing")
    review = max(reviews, key=lambda r: r["id"])
    artifact = conductor._artifact(review)
    if artifact.get("verdict") not in {"approve", "changes_requested"}:
        raise ValueError("progress_review_invalid")
    return review, artifact, runs


def _observe(receipt_id):
    from factory.orchestration import factory_conductor as conductor

    with controls._read_session() as db:
        row = db.get(FactoryReceipt, receipt_id)
        if row is None or not row.task_id:
            raise ValueError("progress_receipt_missing")
        task = conductor._task(row.task_id, session=db)
        review, artifact, _ = _review(db, row.task_id)
        policy = funding.effective_policy(db, row)
    evidence = conductor._review_recovery_evidence(
        task, review, expected_verdict=artifact["verdict"]
    )
    if evidence.get("state") != "ready":
        raise ValueError("progress_" + evidence.get("reason", "artifact_unavailable"))
    raw = provider_quota.fetch_provider_quota_sync(force=True)
    quota = provider_quota.summarise(raw.get("providers", {}))
    if raw.get("available") is not True:
        raise ValueError("progress_quota_unavailable")
    for role, field in (
        ("conductor", "conductor_model"),
        ("worker", "worker_model"),
        ("reviewer", "reviewer_model"),
    ):
        model = policy.get(field)
        if not model:
            raise ValueError("progress_model_pin_missing")
        provider = model_pool.QUOTA_PROVIDERS.get(model_pool.family_for(model))
        view = quota.get(provider, {})
        if (
            provider not in {"codex", "claude"}
            or view.get("observed") is not True
            or model_pool.grant_observation_state(view) != "fresh"
        ):
            raise ValueError("progress_subscription_evidence_stale")
        if not view.get("windows") or any(
            not w.get("usable") or w.get("used_percent", 100) >= 100
            for w in view["windows"]
        ):
            raise ValueError("progress_subscription_unavailable")
        if not model_pool.availability(model, quota, role)[0]:
            raise ValueError("progress_subscription_unavailable")
    return {
        "review_id": review["id"],
        "artifact": artifact,
        "evidence": evidence,
        "quota": quota,
        "policy": policy,
        "observed_at": controls._now().isoformat(),
    }


def _preview(
    db, control, receipt_id, pr_number, head_sha, direction, observed, *, renewal=False
):
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_decisions import decision_in_flight

    row = db.get(FactoryReceipt, receipt_id)
    if (
        row is None
        or row.state != "admitted"
        or row.cancellation_requested
        or control.state != "enabled"
        or (row.task_paused == renewal)
    ):
        raise ValueError("progress_receipt_not_eligible")
    graph._lock_task(db, row.task_id)
    policy = funding.effective_policy(db, row)
    if policy != observed["policy"]:
        raise ValueError("progress_policy_changed")
    item = db.get(WorkItem, row.work_item_id) if row.work_item_id else None
    if item and (
        item.state == "closed" or "human" in {label.lower() for label in item.labels}
    ):
        raise ValueError("progress_human_or_closed_work")
    if row.generation != json.loads(control.policy_json).get("generation", 0):
        raise ValueError("progress_generation_changed")
    if not controls.landing_held(row) or controls.review_recovery_held(
        row.task_id, session=db
    ):
        raise ValueError("progress_hold_refused")
    refusal = controls._quiescence_refusal(db, row.task_id)
    if refusal or decision_in_flight(db, row.id):
        raise ValueError("progress_execution_unresolved")
    if (
        db.exec(
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
        raise ValueError("progress_pending_execution")
    review, artifact, runs = _review(db, row.task_id)
    if (
        review["id"] != observed["review_id"]
        or artifact != observed["artifact"]
        or artifact.get("head_sha") != head_sha
        or artifact.get("pr_number") != pr_number
        or observed["evidence"].get("head_sha") != head_sha
        or observed["evidence"].get("pr_number") != pr_number
    ):
        raise ValueError("progress_artifact_changed")
    from datetime import datetime

    if (
        controls._now() - datetime.fromisoformat(observed["observed_at"])
    ).total_seconds() > 30:
        raise ValueError("progress_observation_expired")
    original = funding.latest(db, row.task_id, controls.TASK_RECOVERY_ACTION)
    if original is None:
        raise ValueError("progress_original_recovery_missing")
    authorization = funding.latest(db, row.task_id, ACTION)
    leases = [
        json.loads(raw)
        for raw in db.exec(
            select(FactoryAudit.detail_json)
            .where(FactoryAudit.task_id == row.task_id, FactoryAudit.action == LEASE)
            .order_by(FactoryAudit.id)
        ).all()
    ]
    if renewal:
        if (
            not authorization
            or authorization["proposal"]["original_recovery"] != original
            or authorization["proposal"]["pr_number"] != pr_number
        ):
            raise ValueError("progress_authority_changed")
        if head_sha in {lease["head_sha"] for lease in leases}:
            raise ValueError("progress_no_new_reviewed_head")
        last = leases[-1]
        current_funding = funding.amendment(db, row.task_id)
        if (
            not current_funding
            or current_funding.get("grant_kind") != LEASE
            or json.loads(row.direction_json or "{}")
            != authorization["proposal"]["prior_direction"]
        ):
            raise ValueError("progress_authority_changed")
        if (
            artifact["verdict"] != "approve"
            and last["verdict"] == "changes_requested"
            and last.get("consecutive_failures", 0) >= 1
        ):
            raise ValueError("progress_repeated_review_failure")
        if any(
            r["status"] in {"failed", "escalated"} and r["id"] > last["review_id"]
            for r in runs
        ):
            raise ValueError("progress_execution_failed")
    elif authorization:
        raise ValueError("progress_already_authorized")
    if controls.delivery_branch_owner(
        db,
        row.repo,
        conductor.delivery_branch(conductor._task(row.task_id, session=db)),
        exclude_receipt_id=row.id,
    ):
        raise ValueError("progress_artifact_owned_elsewhere")
    if not controls.execution_capacity_available(db, control, row):
        raise ValueError("progress_capacity_unavailable")
    spent = controls._accounting(controls._starts(db, row.task_id))
    result = {
        "receipt_id": row.id,
        "task_id": row.task_id,
        "repo": row.repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "review_id": review["id"],
        "verdict": artifact["verdict"],
        "expected_version": control.version,
        "expected_revision": graph.current_version(row.task_id, session=db),
        "original_recovery": original,
        "funding": funding.amendment(db, row.task_id),
        "prior_direction": json.loads(row.direction_json or "{}"),
        "prior_escalation": json.loads(row.escalation_json or "{}"),
        "policy": policy,
        "spent": spent,
        "direction": controls._text(direction, "direction", 4000),
        "lease_seconds": LEASE_SECONDS,
        "work_turn_ceiling": max(
            controls.task_turn_ceiling(policy), spent["turns_used"] + 2
        ),
        "planner_turn_ceiling": max(
            controls.planner_turn_cap(policy), spent["planner_turns_used"] + 2
        ),
        "previous_lease": leases[-1] if leases else None,
    }
    result["preview_sha256"] = _digest(result)
    return result


def preview(receipt_id, pr_number, head_sha, direction):
    observed = _observe(receipt_id)
    with controls._locked_session() as (db, control):
        return _preview(
            db, control, receipt_id, pr_number, head_sha, direction, observed
        )


def _lease(db, row, proposal, actor, observed):
    deadline = (controls._now() + timedelta(seconds=LEASE_SECONDS)).isoformat()
    prior = proposal["previous_lease"]
    lease = {
        "original_recovery_audit_id": proposal["original_recovery"]["audit_id"],
        "pr_number": proposal["pr_number"],
        "head_sha": proposal["head_sha"],
        "review_id": proposal["review_id"],
        "verdict": proposal["verdict"],
        "consecutive_failures": (
            prior.get("consecutive_failures", 0) + 1
            if prior and proposal["verdict"] == "changes_requested"
            else 0
        ),
        "deadline_at": deadline,
        "work_turn_ceiling": proposal["work_turn_ceiling"],
        "planner_turn_ceiling": proposal["planner_turn_ceiling"],
    }
    controls._audit(
        db,
        actor,
        LEASE,
        task_id=row.task_id,
        **lease,
        subscription_evidence=observed["quota"],
        observed_at=observed["observed_at"],
    )
    funding_grant = proposal["funding"] or {}
    controls._audit(
        db,
        actor,
        "funding_granted",
        task_id=row.task_id,
        grant_kind=LEASE,
        policy_overlay={
            **funding_grant.get("policy_overlay", {}),
            "max_task_turns_hard": lease["work_turn_ceiling"],
            "max_planner_turns": lease["planner_turn_ceiling"],
        },
        deadline_at=deadline,
        review_due_at=deadline,
        next_plan=proposal["direction"],
        reason="Continue the same reviewed artifact within an independently admitted finite lease.",
    )
    return lease


def apply(
    receipt_id,
    pr_number,
    head_sha,
    direction,
    *,
    actor,
    request_key,
    expected_preview_sha256,
):
    request = {
        "receipt_id": receipt_id,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "direction": direction,
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    actor = controls._text(actor, "actor")
    with controls._read_session() as db:
        replay = _replay(db, actor, request)
        if replay:
            return replay
    observed = _observe(receipt_id)
    with controls._locked_session() as (db, control):
        replay = _replay(db, actor, request)
        if replay:
            return replay
        proposal = _preview(
            db, control, receipt_id, pr_number, head_sha, direction, observed
        )
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("progress_preview_changed")
        row = db.get(FactoryReceipt, receipt_id)
        lease = _lease(db, row, proposal, actor, observed)
        escalation = proposal["prior_escalation"].copy()
        escalation["resolved"] = {
            "actor": actor,
            "effect": ACTION,
            "request_key": request_key,
            "at": controls._now().isoformat(),
        }
        row.escalation_json = dumps(escalation)
        row.task_paused = False
        row.updated_at = controls._now()
        control.version += 1
        control.actor, control.updated_at = actor, controls._now()
        result = {
            "ok": True,
            "receipt_id": row.id,
            "task_id": row.task_id,
            "version": control.version,
            "lease": lease,
            "landing_hold": True,
        }
        controls._audit(
            db,
            actor,
            ACTION,
            task_id=row.task_id,
            request=request,
            proposal=proposal,
            result=result,
        )
        return result


def _replay(db, actor, request):
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.actor == actor, FactoryAudit.action == ACTION
        )
    ).all():
        previous = json.loads(raw)
        if previous["request"]["request_key"] == request["request_key"]:
            if previous["request"] != request:
                raise ValueError("progress_request_conflict")
            return previous["result"]
    return None


def renew(task_id):
    """Called only after native execution reconciliation, before new starts."""
    with controls._read_session() as db:
        row = controls._receipt(db, task_id)
        authorization = funding.latest(db, task_id, ACTION)
        last = funding.latest(db, task_id, LEASE)
        if not authorization or not last or row.task_paused or row.state != "admitted":
            return False
        review, artifact, _ = _review(db, task_id)
        if review["id"] == last["review_id"]:
            return False
        receipt_id = row.id
    try:
        observed = _observe(receipt_id)
        with controls._locked_session() as (db, control):
            proposal = _preview(
                db,
                control,
                receipt_id,
                artifact["pr_number"],
                artifact["head_sha"],
                authorization["proposal"]["direction"],
                observed,
                renewal=True,
            )
            row = db.get(FactoryReceipt, receipt_id)
            _lease(db, row, proposal, "factory:progress-continuation", observed)
            control.version += 1
            control.actor, control.updated_at = (
                "factory:progress-continuation",
                controls._now(),
            )
        return True
    except ValueError:
        # No observation or rejected evidence is authority to renew a lease.
        return False
