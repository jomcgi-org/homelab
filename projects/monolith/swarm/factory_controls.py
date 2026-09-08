"""Durable operator authority and conservative factory start reservations.

Every mutation locks the singleton control row before inspecting receipts. The
write lock also serializes file-backed SQLite tests; admission never relies on
an unlocked count. Issue text and conductor artifacts cannot configure policy.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import re
from typing import Iterator

from sqlalchemy import update
from sqlmodel import Session, select

from core.db import get_engine
from swarm.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
)
from swarm.models import SwarmTask

_ACTIVE = ("admitted", "uncertain")
_TERMINAL = ("succeeded", "failed", "cancelled")
_POLICY_KEYS = {
    "repo",
    "issue_numbers",
    "generation",
    "max_tasks",
    "max_turns_per_task",
    "task_budget_usd",
    "turn_budget_usd",
    "allowed_models",
    "conductor_model",
    "reviewer_model",
    "base_branch",
    "turn_timeout_seconds",
    "max_attempts",
    "worker_model",
    "task_timeout_seconds",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: object, name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {name}")
    return value


def _integer(value: object, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid {name}")
    return value


def _money(value: object, name: str, *, zero: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"invalid {name}")
    if value < 0 or (not zero and value == 0):
        raise ValueError(f"invalid {name}")
    return float(value)


def normalize_repo(repo: str) -> str:
    if not isinstance(repo, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo
    ):
        raise ValueError("invalid repo")
    if len(repo) > 256 or any(part in (".", "..") for part in repo.split("/")):
        raise ValueError("invalid repo")
    return repo.lower()


def validate_policy(policy: dict) -> dict:
    if not isinstance(policy, dict) or set(policy) not in {
        _POLICY_KEYS,
        _POLICY_KEYS - {"reviewer_model"},
    }:
        raise ValueError("policy must contain exactly the supported operator fields")
    result = dict(policy)
    if "reviewer_model" not in result:
        result["reviewer_model"] = policy["conductor_model"]
    result["repo"] = normalize_repo(policy["repo"])
    issues = policy["issue_numbers"]
    if not isinstance(issues, list) or not 1 <= len(issues) <= 100:
        raise ValueError("invalid issue_numbers")
    result["issue_numbers"] = sorted(
        {_integer(i, "issue_number", 1, 2**31 - 1) for i in issues}
    )
    for key, low, high in (
        ("generation", 0, 2**31 - 1),
        ("max_tasks", 1, 100),
        ("max_turns_per_task", 1, 100),
        ("turn_timeout_seconds", 1, 43200),
        ("max_attempts", 1, 10),
        ("task_timeout_seconds", 1, 86400),
    ):
        result[key] = _integer(policy[key], key, low, high)
    for key in ("task_budget_usd", "turn_budget_usd"):
        result[key] = _money(policy[key], key)
    if result["turn_budget_usd"] > result["task_budget_usd"]:
        raise ValueError("turn budget exceeds task budget")
    models = policy["allowed_models"]
    if not isinstance(models, list) or not 1 <= len(models) <= 16:
        raise ValueError("invalid allowed_models")
    result["allowed_models"] = sorted({_text(m, "model", 128) for m in models})
    if policy["conductor_model"] not in result["allowed_models"]:
        raise ValueError("conductor model is not allowed")
    if result["reviewer_model"] not in result["allowed_models"]:
        raise ValueError("reviewer model is not allowed")
    if policy["worker_model"] not in result["allowed_models"]:
        raise ValueError("worker model is not allowed")
    if result["turn_timeout_seconds"] > result["task_timeout_seconds"]:
        raise ValueError("turn timeout exceeds task timeout")
    result["base_branch"] = _text(policy["base_branch"], "base_branch", 256)
    return result


@contextmanager
def _read_session(session: Session | None = None) -> Iterator[Session]:
    if session is not None:
        yield session
    else:
        with Session(get_engine()) as owned:
            yield owned


@contextmanager
def _locked_session(
    session: Session | None = None,
) -> Iterator[tuple[Session, FactoryControl]]:
    """Supplied sessions retain their transaction for atomic caller composition."""
    with _read_session(session) as db:
        try:
            result = db.execute(
                update(FactoryControl)
                .where(FactoryControl.id == "factory")
                .values(version=FactoryControl.version)
            )
            if result.rowcount != 1:
                raise RuntimeError("factory control migration/seed is missing")
            control = db.exec(
                select(FactoryControl)
                .where(FactoryControl.id == "factory")
                .execution_options(populate_existing=True)
            ).one()
            yield db, control
            if session is None:
                db.commit()
            else:
                db.flush()
        except BaseException:
            if session is None:
                db.rollback()
            raise


def _audit(
    db: Session,
    actor: str,
    action: str,
    *,
    task_id: str | None = None,
    **detail: object,
) -> None:
    if task_id is not None and db.get(SwarmTask, task_id) is None:
        detail["requested_task_id"] = task_id
        task_id = None
    db.add(
        FactoryAudit(
            actor=actor, action=action, task_id=task_id, detail_json=_json(detail)
        )
    )


def _receipt(db: Session, task_id: str) -> FactoryReceipt | None:
    return db.exec(
        select(FactoryReceipt)
        .where(FactoryReceipt.task_id == task_id)
        .execution_options(populate_existing=True)
    ).first()


def _starts(db: Session, task_id: str) -> list[FactoryStart]:
    return list(
        db.exec(
            select(FactoryStart)
            .where(FactoryStart.task_id == task_id)
            .order_by(FactoryStart.id)
            .execution_options(populate_existing=True)
        ).all()
    )


def _accounting(starts: list[FactoryStart]) -> dict:
    return {
        "turns_used": len(starts),
        "committed_cost_usd": sum(
            max(row.max_cost_usd, row.cost_usd or 0)
            if row.status in ("reserved", "uncertain")
            else row.max_cost_usd
            if row.cost_usd is None
            else row.cost_usd
            for row in starts
        ),
        "unresolved_starts": sum(
            row.status in ("reserved", "uncertain") for row in starts
        ),
    }


def _start_dict(row: FactoryStart) -> dict:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "task_id",
            "start_key",
            "actor",
            "model",
            "max_cost_usd",
            "status",
            "cost_usd",
            "session_id",
        )
    }


def _snapshot(db: Session, row: FactoryReceipt, *, body: bool = False) -> dict:
    starts = _starts(db, row.task_id) if row.task_id else []
    result = {
        key: getattr(row, key)
        for key in (
            "id",
            "repo",
            "issue_number",
            "generation",
            "title",
            "url",
            "state",
            "task_id",
            "task_paused",
            "cancellation_requested",
        )
    }
    result.update(
        policy=json.loads(row.policy_json) if row.policy_json else None,
        starts=[_start_dict(s) for s in starts],
        **_accounting(starts),
    )
    if row.task_id:
        task = db.get(SwarmTask, row.task_id)
        admitted = (
            task.created_at.replace(tzinfo=timezone.utc)
            if task.created_at.tzinfo is None
            else task.created_at
        )
        deadline = admitted + timedelta(
            seconds=result["policy"]["task_timeout_seconds"]
        )
        result.update(
            admitted_at=admitted.isoformat(),
            deadline_at=deadline.isoformat(),
            limits={
                "deadline_expired": _now() >= deadline,
                "turn_limit_reached": result["turns_used"]
                >= result["policy"]["max_turns_per_task"],
                "budget_limit_reached": result["committed_cost_usd"]
                >= result["policy"]["task_budget_usd"],
            },
        )
        last = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == row.task_id,
                FactoryAudit.action == "finish_task",
            )
            .order_by(FactoryAudit.id.desc())
        ).first()
        result["evidence"] = (
            json.loads(last.detail_json).get("evidence") if last else None
        )
    if body:
        result["body"] = row.body
    return result


def status(*, session: Session | None = None) -> dict:
    with _read_session(session) as db:
        control = db.exec(
            select(FactoryControl)
            .where(FactoryControl.id == "factory")
            .execution_options(populate_existing=True)
        ).first()
        if control is None:
            return {
                "ok": False,
                "reason": "not_initialized",
                "state": "disabled",
                "receipts": [],
            }
        rows = db.exec(select(FactoryReceipt).order_by(FactoryReceipt.id)).all()
        receipts = [_snapshot(db, r) for r in rows]
        return {
            "ok": True,
            "state": control.state,
            "policy": json.loads(control.policy_json),
            "admitted_count": control.admitted_count,
            "version": control.version,
            "actor": control.actor,
            "receipts": receipts,
            "active_tasks": [r for r in receipts if r["state"] in _ACTIVE],
        }


def task_snapshot(task_id: str, *, session: Session | None = None) -> dict:
    with _read_session(session) as db:
        row = _receipt(db, task_id)
        return (
            {"ok": False, "reason": "unknown_task"}
            if row is None
            else {"ok": True, **_snapshot(db, row, body=True)}
        )


def set_control(
    action: str,
    actor: str,
    *,
    policy: dict | None = None,
    task_id: str | None = None,
    session: Session | None = None,
) -> dict:
    actor = _text(actor, "actor")
    if action not in (
        "configure",
        "enable",
        "pause_admissions",
        "pause_task",
        "resume_task",
        "stop",
    ):
        raise ValueError("invalid control action")
    configured = validate_policy(policy) if action == "configure" else None
    if action != "configure" and policy is not None:
        raise ValueError("policy requires configure action")
    if (action in ("pause_task", "resume_task")) != (task_id is not None):
        raise ValueError("task_id is required only for task pause/resume")
    with _locked_session(session) as (db, control):
        reason = None
        if control.state == "stopped" and action != "stop":
            reason = "stopped"
        elif action == "configure":
            if db.exec(
                select(FactoryReceipt).where(FactoryReceipt.state.in_(_ACTIVE))
            ).first():
                reason = "active_task"
            else:
                control.policy_json = _json(configured)
                control.state = "disabled"
        elif action == "enable":
            if not json.loads(control.policy_json):
                reason = "not_configured"
            else:
                control.state = "enabled"
        elif action == "pause_admissions":
            control.state = "paused"
        elif action in ("pause_task", "resume_task"):
            row = _receipt(db, task_id)
            if row is None or row.state not in _ACTIVE:
                reason = "task_not_active"
            elif row.cancellation_requested:
                reason = "cancellation_pending"
            else:
                row.task_paused = action == "pause_task"
                row.updated_at = _now()
                db.add(row)
        elif action == "stop":
            control.state = "stopped"
            control.stopped_at = control.stopped_at or _now()
            for row in db.exec(
                select(FactoryReceipt).where(FactoryReceipt.state.in_(_ACTIVE))
            ).all():
                row.cancellation_requested = True
                row.updated_at = _now()
                db.add(row)
        if reason is None:
            control.version += 1
            control.updated_at = _now()
            control.actor = actor
            db.add(control)
        _audit(
            db,
            actor,
            action,
            task_id=task_id,
            ok=reason is None,
            reason=reason,
            policy=configured,
        )
        return {
            "ok": reason is None,
            "reason": reason,
            "state": control.state,
            "version": control.version,
        }


def _can_start(db: Session, control: FactoryControl, task_id: str) -> dict:
    row = _receipt(db, task_id)
    reason = None
    if control.state == "stopped":
        reason = "stopped"
    elif control.state not in ("enabled", "paused"):
        reason = "disabled"
    elif row is None or row.state not in _ACTIVE:
        reason = "task_not_active"
    elif row.cancellation_requested:
        reason = "cancellation_pending"
    elif row.task_paused:
        reason = "task_paused"
    elif row.state == "uncertain" or any(
        s.status == "uncertain" for s in _starts(db, task_id)
    ):
        reason = "uncertain_outcome"
    elif row is not None:
        task = db.get(SwarmTask, task_id)
        admitted = (
            task.created_at.replace(tzinfo=timezone.utc)
            if task.created_at.tzinfo is None
            else task.created_at
        )
        timeout = json.loads(row.policy_json)["task_timeout_seconds"]
        if _now() >= admitted + timedelta(seconds=timeout):
            reason = "task_deadline"
    return {"ok": reason is None, "reason": reason}


def can_start(task_id: str, *, session: Session | None = None) -> dict:
    """Fresh read fence. Use start_guard to serialize it with session creation."""
    with _read_session(session) as db:
        control = db.exec(
            select(FactoryControl)
            .where(FactoryControl.id == "factory")
            .execution_options(populate_existing=True)
        ).first()
        return (
            {"ok": False, "reason": "not_initialized"}
            if control is None
            else _can_start(db, control, task_id)
        )


@contextmanager
def start_guard(task_id: str, *, session: Session | None = None) -> Iterator[dict]:
    """Serialize stop with synchronous session persistence, never guest waits.

    Hold this guard only across the API that durably creates the session and
    pending turn. Once it returns, an admitted session remains subject to explicit
    cancellation/reconciliation. A supplied session must commit before any wait.
    """
    with _locked_session(session) as (db, control):
        yield _can_start(db, control, task_id)


def authorize_start(
    task_id: str,
    start_key: str,
    actor: str,
    *,
    model: str,
    max_cost_usd: float,
    session: Session | None = None,
) -> dict:
    actor = _text(actor, "actor")
    start_key = _text(start_key, "start_key", 256)
    model = _text(model, "model", 128)
    cost = _money(max_cost_usd, "max_cost_usd")
    with _locked_session(session) as (db, control):
        check = _can_start(db, control, task_id)
        if not check["ok"]:
            return check
        row = _receipt(db, task_id)
        policy = json.loads(row.policy_json)
        starts = _starts(db, task_id)
        existing = next((s for s in starts if s.start_key == start_key), None)
        if existing:
            if existing.model != model or existing.max_cost_usd != cost:
                return {"ok": False, "reason": "conflicting_start_pin"}
            return {"ok": True, "replayed": True, "start": _start_dict(existing)}
        budget = _accounting(starts)
        reason = None
        if any(s.status == "reserved" for s in starts):
            reason = "start_pending"
        elif model not in policy["allowed_models"]:
            reason = "model_not_allowed"
        elif budget["turns_used"] >= policy["max_turns_per_task"]:
            reason = "turn_limit"
        elif (
            cost > policy["turn_budget_usd"]
            or budget["committed_cost_usd"] + cost > policy["task_budget_usd"]
        ):
            reason = "budget_limit"
        if reason:
            _audit(
                db,
                actor,
                "authorize_start",
                task_id=task_id,
                ok=False,
                reason=reason,
                start_key=start_key,
            )
            return {"ok": False, "reason": reason}
        start = FactoryStart(
            task_id=task_id,
            start_key=start_key,
            actor=actor,
            model=model,
            max_cost_usd=cost,
        )
        db.add(start)
        db.flush()
        _audit(
            db, actor, "authorize_start", task_id=task_id, ok=True, start_key=start_key
        )
        return {"ok": True, "replayed": False, "start": _start_dict(start)}


def record_start_outcome(
    task_id: str,
    start_key: str,
    status: str,
    actor: str,
    *,
    cost_usd: float | None = None,
    session_id: int | None = None,
    reconciled: bool = False,
    session: Session | None = None,
) -> dict:
    actor = _text(actor, "actor")
    if type(reconciled) is not bool:
        raise ValueError("invalid reconciled flag")
    if status not in (*_TERMINAL, "uncertain"):
        raise ValueError("invalid start outcome")
    cost = None if cost_usd is None else _money(cost_usd, "cost_usd", zero=True)
    if session_id is not None:
        _integer(session_id, "session_id", 1, 2**31 - 1)
    # Known completion without provider cost consumes the reserved ceiling.
    # Only uncertain execution retains an active reservation.
    effective = status
    with _locked_session(session) as (db, _control):
        start = next(
            (s for s in _starts(db, task_id) if s.start_key == start_key), None
        )
        if start is None:
            return {"ok": False, "reason": "unknown_start"}
        if (
            start.status == effective
            and start.cost_usd == cost
            and start.session_id == session_id
        ):
            return {"ok": True, "replayed": True, "start": _start_dict(start)}
        if start.status in _TERMINAL:
            return {"ok": False, "reason": "conflicting_outcome"}
        if start.status == "uncertain" and not reconciled:
            return {"ok": False, "reason": "reconciliation_required"}
        if start.session_id is not None and session_id != start.session_id:
            return {"ok": False, "reason": "conflicting_session"}
        start.status, start.cost_usd, start.session_id = effective, cost, session_id
        start.updated_at = _now()
        db.add(start)
        row = _receipt(db, task_id)
        if effective == "uncertain":
            row.state = "uncertain"
        elif row.state == "uncertain" and not any(
            s.status == "uncertain" and s.id != start.id for s in _starts(db, task_id)
        ):
            row.state = "admitted"
        row.updated_at = _now()
        db.add(row)
        _audit(
            db,
            actor,
            "record_start_outcome",
            task_id=task_id,
            start_key=start_key,
            status=effective,
            reconciled=reconciled,
        )
        return {"ok": True, "replayed": False, "start": _start_dict(start)}


def finish_task(
    task_id: str,
    outcome: str,
    actor: str,
    *,
    evidence: dict | None = None,
    session: Session | None = None,
) -> dict:
    actor = _text(actor, "actor")
    if outcome not in (*_TERMINAL, "uncertain"):
        raise ValueError("invalid task outcome")
    if evidence is not None:
        if not isinstance(evidence, dict) or set(evidence) - {
            "pr_url",
            "head_sha",
            "review_session_id",
            "state",
            "reason",
        }:
            raise ValueError("unsupported task evidence")
        for key, value in evidence.items():
            if key == "review_session_id":
                _integer(value, key, 1, 2**31 - 1)
            else:
                _text(value, key, 1024)
        if len(_json(evidence)) > 4096:
            raise ValueError("task evidence too large")
    with _locked_session(session) as (db, _control):
        row = _receipt(db, task_id)
        if row is None:
            return {"ok": False, "reason": "unknown_task"}
        if row.state in _TERMINAL:
            last = db.exec(
                select(FactoryAudit)
                .where(
                    FactoryAudit.task_id == task_id,
                    FactoryAudit.action == "finish_task",
                )
                .order_by(FactoryAudit.id.desc())
            ).first()
            same = (
                row.state == outcome
                and last is not None
                and json.loads(last.detail_json).get("evidence") == evidence
            )
            return {"ok": same, "reason": None if same else "conflicting_outcome"}
        if (
            outcome != "uncertain"
            and _accounting(_starts(db, task_id))["unresolved_starts"]
        ):
            return {"ok": False, "reason": "unresolved_starts"}
        row.state = outcome
        row.updated_at = _now()
        db.add(row)
        if outcome in _TERMINAL:
            task = db.get(SwarmTask, task_id)
            task.settled_at = _now()
            task.start_state = outcome
            db.add(task)
        _audit(
            db,
            actor,
            "finish_task",
            task_id=task_id,
            outcome=outcome,
            evidence=evidence,
        )
        return {"ok": True, "state": outcome}
