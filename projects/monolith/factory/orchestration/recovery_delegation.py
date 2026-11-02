"""Default-off, explicitly authorized calls to the existing recovery owners.

The planner has no grant-writing interface. Human authorization and worker
execution have separate identities. The owner's control lock linearizes each
call admission with revocation. A lease is never proof of worker cessation.
"""

import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, and_, cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import select

from core.jsonio import dumps
from factory.access import is_operator
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt
from factory.execution.review_leases import digest

ACTOR = "factory:recovery-worker"
GRANT = "recovery_call_authorized"
STATE = "recovery_call_state"
ADMIT = "recovery_call_admitted"
REVOKE = "recovery_call_revoked"
DIAGNOSIS = "recovery_diagnosis_required"
MAX_CALLS = 2
MAX_PENDING = 5
LEASE_SECONDS = 180
_worker = None
logger = logging.getLogger(__name__)


class ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceTarget(ExactModel):
    kind: Literal["evidence_continuation"]
    receipt_id: int = Field(gt=0, strict=True)
    decision_id: str = Field(pattern=r"^decision:[0-9a-f]{64}$")
    node_key: str = Field(pattern=r"^correct_[1-9][0-9]*$")
    source_run_id: int = Field(gt=0, strict=True)
    expected_revision: int = Field(ge=0, strict=True)
    pr_number: int = Field(gt=0, strict=True)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    evidence_comment_id: int = Field(gt=0, strict=True)
    deadline_at: str


class RetryTarget(ExactModel):
    kind: Literal["settled_retry"]
    receipt_id: int = Field(gt=0, strict=True)
    session_id: int = Field(gt=0, strict=True)
    permit_id: int = Field(gt=0, strict=True)


class Request(ExactModel):
    operation: Literal["preview", "authorize"]
    target: Annotated[EvidenceTarget | RetryTarget, Field(discriminator="kind")]
    expires_at: str
    request_key: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=4000)
    expected_preview_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def enabled():
    return os.environ.get("FACTORY_RECOVERY_DELEGATION_ENABLED", "false") == "true"


def _require(value, reason):
    if not value:
        raise ValueError("recovery_delegation_" + reason)


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None, "timezone_required")
    return parsed


def _field(db, key, *, integer=False):
    if db.get_bind().dialect.name == "sqlite":
        value = func.json_extract(FactoryAudit.detail_json, "$." + key)
    else:
        value = cast(FactoryAudit.detail_json, JSONB)[key].astext
    return cast(value, BigInteger) if integer else value


def _detail(row):
    return json.loads(row.detail_json)


def _state(db, grant_id):
    row = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.action == STATE,
            _field(db, "grant_id", integer=True) == grant_id,
        )
        .order_by(FactoryAudit.id.desc())
        .limit(1)
    ).first()
    return _detail(row) if row else {"state": "authorized", "calls": 0}


def _revoked(db, grant_id):
    return (
        db.exec(
            select(FactoryAudit.id).where(
                FactoryAudit.action == REVOKE,
                _field(db, "grant_id", integer=True) == grant_id,
            )
        ).first()
        is not None
    )


def _record(db, grant, state, **values):
    controls._audit(
        db,
        ACTOR,
        STATE,
        task_id=grant.task_id,
        grant_id=grant.id,
        state=state,
        **values,
    )


def _pending(db):
    # Terminal links exclude old grants in SQL, so history cannot hide an old
    # unresolved call. Uncertainty is deliberately not a terminal disposition.
    terminal = select(_field(db, "grant_id", integer=True)).where(
        FactoryAudit.action == STATE,
        _field(db, "state").in_(("applied", "refused", "expired", "revoked")),
    )
    return db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action == GRANT, FactoryAudit.id.not_in(terminal))
        .order_by(FactoryAudit.id)
        .limit(MAX_PENDING + 1)
    ).all()


def _owner_request(request, key):
    target = request.target.model_dump()
    if target.pop("kind") == "settled_retry":
        return {**target, "actor": ACTOR, "reason": request.reason, "request_key": key}
    return {
        "receipt_id": target["receipt_id"],
        "decision_id": target["decision_id"],
        "deadline_at": target["deadline_at"],
        "decision": {
            "action": "continue_evidence",
            "reason": request.reason,
            "node_key": target["node_key"],
            "source_run_id": target["source_run_id"],
            "expected_version": target["expected_revision"],
            "pr_number": target["pr_number"],
            "head_sha": target["head_sha"],
            "evidence_comment_id": target["evidence_comment_id"],
        },
    }


