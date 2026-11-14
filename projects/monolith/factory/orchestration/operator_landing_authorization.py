"""One explicit, expiring human decision for an otherwise ineligible delivery.

This is not standing delegation and cannot install or authorize itself. The
existing human-only MCP surface supplies the actor. Git proof, native review,
CI and receipt state are server observations; only the risk acknowledgement,
reason, rollback plan and absolute expiry come from the operator.
"""

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import httpx
import jsonschema
from sqlmodel import select

from core.clock import aware
from core.jsonio import dumps
from factory.execution.models import AgentSession, PendingMessage
from factory.orchestration import factory_controls as controls, graph
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
    WorkItem,
)
from factory.orchestration.models import SwarmNodeRun, SwarmTask

CLASSIFICATION = "requires_operator"
EFFECTS = (
    "public_facing",
    "security_sensitive",
    "authentication_or_credentials",
    "destructive",
)
MAX_DURATION = timedelta(hours=24)
REVOKE_ACTION = "operator_landing_authorization_revoked"
ARM_ACTION = "operator_landing_arm_reserved"
QUEUE_ACTION = "operator_landing_queue_observed"


def _require(condition, reason):
    if not condition:
        raise ValueError("operator_landing_" + reason)


def _utc(value):
    _require(isinstance(value, str) and len(value) <= 40, "expiry_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("operator_landing_expiry_invalid") from exc
    _require(
        parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0),
        "expiry_invalid",
    )
    return parsed.astimezone(timezone.utc)


def _iso(value):
    if value is None:
        return None
    value = aware(value)
    return value.astimezone(timezone.utc).isoformat()


def scope(value):
    """Validate consent without consulting the clock, so old replay is an ACK."""
    _require(
        isinstance(value, dict)
        and set(value)
        == {
            "classification",
            "evidence",
            "acknowledged_effects",
            "rollback_plan",
            "expires_at",
        }
        and value.get("classification") == CLASSIFICATION,
        "scope_invalid",
    )
    effects = value["acknowledged_effects"]
    _require(
        isinstance(effects, dict)
        and set(effects) == set(EFFECTS)
        and all(type(effects[key]) is bool for key in EFFECTS),
        "effects_invalid",
    )
    return {
        "classification": CLASSIFICATION,
        "evidence": controls._text(value["evidence"], "scope evidence", 4000),
        "acknowledged_effects": {key: effects[key] for key in EFFECTS},
        "rollback_plan": controls._text(value["rollback_plan"], "rollback_plan", 4000),
        "expires_at": _utc(value["expires_at"]).isoformat(),
    }


def _expiry(value, *, authorized_at=None):
    expiry = _utc(value["expires_at"])
    now = controls._now()
    _require(now < expiry <= now + MAX_DURATION, "expiry_out_of_bounds")
    if authorized_at is not None:
        issued = _utc(authorized_at)
        _require(
            issued <= now and issued < expiry <= issued + MAX_DURATION, "expiry_invalid"
        )


