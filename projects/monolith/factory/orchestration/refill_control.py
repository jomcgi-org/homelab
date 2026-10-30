"""Narrow, audited refill activation and existing learning-job cadence control."""

import json
from datetime import datetime, timedelta

from core.clock import aware
from core.jsonio import dumps
from sqlalchemy import inspect, text
from sqlmodel import select

from factory.execution import admission, offered_models, provider_quota
from factory.execution.models import (
    AgentCapacityReservation,
    AgentSession,
    PendingMessage,
)
from factory.execution.review_leases import digest
from factory.orchestration import backlog_refill as refill
from factory.orchestration import factory_controls as controls
from factory.orchestration import learning, model_pool
from factory.orchestration.backlog_refill_policy import normalize
from factory.orchestration.factory_models import FactoryAudit

ACTION = "refill_control_applied"
JOB = "factory-retro-daily"


def _job(db, *, lock=False):
    sqlite = db.bind.dialect.name == "sqlite"
    if sqlite and not inspect(db.bind).has_table("routine_jobs"):
        return None
    table = "routine_jobs" if sqlite else "claude_agent.routine_jobs"
    suffix = " FOR UPDATE" if lock and not sqlite else ""
    row = (
        db.execute(
            text(f"SELECT * FROM {table} WHERE name=:name{suffix}"), {"name": JOB}
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def status(db, control):
    policy = json.loads(control.policy_json or "{}")
    events = db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action.startswith(refill.PREFIX))
        .order_by(FactoryAudit.id.desc())
        .limit(6)
    ).all()
    event_rows = []
    for row in events:
        detail = json.loads(row.detail_json or "{}")
        event_rows.append(
            {
                "id": row.id,
                "action": row.action,
                "at": str(row.created_at),
                **{
                    k: detail[k]
                    for k in (
                        "reason",
                        "run_id",
                        "receipt_id",
                        "status",
                        "success",
                        "new_issues",
                        "eligible",
                    )
                    if k in detail
                },
            }
        )
    checkpoints = {}
    for action in (
        "started",
        "completed",
        "skipped",
        "breaker_open",
        "policy_observed",
    ):
        row = refill.latest_event(db, action, repo=policy.get("repo", ""))
        if row is not None:
            detail = json.loads(row.detail_json or "{}")
            checkpoints[action] = {
                "id": row.id,
                "at": str(row.created_at),
                **{
                    k: detail[k]
                    for k in ("run_id", "receipt_id", "reason", "new_issues")
                    if k in detail
                },
            }
    job = _job(db)
    repos = controls.repos_map(policy)
    return {
        "policy": normalize(policy),
        "configured": "backlog_refill" in policy,
        "problem_issues": {
            k: controls.problem_issues_policy(policy).get(k)
            for k in ("enabled", "sources", "improvement_scout")
        },
        "intake_selection": [
            {
                "repo": repo,
                "enabled": entry.get("enabled"),
                "issue_numbers": policy.get("issue_numbers", [])[:200],
                "truncated": len(policy.get("issue_numbers", [])) > 200,
            }
            for repo, entry in list(repos.items())[:20]
        ],
        "producer_checkpoints": checkpoints,
        "recent_events": event_rows[:5],
        "events_truncated": len(event_rows) > 5,
        "learning_job": None
        if job is None
        else {
            k: str(job[k]) if job[k] is not None and k.endswith("_at") else job[k]
            for k in (
                "name",
                "routine_kind",
                "interval_secs",
                "next_run_at",
                "last_run_at",
                "last_status",
                "locked_by",
                "locked_at",
            )
        },
        "coverage": "Bounded current policy and latest events, no inferred execution or generated delivery.",
    }


def _quota(model):
    if model not in {"astra", "opus"} or model not in offered_models():
        raise ValueError("refill_model_unavailable")
    raw = provider_quota.fetch_provider_quota_sync(force=True)
    quota = provider_quota.summarise(raw.get("providers", {}))
    provider = model_pool.QUOTA_PROVIDERS.get(model_pool.family_for(model))
    view = quota.get(provider, {})
    if (
        raw.get("available") is not True
        or model_pool.grant_observation_state(view) != "fresh"
        or not view.get("windows")
        or any(
            not w.get("usable") or w.get("used_percent", 100) >= 100
            for w in view["windows"]
        )
    ):
        raise ValueError("refill_subscription_unavailable")
    if not model_pool.availability(model, quota, "worker")[0]:
        raise ValueError("refill_subscription_unavailable")
    return {
        "provider": provider,
        "observation": view,
        "checked_at": controls._now().isoformat(),
    }


def _observations(enabled, model, hourly):
    observations = {}
    if enabled:
        observations[model] = _quota(model)
    if hourly and "astra" not in observations:
        # The existing Luna evidence job and Astra evaluation use Codex capacity.
        observations["astra"] = _quota("astra")
    return observations