async def _preview_owner(kind, owner):
    if kind == "settled_retry":
        from factory import settled_retry_reconciliation as retry

        return await retry.run("preview", **owner)
    from factory.orchestration import evidence_recovery

    return await asyncio.to_thread(evidence_recovery.preview, owner)


def _key(principal, request):
    return "recovery-grant-" + digest(
        {"authorizer": principal.subject, "request_key": request.request_key}
    )


def _existing(db, key):
    return db.exec(
        select(FactoryAudit).where(
            FactoryAudit.action == GRANT, _field(db, "key") == key
        )
    ).one_or_none()


def _identity(request):
    return request.model_dump(exclude={"operation", "expected_preview_sha256"})


def _replay(principal, request):
    with controls._read_session() as db:
        row = _existing(db, _key(principal, request))
        if row is None:
            return None
        saved = _detail(row)
        _require(saved["request"] == _identity(request), "request_conflict")
        _require(
            saved["preview_sha256"] == request.expected_preview_sha256,
            "preview_changed",
        )
        return {
            "ok": True,
            "grant_id": row.id,
            "state": _state(db, row.id),
            "replay": True,
        }


async def request_authorization(principal, request):
    _require(is_operator(principal), "standing_human_required")
    if request.operation == "authorize":
        prior = await asyncio.to_thread(_replay, principal, request)
        if prior is not None:
            return prior
        _require(enabled(), "disabled")
    expiry = datetime.fromisoformat(request.expires_at.replace("Z", "+00:00"))
    _require(
        expiry.tzinfo is not None
        and controls._now() < expiry <= controls._now() + timedelta(minutes=15),
        "expiry_outside_window",
    )
    if isinstance(request.target, EvidenceTarget):
        _require(
            expiry <= _time(request.target.deadline_at),
            "expiry_after_owner_deadline",
        )
    key = _key(principal, request)
    owner = _owner_request(request, key)
    viewed = await _preview_owner(request.target.kind, owner)
    _require(viewed.get("ok"), "owner_preview_refused")
    if request.target.kind == "settled_retry":
        owner_sha, version, task_id = (
            viewed["preview_sha256"],
            viewed["local"]["version"],
            viewed["local"]["task_id"],
        )
    else:
        owner_sha, version, task_id = (
            viewed["proposal"]["preview_sha256"],
            viewed["proposal"]["control_version"],
            viewed["scope"]["task_id"],
        )
    proposal = {
        "key": key,
        "authorizer": principal.subject,
        "execution_actor": ACTOR,
        "request": _identity(request),
        "owner_request": owner,
        "owner_preview_sha256": owner_sha,
        "control_version": version,
        "task_id": task_id,
        "max_owner_calls": MAX_CALLS,
        "owner_preview": viewed,
    }
    proposal["preview_sha256"] = digest(proposal)
    if request.operation == "preview":
        return {"ok": True, "enabled": enabled(), "proposal": proposal}
    _require(
        proposal["preview_sha256"] == request.expected_preview_sha256, "preview_changed"
    )
    return await asyncio.to_thread(_authorize, principal, request, proposal)


def conflicting_retry_intent(db, task_id, owner):
    from factory import settled_retry_reconciliation as retry

    rows = db.exec(
        select(FactoryAudit).where(
            FactoryAudit.task_id == task_id,
            FactoryAudit.action.in_((retry.INTENT, retry.APPLIED)),
        )
    ).all()
    completed = {
        digest(_detail(r)["request"]) for r in rows if r.action == retry.APPLIED
    }
    return any(
        r.action == retry.INTENT
        and digest(_detail(r)["request"]) not in completed
        and _detail(r)["request"] != owner
        for r in rows
    )


def _authorize(principal, request, proposal):
    with controls._locked_session() as (db, control):
        prior = _existing(db, proposal["key"])
        if prior:
            saved = _detail(prior)
            _require(saved == proposal, "request_conflict")
            return {"ok": True, "grant_id": prior.id, "replay": True}
        _require(enabled() and control.state == "enabled", "disabled_or_held")
        _require(control.version == proposal["control_version"], "control_changed")
        _require(controls._now() < _time(request.expires_at), "expired")
        pending = _pending(db)
        _require(len(pending) < MAX_PENDING, "pending_limit")
        _require(
            not any(row.task_id == proposal["task_id"] for row in pending),
            "overlapping_grant",
        )
        _require(
            not conflicting_retry_intent(
                db, proposal["task_id"], proposal["owner_request"]
            ),
            "other_owner_intent",
        )
        receipt = db.get(FactoryReceipt, request.target.receipt_id)
        _require(
            receipt is not None and receipt.task_id == proposal["task_id"],
            "owner_changed",
        )
        row = FactoryAudit(
            actor=principal.subject,
            action=GRANT,
            task_id=proposal["task_id"],
            detail_json=dumps(proposal),
        )
        db.add(row)
        db.flush()
        return {
            "ok": True,
            "grant_id": row.id,
            "state": "authorized",
            "preview_sha256": proposal["preview_sha256"],
        }


