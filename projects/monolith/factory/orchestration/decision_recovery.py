"""Explicit standing-operator reconciliation of one durable decision request.

No GitHub writes or automatic replay. Operator evidence is an attributable
attestation of fresh source reads, never inferred from the age of a request.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlmodel import select

from core.clock import as_utc, iso
from factory.access import is_operator
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: datetime
    cessation_confirmed: bool
    cessation: str = Field(min_length=1, max_length=2000)
    effects: str = Field(min_length=1, max_length=2000)
    conclusion: Literal["completed", "no_effects", "unknown"]


class Settlement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=256)
    request_key: str = Field(min_length=1, max_length=256)
    decision_id: str = Field(pattern=r"^decision:[0-9a-f]{64}$")
    expected_audit_id: int = Field(gt=0)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    settlement_key: str = Field(min_length=1, max_length=256)
    evidence: Evidence


@contextmanager
def execution_lock(receipt_id: int):
    """Try a receipt-scoped lock across effects and their durable completion.

    PostgreSQL's transaction lock is released on connection/process loss.
    File-backed SQLite tests use flock, including across threads/processes.
    This lock is always acquired before the existing control-row lock.
    """
    engine = controls.get_engine()
    if engine.dialect.name == "sqlite":
        import fcntl

        database = engine.url.database
        if not database or database == ":memory:":
            raise ValueError("decision execution requires file-backed SQLite")
        path = Path(database).with_name(Path(database).name + f".decision-{receipt_id}")
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
    elif engine.dialect.name == "postgresql":
        key = int.from_bytes(
            hashlib.sha256(f"factory-decision:{receipt_id}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        with engine.begin() as connection:
            acquired = connection.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
            ).scalar_one()
            yield bool(acquired)
    else:
        raise ValueError("unsupported decision execution lock dialect")


def _events(db, receipt_id: int):
    # Native JSON extraction keeps the read targeted even with a large audit log.
    from factory.orchestration.mcp import _audit_text

    return db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.action == "decision_request",
            _audit_text(("request", "receipt_id"), db.get_bind().dialect.name)
            == str(receipt_id),
        )
        .order_by(FactoryAudit.id)
    ).all()


def _view(row):
    detail = json.loads(row.detail_json)
    return {
        "audit_id": row.id,
        "actor": row.actor,
        "recorded_at": iso(as_utc(row.created_at)),
        "sha256": hashlib.sha256(row.detail_json.encode()).hexdigest(),
        **detail,
    }


def unresolved(db, receipt_id: int, limit: int = 10) -> dict:
    latest = {}
    first = {}
    for row in _events(db, receipt_id):
        key = (row.actor, json.loads(row.detail_json)["request"]["request_key"])
        first.setdefault(key, row.created_at)
        latest[key] = row
    rows = []
    for key, row in latest.items():
        view = _view(row)
        if view["result"]["state"] not in ("accepted", "outcome_unknown"):
            continue
        request = view["request"]
        rows.append(
            {
                "audit_id": row.id,
                "sha256": view["sha256"],
                "actor": row.actor,
                **{
                    name: request.get(name)
                    for name in (
                        "receipt_id",
                        "decision_id",
                        "request_key",
                        "action",
                        "option_key",
                    )
                },
                "created_at": iso(as_utc(first[key])),
                "updated_at": view["recorded_at"],
                "state": view["result"]["state"],
                "inspection_path": f"/api/swarm/factory/decision-requests/{receipt_id}/{row.id}",
                "settlement_path": f"/api/swarm/factory/decision-requests/{receipt_id}/settle",
            }
        )
    return {"items": rows[-limit:], "total": len(rows), "truncated": len(rows) > limit}


def inspect_request(receipt_id: int, audit_id: int, principal) -> dict:
    from factory.orchestration.mcp import _audit_text

    if not is_operator(principal):
        raise ValueError("standing operator authority is required")
    with controls._read_session() as db:
        rows = _events(db, receipt_id)
        target = next((row for row in rows if row.id == audit_id), None)
        if target is None:
            raise ValueError("no such request on this receipt")
        view = _view(target)
        key = view["request"]["request_key"]
        history = [
            row
            for row in rows
            if row.actor == target.actor
            and json.loads(row.detail_json)["request"]["request_key"] == key
        ]
        dialect = db.get_bind().dialect.name
        settlements = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.action == "decision_request_settled",
                _audit_text(("receipt_id",), dialect) == str(receipt_id),
                _audit_text(("settlement", "actor"), dialect) == target.actor,
                _audit_text(("settlement", "request_key"), dialect) == key,
            )
            .order_by(FactoryAudit.id.desc())
            .limit(11)
        ).all()
        return {
            "observed_at": iso(controls._now()),
            "original": _view(history[0]),
            "latest": _view(history[-1]),
            "event_count": len(history),
            "settlements": [_view(event) for event in settlements[:10]],
            "settlements_truncated": len(settlements) > 10,
        }


def settle(receipt_id: int, body: Settlement, principal) -> dict:
    from factory.orchestration import factory_decisions as decisions

    if not is_operator(principal):
        raise ValueError("standing operator authority is required")
    with execution_lock(receipt_id) as acquired:
        if not acquired:
            raise ValueError("original request execution is live")
        with controls._locked_session() as (db, control):
            payload = {"receipt_id": receipt_id, **body.model_dump(mode="json")}
            prior = db.exec(
                select(FactoryAudit)
                .where(
                    FactoryAudit.action == "decision_request_settled",
                    FactoryAudit.actor == principal.subject,
                )
                .order_by(FactoryAudit.id)
            ).all()
            for event in prior:
                detail = json.loads(event.detail_json)
                if detail["settlement"]["settlement_key"] == body.settlement_key:
                    if detail["settlement"] != payload:
                        raise ValueError("conflicting settlement key")
                    return detail["result"]
            rows = _events(db, receipt_id)
            matching = [
                row
                for row in rows
                if row.actor == body.actor
                and json.loads(row.detail_json)["request"]["request_key"]
                == body.request_key
            ]
            if not matching:
                raise ValueError("no such actor-scoped request on this receipt")
            view = _view(matching[-1])
            request = view["request"]
            if (
                view["audit_id"] != body.expected_audit_id
                or view["sha256"] != body.expected_sha256
                or request.get("decision_id") != body.decision_id
            ):
                raise ValueError("stale request evidence")
            if view["result"]["state"] not in ("accepted", "outcome_unknown"):
                raise ValueError("request is already settled")
            row = decisions._receipt(db, receipt_id)
            decisions._require_current_generation(db, row, control)
            escalation = decisions.escalation_of(row) or {}
            resolution = escalation.get("resolved")
            identity = decisions._fields(row)["decision_id"]
            if identity != body.decision_id:
                raise ValueError("stale decision evidence")
            age = (controls._now() - as_utc(body.evidence.observed_at)).total_seconds()
            if not 0 <= age <= 300 or not all(
                value.strip()
                for value in (body.evidence.cessation, body.evidence.effects)
            ):
                raise ValueError("fresh cessation and effect evidence is required")
            if as_utc(body.evidence.observed_at) < as_utc(matching[-1].created_at):
                raise ValueError("evidence predates the observed request result")
            result = {
                **view["result"],
                "ok": False,
                "state": "outcome_unknown",
                "reason": "operator reconciliation remains unproven",
            }
            if (
                body.evidence.conclusion != "unknown"
                and not body.evidence.cessation_confirmed
            ):
                raise ValueError("positive cessation evidence is required")
            if body.evidence.conclusion == "completed":
                # Only the original durable resolution can prove completion here.
                # External effects without it remain unknown for reconciliation.
                if (
                    request["action"] != "decide"
                    or not resolution
                    or resolution.get("decision_id") != body.decision_id
                    or resolution.get("option_key") != request["option_key"]
                    or resolution.get("actor") != body.actor
                    or resolution.get("note")
                    != ((request.get("note") or "").strip() or None)
                ):
                    raise ValueError("no exact durable completion evidence")
                result = decisions._decision_result(request, resolution, applied=False)
            elif body.evidence.conclusion == "no_effects":
                from factory.orchestration.factory_models import WorkItem

                direction = json.loads(row.direction_json or "{}")
                item = db.get(WorkItem, row.work_item_id) if row.work_item_id else None
                if (
                    resolution
                    or row.cancellation_requested
                    or row.task_paused
                    or controls.landing_held(row)
                    or (item and "human" in {label.lower() for label in item.labels})
                    or any(
                        key in direction
                        for key in (
                            "human_handoff",
                            "nonrenewable_recovery",
                            "review_recovery",
                        )
                    )
                ):
                    raise ValueError("resolution or active hold prevents abandonment")
                if row.task_id:
                    from factory.orchestration.models import SwarmTask

                    refusal = controls._quiescence_refusal(db, row.task_id)
                    task = db.get(SwarmTask, row.task_id, populate_existing=True)
                    if (
                        row.state in decisions._RUNNING_STATES
                        or task is None
                        or task.settled_at is None
                        or controls.review_recovery_held(row.task_id, session=db)
                        or refusal
                    ):
                        raise ValueError(
                            "active task requires its existing recovery owner"
                        )
                # A completed child write is positive partial-effect evidence.
                children = decisions._decision_audits(
                    db, receipt_id, "decision_child_created"
                )
                if any(
                    item.get("decision_id") in (None, body.decision_id)
                    for item in children
                ):
                    raise ValueError("partial effects require reconciliation")
                result = {
                    **result,
                    "state": "abandoned",
                    "reason": "operator proved cessation with no effects",
                }
            decisions._request_event(db, body.actor, request, result)
            controls._audit(
                db,
                principal.subject,
                "decision_request_settled",
                settlement=payload,
                result=result,
                receipt_id=receipt_id,
                decision_id=body.decision_id,
                original_actor=body.actor,
                option_key=request["option_key"],
                released_claim=result["state"] == "abandoned",
            )
            return result
