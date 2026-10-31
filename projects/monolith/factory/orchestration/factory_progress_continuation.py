"""Explicit same-artifact continuation; finite leases, never reset accounting."""

import json
import re
from datetime import timedelta

import httpx
from core.jsonio import dumps
from sqlmodel import select

from factory.execution import provider_quota
from factory.execution.models import AgentSession, PendingMessage
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_funding_limits as funding
from factory.orchestration import graph, model_pool
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt, WorkItem

ACTION = "progress_continuation_authorized"
LEASE = "progress_continuation_lease"
RENEWAL = "progress_continuation_renewed"
REFUSAL = "progress_continuation_renewal_refused"
LEASE_SECONDS = 7200


def _compatible_direction(previous, current):
    """Only append staged holds with an already-bound scope and effects.

    Free text cannot establish new authority. Existing records stay byte-for-
    value identical, and additions may only restate a validated unmerged gate
    or require more checks. Every other direction field remains exact.
    """
    from factory.orchestration import factory_gates

    if previous == current:
        return True
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    before, after = (
        previous.get("conductor_gates", []),
        current.get("conductor_gates", []),
    )
    if (
        not isinstance(before, list)
        or not isinstance(after, list)
        or after[: len(before)] != before
        or {k: v for k, v in previous.items() if k != "conductor_gates"}
        != {k: v for k, v in current.items() if k != "conductor_gates"}
    ):
        return False

    def staged(gate):
        try:
            factory_gates.validate_gate(gate)
        except ValueError:
            return False
        return (
            gate.get("kind") == "live_validation"
            and gate.get("leave_unmerged") is True
            and not factory_gates.restricted(gate)
        )

    def boundary(gate):
        return {k: v for k, v in gate.items() if k not in {"reason", "live_checks"}}

    return all(
        staged(added)
        and any(
            staged(bound)
            and boundary(added) == boundary(bound)
            and set(bound["live_checks"]) <= set(added["live_checks"])
            for bound in before
        )
        for added in after[len(before) :]
    )


def _allocation_pause(db, row, last_lease):
    """An operator renewal may resolve only this factory-owned allocation pause."""
    from factory.orchestration import factory_pause

    card = json.loads(row.escalation_json or "{}")
    pause = db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.task_id == row.task_id, FactoryAudit.action == "pause_task")
        .order_by(FactoryAudit.id.desc())
    ).first()
    cause = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id == row.task_id,
            FactoryAudit.action == "nonrenewable_recovery_paused",
        )
        .order_by(FactoryAudit.id.desc())
    ).first()
    reason = json.loads(cause.detail_json).get("reason", "") if cause else ""
    if (
        pause is None
        or cause is None
        or pause.actor != "factory:nonrenewable-recovery"
        or cause.actor != pause.actor
        or cause.id <= pause.id
        or not json.loads(pause.detail_json).get("ok")
        or pause.id <= last_lease["audit_id"]
        or not factory_pause.is_card(card)
        or card.get("actor") != pause.actor
        or card.get("task_id") != row.task_id
        or card.get("resolved") is not None
        or card.get("options") != [factory_pause.STOP]
        or reason != card.get("reason", "")[:1000]
        or not reason.startswith("Correction plan exceeds current allocation:")
    ):
        raise ValueError("progress_pause_not_owned")
    return {"pause_audit_id": pause.id, "cause_audit_id": cause.id}