def _review_scope(artifact):
    from factory.orchestration.repair_landing_delegation import SCOPE_SCHEMA

    value = artifact.get("landing_scope")
    try:
        jsonschema.validate(value, SCOPE_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ValueError("operator_landing_review_scope_missing_or_invalid") from exc
    return value


def refuse_private_downgrade(db, task_id, head_sha):
    """A typed security assessment cannot be replaced by a private attestation.

    Truly old, untyped ordinary reviews retain their existing route. A typed
    artifact that is present but malformed fails closed instead of silently
    becoming an untyped review.
    """
    from factory import review_publisher

    runs = db.exec(
        select(SwarmNodeRun)
        .where(
            SwarmNodeRun.task_id == task_id,
            SwarmNodeRun.node_key.startswith("review_", autoescape=True),
            SwarmNodeRun.head_sha == head_sha,
        )
        .order_by(SwarmNodeRun.id.desc())
    ).all()
    if not runs:
        return
    for run in runs:
        try:
            outcome = json.loads(run.outcome_json or "{}")
            value = outcome.get("value", outcome.get("artifact", {}).get("value", {}))
            if not isinstance(value, dict) or "landing_scope" not in value:
                continue
            validation, artifact = review_publisher._artifact(run)
            _require(validation == "ok", "review_invalid")
            assessment = _review_scope(artifact)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(
                "operator_landing_review_scope_missing_or_invalid"
            ) from exc
        # These are the same immutable head bytes. A later private or untyped
        # assessment cannot erase a typed operator requirement. The explicit
        # operator route resolves that decision without downgrading it.
        _require(
            assessment["classification"] != CLASSIFICATION
            and not any(assessment[key] for key in EFFECTS),
            "private_scope_downgrade",
        )


def _review(db, row, pull, consent, git_proof):
    from factory import review_publisher

    review = review_publisher.collect(row.task_id, session=db)
    _require(
        isinstance(review, review_publisher.ReviewEvidence)
        and (review.repo, review.pr_number, review.head_sha)
        == (row.repo, pull["number"], pull["head"]["sha"]),
        "independent_review_missing",
    )
    run = db.get(SwarmNodeRun, review.review_run_id)
    _require(run is not None, "review_invalid")
    validation, artifact = review_publisher._artifact(run)
    _require(validation == "ok", "review_invalid")
    assessment = _review_scope(artifact)
    _require(
        assessment["base_sha"] == git_proof["merge_base_sha"]
        and sorted(assessment["paths"])
        == [entry["path"] for entry in git_proof["files"]],
        "review_scope_changed",
    )
    _require(
        all(
            not assessment[key] or consent["acknowledged_effects"][key]
            for key in EFFECTS
        ),
        "effect_not_acknowledged",
    )
    return {
        "identity": {
            key: value
            for key, value in asdict(review).items()
            if key != "published_check_id"
        },
        "artifact_sha256": controls.digest(artifact),
        "scope": assessment,
    }


def _ci(repo, head):
    from factory.orchestration.factory_conductor import (
        github_get,
        _required_checks_passed,
    )

    # This endpoint returns each context's latest status. A truncated first page
    # cannot prove that every non-advisory context passed.
    checks = github_get(repo, f"commits/{head}/status?per_page=100")
    statuses = checks.get("statuses")
    _require(
        checks.get("sha") == head
        and isinstance(statuses, list)
        and type(checks.get("total_count")) is int
        and 0 < checks["total_count"] == len(statuses) <= 100
        and all(
            isinstance(item, dict)
            and isinstance(item.get("context"), str)
            and 0 < len(item["context"]) <= 256
            and type(item.get("id")) is int
            and item["id"] > 0
            and item.get("state") in {"success", "error", "failure", "pending"}
            for item in statuses
        )
        and len({item["context"] for item in statuses}) == len(statuses)
        and _required_checks_passed(checks),
        "ci_unverified",
    )
    return {
        "head_sha": head,
        "statuses": sorted(
            [
                {
                    key: item.get(key)
                    for key in ("id", "context", "state", "target_url", "updated_at")
                }
                for item in statuses
            ],
            key=lambda item: item["context"],
        ),
    }


def binding(db, control, row):
    """Bind durable state without binding our own grant bytes or audit writes."""
    from factory.orchestration import factory_landing_release as releases

    direction = json.loads(row.direction_json or "{}")
    direction.pop("landing_authorization", None)
    transition = releases.latest_policy_transition(db)
    starts = db.exec(
        select(FactoryStart)
        .where(FactoryStart.task_id == row.task_id)
        .order_by(FactoryStart.id)
    ).all()
    task = db.get(SwarmTask, row.task_id)
    work_item = db.get(WorkItem, row.work_item_id) if row.work_item_id else None
    return {
        "generation": row.generation,
        "task_class": row.task_class,
        "receipt_state": row.state,
        "receipt_updated_at": _iso(row.updated_at),
        "receipt_body_sha256": controls.digest(row.body),
        "work_item_sha256": controls.digest(
            work_item.model_dump(mode="json") if work_item else None
        ),
        "task_sha256": controls.digest(task.model_dump(mode="json") if task else None),
        "direction_sha256": controls.digest(direction),
        "policy_sha256": controls.digest(controls._effective_policy(db, row)),
        "control_state": control.state,
        "control_policy_sha256": controls.digest(json.loads(control.policy_json)),
        "policy_transition_id": transition.id if transition else None,
        "graph_revision": graph.current_version(row.task_id, session=db),
        "starts_sha256": controls.digest(
            [start.model_dump(mode="json") for start in starts]
        ),
    }


def preview_evidence(db, control, row, pull, consent, git_proof, actor):
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_funding_limits as funding
    from factory.orchestration import operator_landing_evidence, owner_prs

    controls._text(actor, "actor")
    _expiry(consent)
    _require(
        not funding.routine_hold(db, row, row.task_id)
        and not funding._recovery_held(db, row.task_id)
        and funding.pending(db, row.task_id) is None
        and db.exec(
            select(PendingMessage.id)
            .join(AgentSession)
            .where(
                AgentSession.local_session_id.startswith(
                    f"factory:{row.task_id}:", autoescape=True
                )
            )
            .limit(1)
        ).first()
        is None,
        "receipt_held",
    )
    _require(isinstance(git_proof, dict), "evidence_missing")
    current = conductor.github_get(row.repo, f"pulls/{pull['number']}")
    direction = json.loads(row.direction_json or "{}")
    identity = {
        **direction,
        "id": row.task_id,
        "repo": row.repo,
        "base_branch": controls._effective_policy(db, row)["base_branch"],
    }
    _require(
        current.get("number") == pull["number"]
        and current.get("head", {}).get("sha") == pull["head"]["sha"]
        and current.get("state") == "open"
        and not current.get("draft")
        and not current.get("merged")
        and current.get("body") == pull.get("body")
        and conductor.delivery_identity_matches(identity, current),
        "pull_changed",
    )
    operator_landing_evidence.confirm(current, git_proof)
    owner_prs.assert_unheld({**identity, "delivery_pr_number": pull["number"]}, current)
    return {
        "git": git_proof,
        "review": _review(db, row, current, consent, git_proof),
        "ci": _ci(row.repo, current["head"]["sha"]),
        "binding": binding(db, control, row),
    }


def _audit_for_grant(db, row, grant):
    from factory.orchestration import factory_landing_release as releases

    audit_id = grant.get("authorization_audit_id")
    _require(type(audit_id) is int and audit_id > 0, "grant_unaudited")
    event = db.get(FactoryAudit, audit_id)
    _require(
        event is not None
        and event.action == releases.SCOPE_ACTION
        and event.task_id == row.task_id
        and event.actor == grant.get("actor"),
        "grant_unaudited",
    )
    detail = controls.audit_detail(event)
    request, result = detail.get("request", {}), detail.get("result", {})
    _require(
        isinstance(request, dict)
        and isinstance(result, dict)
        and result.get("ok") is True
        and result.get("landing_authorization") == grant
        and all(
            request.get(key) == grant.get(key)
            for key in (
                "receipt_id",
                "pr_number",
                "head_sha",
                "scope",
                "request_key",
                "reason",
            )
        )
        and result.get("authorization_audit_id") == audit_id,
        "grant_unaudited",
    )
    return event


def _revoked(db, grant):
    events = db.exec(
        select(FactoryAudit).where(
            FactoryAudit.action == REVOKE_ACTION,
            FactoryAudit.task_id == grant["task_id"],
        )
    ).all()
    return any(
        controls.audit_detail(event).get("result", {}).get("ok") is True
        and controls.audit_detail(event).get("result", {}).get("authorization_audit_id")
        == grant["authorization_audit_id"]
        for event in events
    )


def refusal(db, row, number, head, grant):
    """Recheck audited authority on both final arming and queued observations."""
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_landing_release as releases

    try:
        if grant.get("revocation") is not None or _revoked(db, grant):
            return "operator_landing_grant_revoked"
        _audit_for_grant(db, row, grant)
        _require(
            grant.get("schema_version") == 2
            and all(
                grant.get(key) == value
                for key, value in {
                    "receipt_id": row.id,
                    "task_id": row.task_id,
                    "repo": row.repo,
                    "pr_number": number,
                    "head_sha": head,
                }.items()
            ),
            "grant_identity_changed",
        )
        consent = scope(grant["scope"])
        _require(consent == grant["scope"], "scope_invalid")
        _expiry(consent, authorized_at=grant["authorized_at"])
        control = db.get(FactoryControl, "factory")
        pull = conductor.github_get(row.repo, f"pulls/{number}")
        proposal = releases._preview(
            db,
            control,
            row.id,
            number,
            head,
            grant["reason"],
            pull,
            scope=consent,
            actor=grant["actor"],
            operator_git_evidence=grant["operator_evidence"]["git"],
        )
        _require(
            proposal["operator_evidence"] == grant["operator_evidence"],
            "evidence_changed",
        )
    except httpx.HTTPError:
        return "operator_landing_observation_unavailable"
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        message = str(exc)
        return (
            message
            if isinstance(exc, ValueError)
            and re.fullmatch(
                r"(?:operator_landing|landing_release|scoped_landing|review_only_recovery)[a-z_]*",
                message,
            )
            else "operator_landing_grant_invalid"
        )
    return None


def revoke(
    receipt_id,
    pr_number,
    head_sha,
    reason,
    *,
    actor,
    request_key,
    authorization_audit_id,
):
    """Revoke this audit only; a replay never changes a replacement grant."""
    actor = controls._text(actor, "actor")
    _require(
        type(authorization_audit_id) is int and authorization_audit_id > 0,
        "authorization_id_invalid",
    )
    request = {
        "receipt_id": receipt_id,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "reason": controls._text(reason, "reason", 4000),
        "request_key": controls._text(request_key, "request_key"),
        "authorization_audit_id": authorization_audit_id,
    }
    with controls._locked_session() as (db, control):
        for event in db.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == REVOKE_ACTION, FactoryAudit.actor == actor
            )
        ).all():
            detail = controls.audit_detail(event)
            if detail.get("request", {}).get("request_key") == request_key:
                _require(detail["request"] == request, "conflicting_revoke_request")
                return detail["result"]
        row = db.get(FactoryReceipt, receipt_id)
        _require(row is not None and row.task_id, "receipt_missing")
        graph._lock_task(db, row.task_id)
        direction = json.loads(row.direction_json or "{}")
        grant = direction.get("landing_authorization")
        _require(
            isinstance(grant, dict)
            and grant.get("schema_version") == 2
            and (
                grant.get("authorization_audit_id"),
                grant.get("pr_number"),
                grant.get("head_sha"),
            )
            == (authorization_audit_id, pr_number, head_sha),
            "revoke_target_changed",
        )
        if grant.get("revocation") is None:
            _audit_for_grant(db, row, grant)
        else:
            _require(_revoked(db, grant), "grant_unaudited")
        result = {
            "ok": True,
            "operation": "revoke",
            "receipt_id": row.id,
            "task_id": row.task_id,
            "authorization_audit_id": authorization_audit_id,
            "delivery_accepted": False,
        }
        tombstone = {
            "actor": actor,
            "request_key": request_key,
            "reason": request["reason"],
            "revoked_at": controls._now().isoformat(),
        }
        direction["landing_authorization"] = {**grant, "revocation": tombstone}
        row.direction_json = dumps(direction)
        row.updated_at = controls._now()
        control.version += 1
        control.actor = actor
        control.updated_at = controls._now()
        result["version"] = control.version
        controls._audit(
            db,
            actor,
            REVOKE_ACTION,
            task_id=row.task_id,
            request=request,
            result=result,
            revocation=tombstone,
        )
        return result


