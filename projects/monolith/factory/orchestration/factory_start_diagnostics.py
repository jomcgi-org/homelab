"""Private observations of existing start fences, without dispatch authority."""

import json
import re
from datetime import UTC, datetime

from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_funding_limits as limits
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryReceipt,
    funding_audit_filter,
)
from factory.orchestration.models import SwarmNodeRun

_REASONS = frozenset(
    {
        "not_initialized",
        "review_only_recovery",
        "stopped",
        "disabled",
        "task_not_active",
        "cancellation_pending",
        "task_paused",
        "uncertain_outcome",
        "task_deadline",
        "landing_recovery_deadline",
        "operator_recovery_requires_plan",
        "funding_review_expired",
        "funding_review_pending",
        "funding_lease_due",
        "capacity_waiting",
        "evidence_continuation_authority_changed",
        "evidence_continuation_scope_exhausted",
        "evidence_continuation_acceptance_not_ready",
    }
)
_FAILURES = frozenset(
    {
        "authority_invalid",
        "routine_pause_held",
        "pre_model_failure",
        "execution_failed",
        "artifact_invalid",
        "decision_refused",
        "unstarted_expired",
    }
)
_NODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TASK = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_COVERAGE = (
    "Current can_start control subgate. Transport, quota and physical capacity "
    "are unevaluated. Reads may race and do not prove reconciliation, dispatch, "
    "nonexecution or cessation. This grants no start or recovery authority."
)


def _integer(value):
    return value if type(value) is int and 0 < value <= 2**63 - 1 else None


def _timestamp(value):
    if isinstance(value, str) and len(value) <= 64:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _detail(event):
    try:
        value = json.loads(event.detail_json)
        return {**value, "audit_id": event.id} if isinstance(value, dict) else None
    except (ValueError, TypeError, RecursionError):
        return None


def _observe(read):
    try:
        return read()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        # Unreadable evidence never becomes an affirmative diagnostic.
        return None


def start_gate(db, task_id, *, start_key=None):
    """Project finite control reasons without returning funding plans or direction."""
    with db.no_autoflush:
        result = _observe(
            lambda: controls.can_start(task_id, session=db, start_key=start_key)
        )
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        return {"ok": None, "reason": "unavailable"}
    reason = result.get("reason")
    if result["ok"] is True and reason is None:
        return {"ok": True, "reason": None}
    if result["ok"] is False and isinstance(reason, str) and reason in _REASONS:
        return {"ok": False, "reason": reason}
    return {"ok": None, "reason": "unrecognized"}


def _latest(db, task_id, action):
    return db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.task_id == task_id, funding_audit_filter(action))
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()


def _summary(db, task_id, event):
    if event is None:
        return None
    detail = _detail(event)
    result = {
        "audit_id": event.id,
        "created_at": _timestamp(event.created_at),
        "detail_available": detail is not None,
    }
    if detail is None:
        return result
    node = detail.get("node_key")
    source = _integer(detail.get("source_run_id"))
    run_id = (
        db.exec(
            select(SwarmNodeRun.id)
            .where(SwarmNodeRun.id == source, SwarmNodeRun.task_id == task_id)
            .limit(1)
        ).first()
        if source is not None
        else None
    )
    category = detail.get("failure_category")
    return {
        **result,
        "request_id": _integer(detail.get("request_id")),
        "node_key": node if isinstance(node, str) and _NODE.fullmatch(node) else None,
        "source_run_id": run_id,
        "failure_category": category
        if isinstance(category, str) and category in _FAILURES
        else None,
        "has_refusal": bool(detail.get("refusal")),
        "has_escalation": bool(detail.get("escalated")),
        "budget_approval_required": bool(detail.get("budget_approval_required")),
        **{
            key: _timestamp(detail.get(key))
            for key in ("deadline_at", "review_due_at", "retry_after")
        },
    }


def _pending_relationship(events):
    request_event = events["review_requested"]
    if request_event is None:
        return {"state": "none", "request_id": None}
    request = _detail(request_event)
    settled_event = events["review_settled"]
    settled = _detail(settled_event) if settled_event else None
    if request is None or (
        settled_event
        and (settled is None or _integer(settled.get("request_id")) is None)
    ):
        return {"state": "unavailable", "request_id": None}
    pending = settled is None or settled["request_id"] != request["audit_id"]
    return {
        "state": "pending" if pending else "settled",
        "request_id": request["audit_id"],
    }