def _review(db, task_id):
    from factory.orchestration import factory_conductor as conductor

    runs = graph.node_runs(task_id, session=db)
    reviews = [r for r in runs if r["node_key"].startswith("review_")]
    if not reviews:
        raise ValueError("progress_review_missing")
    review = max(reviews, key=lambda r: r["id"])
    if review["status"] != "succeeded":
        raise ValueError("progress_review_not_succeeded")
    artifact = conductor._artifact(review)
    if artifact.get("verdict") not in {"approve", "changes_requested"}:
        raise ValueError("progress_review_invalid")
    row = controls._receipt(db, task_id)
    policy = funding.effective_policy(db, row)
    implementers = [
        r
        for r in runs
        if conductor._is_implementation(r["node_key"])
        and r["status"] == "succeeded"
        and conductor._artifact(r).get("pr_number") == artifact.get("pr_number")
    ]
    allowed = model_pool.pool_for("reviewer", policy)
    if row.task_class in conductor.JUDGMENT_CLASSES:
        allowed = [model for model in allowed if model in conductor.JUDGMENT_MODELS]
    reviewer_model = review.get("pin", {}).get("model")
    if (
        not implementers
        or not review.get("session_id")
        or any(
            not r.get("session_id") or r["session_id"] == review["session_id"]
            for r in implementers
        )
        or reviewer_model not in allowed
        or reviewer_model not in policy["allowed_models"]
        or review.get("head_sha") != artifact.get("head_sha")
    ):
        raise ValueError("progress_independent_review_missing")
    session = db.get(AgentSession, review["session_id"])
    if (
        session is None
        or session.local_session_id
        != f"factory:{task_id}:{review['node_key']}:{review['attempt']}"
        or session.model != reviewer_model
    ):
        raise ValueError("progress_review_provenance_missing")
    return review, artifact, runs


class SubscriptionRefusal(ValueError):
    """Admission refusal with bounded read-only evidence for the operator."""

    def __init__(self, reason, diagnostics):
        super().__init__(reason)
        self.diagnostics = diagnostics


def _subscription_diagnostics(policy, node_models, raw, quota, refused_model):
    origins = {}

    def add(model, origin):
        origins.setdefault(model, set()).add(origin)

    for model in policy["allowed_models"]:
        add(model, "allowed_models")
    for field in ("conductor_model", "worker_model", "reviewer_model"):
        add(policy.get(field), field)
    for role, pool in policy.get("model_pools", {}).items():
        for model in pool:
            add(model, "model_pools." + role)
    for model in node_models:
        add(model, "graph_pin")

    def describe(model):
        family = model_pool.family_for(model) if model else None
        provider = model_pool.QUOTA_PROVIDERS.get(family)
        view = quota.get(provider, {})
        observed = raw.get("providers", {}).get(provider, {})
        freshness = model_pool.grant_observation_state(view)
        if not model:
            state = "missing_model"
        elif provider not in {"codex", "claude"}:
            state = "unsupported_subscription_feed"
        elif view.get("observed") is not True:
            state = "missing_observation"
        elif freshness != "fresh":
            state = freshness + "_observation"
        elif not view.get("windows") or any(
            not w.get("usable") or w.get("used_percent", 100) >= 100
            for w in view["windows"]
        ):
            state = "subscription_unavailable"
        elif any(
            not model_pool.availability(model, quota, role)[0]
            for role in ("conductor", "worker", "reviewer")
        ):
            state = "role_admission_refused"
        else:
            state = "fresh_admissible"
        return {
            "model": model,
            "family": family,
            "provider": provider,
            "required_by": sorted(origins.get(model, ()))[:16],
            "state": state,
            "observed_at": observed.get("observed_at"),
            "age_seconds": view.get("age_seconds"),
        }

    models = sorted(origins, key=lambda model: str(model))
    return {
        "broker_available": raw.get("available") is True,
        "checked_at": controls._now().isoformat(),
        "refused_model": describe(refused_model),
        "models": [describe(model) for model in models[:32]],
        "model_count": len(models),
        "truncated": len(models) > 32,
        "effect": "read_only_refusal_no_admission",
    }