def _grant(row):
    grant = (
        json.loads(row.direction_json or "{}").get("landing_authorization")
        if row
        else None
    )
    return (
        grant if isinstance(grant, dict) and grant.get("schema_version") == 2 else None
    )


def _attempts(db, grant):
    return [
        event
        for event in db.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == ARM_ACTION,
                FactoryAudit.task_id == grant["task_id"],
            )
        ).all()
        if controls.audit_detail(event).get("authorization_audit_id")
        == grant["authorization_audit_id"]
    ]


def reserve_arm(task_id, number, head, node_id):
    """Commit one attempt before I/O; unknown network outcomes cannot retry it."""
    from factory.orchestration import factory_landing_release as releases

    with controls._locked_session() as (db, control):
        row = controls._receipt(db, task_id)
        grant = _grant(row)
        if grant is None:
            return None
        refusal_reason = releases.scoped_refusal(db, row, number, head)
        _require(not refusal_reason, "arm_refused:" + (refusal_reason or ""))
        _require(
            control.state == "enabled"
            and controls.repo_auto_merge_enabled(
                json.loads(control.policy_json), row.repo
            ),
            "automatic_landing_disabled",
        )
        _require(not _attempts(db, grant), "arming_attempt_consumed")
        event = FactoryAudit(
            actor="factory:landing",
            action=ARM_ACTION,
            task_id=task_id,
            detail_json=dumps(
                {
                    "authorization_audit_id": grant["authorization_audit_id"],
                    "pr_number": number,
                    "head_sha": head,
                    "node_id": node_id,
                }
            ),
        )
        db.add(event)
        db.flush()
        return {
            "authorization_audit_id": grant["authorization_audit_id"],
            "attempt_audit_id": event.id,
        }