def _plan(db, control, enabled, model, hourly, expected_version, reason, quota):
    if (
        type(enabled) is not bool
        or type(hourly) is not bool
        or type(expected_version) is not int
    ):
        raise ValueError("refill_invalid_request")
    if control.version != expected_version:
        raise ValueError("refill_version_conflict")
    if control.state == "stopped":
        raise ValueError("refill_factory_stopped")
    policy = json.loads(control.policy_json or "{}")
    block = normalize(policy)
    if enabled:
        if (
            control.state != "enabled"
            or not controls.intake_policy(policy)["enabled"]
            or policy.get("repo") not in controls.enabled_repos(policy)
        ):
            raise ValueError("refill_intake_not_enabled")
        if model not in policy.get("allowed_models", []):
            raise ValueError("refill_model_not_allowed")
    for required in ({model} if enabled else set()) | ({"astra"} if hourly else set()):
        if required not in quota or controls._now() - aware(
            datetime.fromisoformat(quota[required]["checked_at"])
        ) > timedelta(seconds=30):
            raise ValueError("refill_quota_changed")
    changed = {**block, "enabled": enabled}
    if enabled:
        changed.update(model=model, max_findings=5)
    if changed != block and refill._ledger(db, policy.get("repo", ""))[2]:
        raise ValueError("refill_in_flight")
    job = _job(db, lock=True) if hourly else None
    if hourly:
        if (
            job is None
            or job["routine_kind"] != "qwen-drain"
            or job["interval_secs"] not in (86400, 3600)
            or job["next_run_at"] is None
            or job["locked_by"] is not None
            or job["locked_at"] is not None
            or job["last_status"] != "ok"
        ):
            raise ValueError("refill_learning_job_held")
        payload = learning._json(job["payload"])
        if (
            not learning.enabled()
            or payload.get("learning_stage") != "evidence"
            or payload.get("model") != "luna"
            or payload.get("repo") != learning.REPO
            or payload.get("digest") != "factory-retro"
        ):
            raise ValueError("refill_learning_job_unrecognized")
        if (
            db.exec(
                select(AgentCapacityReservation.id).where(
                    AgentCapacityReservation.routine_job_name == JOB,
                    AgentCapacityReservation.state != "settled",
                )
            ).first()
            is not None
        ):
            raise ValueError("refill_learning_execution_active")
        owners = select(AgentSession.id).where(
            AgentSession.local_session_id.endswith(":qwen-drain:" + JOB)
        )
        if (
            db.exec(
                select(AgentSession.id).where(
                    AgentSession.id.in_(owners),
                    (AgentSession.status == "running")
                    | AgentSession.ember_session_id.is_not(None)
                    | AgentSession.result_receipt_fence_id.is_not(None),
                )
            ).first()
            is not None
            or db.exec(
                select(PendingMessage.id).where(PendingMessage.session_id.in_(owners))
            ).first()
            is not None
        ):
            raise ValueError("refill_learning_execution_active")
        from factory.execution import store

        if (
            db.exec(
                select(AgentSession.id).where(
                    AgentSession.id.in_(owners),
                    store._unknown_outcome_exists(AgentSession.id),
                )
            ).first()
            is not None
        ):
            raise ValueError("refill_learning_execution_unknown")
    proposal = {
        "ok": True,
        "version": control.version,
        "policy_sha256": digest(policy),
        "refill_before": block,
        "refill_after": changed,
        "hourly": hourly,
        "job_sha256": digest(job) if job else None,
        "reason": controls._text(reason, "reason"),
        "generation": policy.get("generation"),
        "preserves_breaker_history": True,
    }
    proposal["preview_sha256"] = digest(proposal)
    return proposal, job


def preview(enabled, model, hourly, expected_version, reason):
    quota = _observations(enabled, model, hourly)
    with controls._locked_session() as (db, control):
        admission.lock_pool(db)
        proposal, _job_row = _plan(
            db, control, enabled, model, hourly, expected_version, reason, quota
        )
        return {**proposal, "subscription": quota}


def apply(
    actor,
    enabled,
    model,
    hourly,
    expected_version,
    reason,
    request_key,
    expected_preview_sha256,
):
    request = {
        "enabled": enabled,
        "model": model,
        "hourly": hourly,
        "expected_version": expected_version,
        "reason": reason,
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    actor = controls._text(actor, "actor")

    def replay(db):
        for raw in db.exec(
            select(FactoryAudit.detail_json).where(
                FactoryAudit.actor == actor, FactoryAudit.action == ACTION
            )
        ).all():
            old = json.loads(raw)
            if old["request"]["request_key"] == request_key:
                if old["request"] != request:
                    raise ValueError("refill_request_conflict")
                return old["result"]

    with controls._read_session() as db:
        prior = replay(db)
        if prior:
            return prior
    quota = _observations(enabled, model, hourly)
    with controls._locked_session() as (db, control):
        admission.lock_pool(db)
        prior = replay(db)
        if prior:
            return prior
        proposal, job = _plan(
            db, control, enabled, model, hourly, expected_version, reason, quota
        )
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("refill_preview_changed")
        policy = json.loads(control.policy_json)
        policy["backlog_refill"] = proposal["refill_after"]
        control.policy_json = dumps(policy)
        if hourly and job["interval_secs"] != 3600:
            table = learning._table(db, "claude_agent", "routine_jobs")
            # Keep overdue work due. Never rearm a held job or clear its lease.
            due = min(
                aware(datetime.fromisoformat(str(job["next_run_at"]))),
                controls._now() + timedelta(hours=1),
            )
            db.execute(
                text(
                    f"UPDATE {table} SET interval_secs=3600,next_run_at=:due WHERE name=:name"
                ),
                {"due": due, "name": JOB},
            )
        control.version += 1
        control.actor = actor
        control.updated_at = controls._now()
        result = {
            "ok": True,
            "version": control.version,
            "enabled": enabled,
            "hourly": hourly,
            "dispatched": False,
            "generation": policy.get("generation"),
        }
        controls._audit(
            db,
            actor,
            ACTION,
            request=request,
            proposal=proposal,
            subscription=quota,
            result=result,
        )
        return result