def _observe(receipt_id):
    from factory.orchestration import factory_conductor as conductor

    with controls._read_session() as db:
        row = db.get(FactoryReceipt, receipt_id)
        if row is None or not row.task_id:
            raise ValueError("progress_receipt_missing")
        task = conductor._task(row.task_id, session=db)
        review, artifact, _ = _review(db, row.task_id)
        policy = funding.effective_policy(db, row)
        revision = graph.current_version(row.task_id, session=db)
        node_models = [n["model"] for n in graph.load_graph(row.task_id, session=db)]
    issue = conductor.github_get(task["repo"], f"issues/{task['issue_number']}")
    if issue.get("state") != "open" or "human" in {
        label["name"].lower() for label in issue.get("labels", [])
    }:
        raise ValueError("progress_human_or_closed_work")
    evidence = conductor._review_recovery_evidence(
        task, review, expected_verdict=artifact["verdict"]
    )
    if evidence.get("state") != "ready":
        raise ValueError("progress_" + evidence.get("reason", "artifact_unavailable"))
    raw = provider_quota.fetch_provider_quota_sync(force=True)
    quota = provider_quota.summarise(raw.get("providers", {}))
    if raw.get("available") is not True:
        raise ValueError("progress_quota_unavailable")
    # Admission can select any allowed model, not just the scalar defaults.
    # Cover pools and existing node pins too; unknown/API-only lanes refuse.
    models = set(policy["allowed_models"])
    models.update(
        policy.get(field)
        for field in ("conductor_model", "worker_model", "reviewer_model")
    )
    for pool in policy.get("model_pools", {}).values():
        models.update(pool)
    models.update(node_models)
    for model in sorted(models, key=lambda value: str(value)):
        if not model:
            raise ValueError("progress_model_pin_missing")
        provider = model_pool.QUOTA_PROVIDERS.get(model_pool.family_for(model))
        view = quota.get(provider, {})
        if (
            provider not in {"codex", "claude"}
            or view.get("observed") is not True
            or model_pool.grant_observation_state(view) != "fresh"
        ):
            raise SubscriptionRefusal(
                "progress_subscription_evidence_stale",
                _subscription_diagnostics(policy, node_models, raw, quota, model),
            )
        if not view.get("windows") or any(
            not w.get("usable") or w.get("used_percent", 100) >= 100
            for w in view["windows"]
        ):
            raise SubscriptionRefusal(
                "progress_subscription_unavailable",
                _subscription_diagnostics(policy, node_models, raw, quota, model),
            )
        if any(
            not model_pool.availability(model, quota, role)[0]
            for role in ("conductor", "worker", "reviewer")
        ):
            raise SubscriptionRefusal(
                "progress_subscription_unavailable",
                _subscription_diagnostics(policy, node_models, raw, quota, model),
            )
    return {
        "review_id": review["id"],
        "artifact": artifact,
        "evidence": evidence,
        "quota": quota,
        "policy": policy,
        "graph_revision": revision,
        "observed_at": controls._now().isoformat(),
    }


def _preview(
    db,
    control,
    receipt_id,
    pr_number,
    head_sha,
    direction,
    observed,
    *,
    renewal=False,
    resume_paused=False,
):
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_decisions import decision_in_flight

    row = db.get(FactoryReceipt, receipt_id)
    if (
        row is None
        or row.state != "admitted"
        or row.cancellation_requested
        or control.state != "enabled"
        or row.task_paused != (not renewal or resume_paused)
    ):
        raise ValueError("progress_receipt_not_eligible")
    graph._lock_task(db, row.task_id)
    policy = funding.effective_policy(db, row)
    if (
        policy != observed["policy"]
        or graph.current_version(row.task_id, session=db) != observed["graph_revision"]
    ):
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
            or not leases
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
            or not _compatible_direction(
                last.get(
                    "direction_snapshot", authorization["proposal"]["prior_direction"]
                ),
                json.loads(row.direction_json or "{}"),
            )
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
    if renewal:
        result.update(
            renewal=True,
            resume_paused=resume_paused,
            authorization_audit_id=authorization["audit_id"],
            pause_binding=_allocation_pause(
                db, row, funding.latest(db, row.task_id, LEASE)
            )
            if resume_paused
            else None,
        )
    result["preview_sha256"] = controls.digest(result)
    return result


def _target(pr_number, head_sha):
    controls._integer(pr_number, "pr_number", 1, 2**31 - 1)
    if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
        raise ValueError("progress_head_invalid")