def _owner_result(db, saved):
    if saved["request"]["target"]["kind"] == "settled_retry":
        from factory import settled_retry_reconciliation as retry

        row = retry._event(db, saved["owner_request"], retry.APPLIED)
        return row["result"] if row else None
    from factory.orchestration import evidence_recovery

    return evidence_recovery._replay(
        db,
        ACTOR,
        {
            **saved["owner_request"],
            "request_key": saved["key"],
            "expected_preview_sha256": saved["owner_preview_sha256"],
        },
    )


def guard(db, actor, kind, owner, owner_sha, token):
    """Called only under the shared owner's control lock at mutation admission."""
    if actor != ACTOR:
        _require(token is None, "unexpected_grant")
        return
    _require(
        isinstance(token, dict) and set(token) == {"grant_id", "call_id"},
        "grant_required",
    )
    grant = db.get(FactoryAudit, token["grant_id"])
    _require(grant is not None and grant.action == GRANT, "grant_missing")
    saved, state = _detail(grant), _state(db, grant.id)
    _require(
        saved["request"]["target"]["kind"] == kind
        and saved["owner_request"] == owner
        and saved["owner_preview_sha256"] == owner_sha,
        "scope_changed",
    )
    _require(enabled() and not _revoked(db, grant.id), "disabled_or_revoked")
    _require(controls._now() < _time(saved["request"]["expires_at"]), "expired")
    _require(
        state["state"] == "claimed"
        and state["call_id"] == token["call_id"]
        and 1 <= state["calls"] <= MAX_CALLS,
        "call_changed",
    )
    _require(
        db.get(controls.FactoryControl, "factory").state == "enabled", "factory_held"
    )
    _require(
        not conflicting_retry_intent(db, grant.task_id, owner), "other_owner_intent"
    )
    controls._audit(
        db,
        ACTOR,
        ADMIT,
        task_id=grant.task_id,
        grant_id=grant.id,
        call_id=token["call_id"],
        calls=state["calls"],
    )


def _claim():
    with controls._locked_session() as (db, control):
        for grant in _pending(db):
            saved, state = _detail(grant), _state(db, grant.id)
            result = _owner_result(db, saved)
            if result is not None:
                _record(db, grant, "applied", calls=state["calls"], result=result)
                continue
            if state["state"] in ("claimed", "uncertain") and controls._now() < _time(
                state["lease_until"]
            ):
                continue
            closed = _revoked(db, grant.id) or controls._now() >= _time(
                saved["request"]["expires_at"]
            )
            if closed or state["calls"] >= MAX_CALLS:
                if state["calls"] == 0:
                    _record(
                        db,
                        grant,
                        "revoked" if _revoked(db, grant.id) else "expired",
                        calls=0,
                    )
                else:
                    _diagnose(db, grant, state)
                # Any admitted/crashed call remains unresolved. Do not let time
                # or exhaustion authorize an overlapping replacement request.
                continue
            if not enabled() or control.state != "enabled":
                continue
            call_id = f"{grant.id}:{state['calls'] + 1}"
            _record(
                db,
                grant,
                "claimed",
                calls=state["calls"] + 1,
                call_id=call_id,
                lease_until=(
                    controls._now() + timedelta(seconds=LEASE_SECONDS)
                ).isoformat(),
            )
            return {"grant_id": grant.id, "call_id": call_id}, saved
    return None


def _diagnose(db, grant, state):
    """Retain one bounded diagnosis request on the existing task, never rearm it."""
    if (
        db.exec(
            select(FactoryAudit.id).where(
                FactoryAudit.action == DIAGNOSIS,
                _field(db, "grant_id", integer=True) == grant.id,
            )
        ).first()
        is not None
    ):
        return
    controls._audit(
        db,
        ACTOR,
        DIAGNOSIS,
        task_id=grant.task_id,
        grant_id=grant.id,
        calls=state["calls"],
        state="unresolved_recovery",
        reason="Authorized recovery has no durable completion and cannot make another call. Inspect this exact grant and shared-owner intent/result journal. Preserve uncertainty and accounting; do not reset attempts, rearm or create a replacement owner.",
    )