def arm_refusal(db, row, number, head, node_id, reservation):
    grant = _grant(row)
    if grant is None:
        return "operator_landing_grant_changed" if reservation is not None else None
    try:
        _expiry(grant["scope"], authorized_at=grant["authorized_at"])
    except (KeyError, TypeError, ValueError):
        return "operator_landing_expiry_out_of_bounds"
    attempts = _attempts(db, grant)
    if (
        not isinstance(reservation, dict)
        or reservation.get("authorization_audit_id") != grant["authorization_audit_id"]
        or len(attempts) != 1
        or attempts[0].id != reservation.get("attempt_audit_id")
        or controls.audit_detail(attempts[0])
        != {
            "authorization_audit_id": grant["authorization_audit_id"],
            "pr_number": number,
            "head_sha": head,
            "node_id": node_id,
        }
    ):
        return "operator_landing_arming_attempt_invalid"
    return None


def queue_refusal(db, row, pull):
    """Bind the first observed queue entry; the observer is not a merge lock."""
    from factory.orchestration import factory_landing as landing

    grant = _grant(row)
    if grant is None:
        return None
    attempts = _attempts(db, grant)
    if len(attempts) != 1:
        return "operator_landing_arming_attempt_missing"
    attempt = controls.audit_detail(attempts[0])
    if (attempt.get("head_sha"), attempt.get("pr_number"), attempt.get("node_id")) != (
        pull["head"]["sha"],
        pull["number"],
        pull.get("node_id"),
    ):
        return "operator_landing_arming_attempt_invalid"
    response = landing.github_graphql(
        landing._QUEUE_ENTRIES, {"ids": [pull["node_id"]]}
    )
    nodes = response.get("nodes")
    if (
        not isinstance(nodes, list)
        or len(nodes) != 1
        or not isinstance(nodes[0], dict)
        or nodes[0].get("id") != pull["node_id"]
        or "mergeQueueEntry" not in nodes[0]
    ):
        return "operator_landing_queue_unverified"
    entry = nodes[0]["mergeQueueEntry"]
    if entry is not None and (
        not isinstance(entry, dict)
        or not isinstance(entry.get("id"), str)
        or not entry["id"]
    ):
        return "operator_landing_queue_unverified"
    prior = [
        controls.audit_detail(event)
        for event in db.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == QUEUE_ACTION, FactoryAudit.task_id == row.task_id
            )
        ).all()
        if controls.audit_detail(event).get("authorization_audit_id")
        == grant["authorization_audit_id"]
    ]
    if prior:
        if entry is None or any(
            event.get("queue_entry_id") != entry["id"] for event in prior
        ):
            return "operator_landing_queue_admission_consumed"
    elif entry is not None:
        controls._audit(
            db,
            "factory:landing",
            QUEUE_ACTION,
            task_id=row.task_id,
            authorization_audit_id=grant["authorization_audit_id"],
            attempt_audit_id=attempts[0].id,
            queue_entry_id=entry["id"],
        )
    return None