def preview(receipt_id, pr_number, head_sha, direction, *, renewal=False):
    _target(pr_number, head_sha)
    observed = _observe(receipt_id)
    with controls._locked_session() as (db, control):
        return _preview(
            db,
            control,
            receipt_id,
            pr_number,
            head_sha,
            direction,
            observed,
            renewal=renewal,
            resume_paused=renewal,
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
        "direction_snapshot": proposal["prior_direction"],
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
    renewal=False,
):
    _target(pr_number, head_sha)
    request = {
        "receipt_id": receipt_id,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "direction": direction,
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    actor = controls._text(actor, "actor")
    action = RENEWAL if renewal else ACTION
    with controls._read_session() as db:
        replay = _replay(db, actor, request, action=action)
        if replay:
            return replay
    observed = _observe(receipt_id)
    with controls._locked_session() as (db, control):
        replay = _replay(db, actor, request, action=action)
        if replay:
            return replay
        proposal = _preview(
            db,
            control,
            receipt_id,
            pr_number,
            head_sha,
            direction,
            observed,
            renewal=renewal,
            resume_paused=renewal,
        )
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("progress_preview_changed")
        row = db.get(FactoryReceipt, receipt_id)
        lease = _lease(db, row, proposal, actor, observed)
        escalation = proposal["prior_escalation"].copy()
        escalation["resolved"] = {
            "actor": actor,
            "effect": action,
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
            action,
            task_id=row.task_id,
            request=request,
            proposal=proposal,
            result=result,
        )
        return result


def _replay(db, actor, request, *, action=ACTION):
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.actor == actor, FactoryAudit.action == action
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
    binding = None
    try:
        with controls._read_session() as db:
            row = controls._receipt(db, task_id)
            authorization = funding.latest(db, task_id, ACTION)
            last = funding.latest(db, task_id, LEASE)
            if (
                not authorization
                or not last
                or row.task_paused
                or row.state != "admitted"
            ):
                return False
            binding = {
                "authorization_audit_id": authorization["audit_id"],
                "lease_audit_id": last["audit_id"],
            }
            review, artifact, _ = _review(db, task_id)
            if review["id"] == last["review_id"]:
                return False
            binding.update(review_id=review["id"], head_sha=artifact.get("head_sha"))
            receipt_id = row.id
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
    except ValueError as exc:
        # No observation or rejected evidence is authority to renew a lease.
        if binding is not None:
            _record_refusal(task_id, binding, exc)
        return False
    except httpx.HTTPError:
        if binding is not None:
            _record_refusal(
                task_id, binding, ValueError("progress_observation_unavailable")
            )
        return False


def _record_refusal(task_id, binding, exc):
    reason = str(exc)[:300]
    diagnostics = getattr(exc, "diagnostics", None)
    key = {**binding, "reason": reason}
    if diagnostics:
        refused = diagnostics["refused_model"]
        key["subscription"] = {k: refused[k] for k in ("model", "state")}
    with controls._locked_session() as (db, _control):
        last = funding.latest(db, task_id, REFUSAL)
        if last and last["key"] == key:
            return
        controls._audit(
            db,
            "factory:progress-continuation",
            REFUSAL,
            task_id=task_id,
            key=key,
            reason=reason,
            **binding,
            subscription_diagnostics=diagnostics,
        )


def renewal_status(db, task_id):
    """Bounded last-attempt evidence, not a claim of current admission eligibility."""
    if not task_id:
        return None
    event = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id == task_id, FactoryAudit.action.in_((LEASE, REFUSAL))
        )
        .order_by(FactoryAudit.id.desc())
    ).first()
    if event is None:
        return None
    detail = json.loads(event.detail_json)
    return {
        "state": "refused" if event.action == REFUSAL else "leased",
        "audit_id": event.id,
        "observed_at": str(event.created_at),
        "coverage": "last_recorded_attempt",
        **{
            k: detail[k]
            for k in (
                "reason",
                "head_sha",
                "review_id",
                "authorization_audit_id",
                "lease_audit_id",
                "deadline_at",
                "work_turn_ceiling",
                "planner_turn_ceiling",
                "subscription_diagnostics",
            )
            if k in detail
        },
    }