def receipt_diagnostics(db, row, snapshot):
    """Read one receipt and the latest bounded funding decision identities."""
    from factory.orchestration import factory_funding as funding
    from factory.orchestration.evidence_continuation import recovery_grant

    result = {
        "observed_at": controls._now().isoformat(),
        "coverage": _COVERAGE,
        "recovery_authorized": False,
        "receipt_state": snapshot.get("state"),
        "effective_deadline_at": _timestamp(snapshot.get("deadline_at")),
        "can_start": {"ok": None, "reason": "no_task"},
        "funding": None,
    }
    if not row.task_id:
        return result
    with db.no_autoflush:
        events = {
            name: _latest(db, row.task_id, "funding_" + name)
            for name in ("review_requested", "review_settled", "granted")
        }
        settled = (
            _detail(events["review_settled"]) if events["review_settled"] else None
        )
        settled_available = events["review_settled"] is None or settled is not None
        result["can_start"] = start_gate(db, row.task_id)
        result["funding"] = {
            "enabled": limits.enabled(),
            "enrolled": events["review_requested"] is not None,
            "review_relationship": _pending_relationship(events),
            "latest": {
                key: _summary(db, row.task_id, event) for key, event in events.items()
            },
            "awaiting_operator": _observe(
                lambda: funding._awaiting_operator(db, row.task_id, settled)
            )
            if settled_available
            else None,
            "refusal_cooldown_active": _observe(lambda: funding._refusal_wait(settled))
            if settled_available
            else None,
            "nonrenewable_recovery": _observe(
                lambda: controls.nonrenewable_recovery(row)
            ),
            "evidence_recovery": _observe(
                lambda: recovery_grant(db, row.task_id) is not None
            ),
            "review_recovery_held": _observe(
                lambda: controls.review_recovery_held(row.task_id, session=db)
            ),
            "landing_recovery_expired": _observe(
                lambda: controls._landing_recovery_expired(db, row.task_id)
            ),
        }
    return result


def session_gate(db, agent):
    """Bind the executor's control subgate to its durable canonical attempt."""
    result = {
        "status": "unknown",
        "coverage": _COVERAGE,
        "recovery_authorized": False,
        "can_start": {"ok": None, "reason": "identity_unavailable"},
    }
    if agent is None:
        return result
    local = agent.get("local_session_id")
    if not isinstance(local, str):
        return result
    if not local.startswith("factory:"):
        return {**result, "status": "not_factory"}
    fields = local.split(":")
    if (
        len(fields) != 4
        or not _TASK.fullmatch(fields[1])
        or not _NODE.fullmatch(fields[2])
        or not fields[3].isascii()
        or not fields[3].isdigit()
        or len(fields[3]) > 19
    ):
        return result
    task_id, node_key, raw_attempt = fields[1:]
    attempt = _integer(int(raw_attempt))
    key = "factory-node:" + ":".join(fields[1:])
    if (
        attempt is None
        or raw_attempt != str(attempt)
        or agent.get("workflow_id") != key
        or agent.get("node_key") != node_key
        or agent.get("node_attempt") != attempt
    ):
        return result
    with db.no_autoflush:
        run = db.exec(
            select(
                SwarmNodeRun.id,
                SwarmNodeRun.session_id,
                SwarmNodeRun.dispatch_key,
                SwarmNodeRun.status,
            )
            .where(
                SwarmNodeRun.task_id == task_id,
                SwarmNodeRun.node_key == node_key,
                SwarmNodeRun.attempt == attempt,
            )
            .limit(1)
        ).first()
        row = db.exec(
            select(FactoryReceipt.id, FactoryReceipt.state)
            .where(FactoryReceipt.task_id == task_id)
            .limit(1)
        ).first()
        if (
            run is None
            or row is None
            or run.session_id != agent["id"]
            or run.dispatch_key != key
        ):
            return result
        return {
            **result,
            "status": "matched",
            "receipt_id": row.id,
            "receipt_state": row.state,
            "node_key": node_key,
            "attempt": attempt,
            "run_id": run.id,
            "run_status": run.status
            if run.status
            in {"pending", "running", "succeeded", "failed", "cancelled", "uncertain"}
            else None,
            "can_start": start_gate(db, task_id, start_key=key),
        }