def diagnostic_context(db, task_id):
    receipts = select(FactoryReceipt.id).where(FactoryReceipt.task_id == task_id)
    rows = db.exec(
        select(FactoryAudit)
        .where(
            or_(
                FactoryAudit.task_id == task_id,
                and_(
                    FactoryAudit.task_id.is_(None),
                    _field(db, "owner_receipt_id", integer=True).in_(receipts),
                ),
            ),
            FactoryAudit.action.in_((DIAGNOSIS, "learning_diagnosis_required")),
        )
        .order_by(FactoryAudit.id.desc())
        .limit(5)
    ).all()
    return [
        {
            "audit_id": row.id,
            "action": row.action,
            **{
                key: value
                for key, value in _detail(row).items()
                if key
                in {
                    "grant_id",
                    "calls",
                    "state",
                    "reason",
                    "input_sha256",
                    "source",
                    "owner_receipt_id",
                }
            },
        }
        for row in reversed(rows)
    ]


def _finish(token, result):
    with controls._locked_session() as (db, _control):
        grant = db.get(FactoryAudit, token["grant_id"])
        saved, state = _detail(grant), _state(db, grant.id)
        committed = _owner_result(db, saved)
        if committed is not None:
            if state["state"] != "applied":
                _record(db, grant, "applied", calls=state["calls"], result=committed)
            return
        if state.get("call_id") != token["call_id"] or state["state"] != "claimed":
            return
        # Even a guard exception may follow a remote fence. Only an owner's
        # durable APPLIED record establishes completion, not a returned value.
        _record(
            db,
            grant,
            "uncertain",
            calls=state["calls"],
            call_id=token["call_id"],
            lease_until=state["lease_until"],
            result=result,
        )


async def run_once():
    claimed = await asyncio.to_thread(_claim)
    if claimed is None:
        return
    token, saved = claimed
    result = {"ok": False, "state": "outcome_unknown"}
    try:
        if saved["request"]["target"]["kind"] == "settled_retry":
            from factory import settled_retry_reconciliation as retry

            result = await retry.run(
                "apply",
                **saved["owner_request"],
                expected_preview_sha256=saved["owner_preview_sha256"],
                delegation=token,
            )
        else:
            from factory.orchestration import evidence_recovery

            result = await asyncio.to_thread(
                evidence_recovery.apply,
                saved["owner_request"],
                actor=ACTOR,
                request_key=saved["key"],
                expected_preview_sha256=saved["owner_preview_sha256"],
                delegation=token,
            )
    except Exception as exc:
        result = {"ok": False, "state": "outcome_unknown", "error": type(exc).__name__}
    finally:
        await asyncio.shield(asyncio.to_thread(_finish, token, result))


def launch_tick():
    """One asynchronous owner call at a time, on the existing conductor cadence."""
    global _worker
    if not enabled() or (_worker is not None and not _worker.done()):
        return
    if _worker is not None and not _worker.cancelled() and _worker.exception():
        logger.error(
            "recovery delegation observation failed", exc_info=_worker.exception()
        )
    _worker = asyncio.create_task(run_once(), name="factory-authorized-recovery")


async def stop():
    global _worker
    if _worker is not None:
        _worker.cancel()
        await asyncio.gather(_worker, return_exceptions=True)
        _worker = None


def status(grant_id):
    with controls._read_session() as db:
        grant = db.get(FactoryAudit, grant_id)
        _require(grant is not None and grant.action == GRANT, "grant_missing")
        saved = _detail(grant)
        return {
            "grant_id": grant_id,
            "authorizer": saved["authorizer"],
            "execution_actor": ACTOR,
            "request": saved["request"],
            "max_owner_calls": MAX_CALLS,
            "state": _state(db, grant_id),
            "revoked": _revoked(db, grant_id),
            "owner_result": _owner_result(db, saved),
        }


def revoke(principal, grant_id, expected_preview_sha256):
    _require(is_operator(principal), "standing_human_required")
    with controls._locked_session() as (db, _control):
        grant = db.get(FactoryAudit, grant_id)
        _require(grant is not None and grant.action == GRANT, "grant_missing")
        saved = _detail(grant)
        _require(saved["preview_sha256"] == expected_preview_sha256, "preview_changed")
        if not _revoked(db, grant_id):
            controls._audit(
                db, principal.subject, REVOKE, task_id=grant.task_id, grant_id=grant_id
            )
        state = _state(db, grant_id)
        return {
            "ok": True,
            "grant_id": grant_id,
            "revoked": True,
            "calls_started": state["calls"],
            "in_flight_may_finish": state["calls"] > 0,
            "owner_result": _owner_result(db, saved),
        }
