"""Resolve one proven undelivered Claude probe without erasing its history."""

import json
from types import SimpleNamespace

from sqlalchemy import or_
from sqlmodel import select

from factory.execution import admission, permit_supervision as supervision
from factory.execution.constants import UNKNOWN_INVOCATION
from factory.execution.models import (
    AgentSession,
    AgentTurn,
    AgentCapacityReservation,
    AgentResultReceipt,
    PendingMessage,
    ProbeObservation,
)
from factory.execution.review_leases import digest
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit

ACTION = "quota_probe_undelivered_resolved"
PREFIX = "synthetic:factory-quota:"


def proof(db, session_id, seq, permit_id):
    """Recheck all evidence, including the original supervision settlement."""
    if (
        any(type(v) is not int or v < 1 for v in (session_id, seq, permit_id))
        or seq != 1
    ):
        raise ValueError("probe_invalid_identity")
    agent = db.exec(
        select(AgentSession).where(AgentSession.id == session_id).with_for_update()
    ).first()
    permit = db.exec(
        select(AgentCapacityReservation)
        .where(AgentCapacityReservation.id == permit_id)
        .with_for_update()
    ).first()
    observed = db.get(ProbeObservation, permit_id)
    if (
        agent is None
        or permit is None
        or observed is None
        or agent.admission_tier != "probe"
        or agent.status != "failed"
        or not agent.local_session_id.startswith(PREFIX)
        or permit.session_id != session_id
        or permit.local_session_id != agent.local_session_id
        or permit.pending_seq != seq
        or permit.tier != "probe"
        or permit.state != "settled"
        or permit.outcome != "no_guest_bound"
        or permit.settled_at is None
        or permit.routine_job_name is not None
        or agent.workflow_id is not None
        or agent.node_key is not None
        or agent.node_attempt is not None
        or agent.ember_session_id is not None
        or agent.ember_session_token is not None
        or agent.result_receipt_fence_id is not None
        or supervision._has_binding_evidence(agent)
        or admission.cleanup_pending(db, agent)
    ):
        raise ValueError("probe_not_settled_undelivered")
    if (
        observed.reason != "no_guest_bound"
        or observed.settled_at is None
        or not observed.identity_sha256
        or observed.guest_id is not None
        or observed.generation is not None
        or observed.invoke_started_at is not None
        or observed.cp_updated_at is not None
        or observed.evidence_json not in (None, "{}")
    ):
        raise ValueError("probe_settlement_evidence_invalid")
    starts = []
    for audit in db.exec(
        select(FactoryAudit).where(
            FactoryAudit.action == "quota_probe_started",
            FactoryAudit.actor == "factory:quota-probe",
        )
    ).all():
        try:
            detail = json.loads(audit.detail_json)
            if detail.get("session_key") == agent.local_session_id:
                starts.append(audit)
        except (ValueError, AttributeError):
            continue
    if len(starts) != 1 or json.loads(starts[0].detail_json).get("model") != "haiku":
        raise ValueError("probe_origin_invalid")
    turns = db.exec(select(AgentTurn).where(AgentTurn.session_id == session_id)).all()
    permits = db.exec(
        select(AgentCapacityReservation).where(
            or_(
                AgentCapacityReservation.session_id == session_id,
                AgentCapacityReservation.local_session_id == agent.local_session_id,
            )
        )
    ).all()
    if len(turns) != 1 or len(permits) != 1 or permits[0].id != permit_id:
        raise ValueError("probe_ambiguous_attempt")
    turn = turns[0]
    if (
        turn.seq != seq
        or turn.terminal_reason != "error"
        or turn.stop_reason != UNKNOWN_INVOCATION
    ):
        raise ValueError("probe_turn_changed")
    if (
        db.exec(
            select(PendingMessage.id).where(PendingMessage.session_id == session_id)
        ).first()
        is not None
        or db.exec(
            select(AgentResultReceipt.id).where(
                AgentResultReceipt.session_id == session_id
            )
        ).first()
        is not None
    ):
        raise ValueError("probe_delivery_or_pending_work")
    try:
        usage = json.loads(turn.usage_json or "{}")
        recovery = usage["recovery"]
        # Reuse the original dispatch proof. The permit's outcome was changed
        # by supervision, so use a detached view, never mutate the ORM record.
        supervision._no_guest_delivery(
            SimpleNamespace(outcome="delivery_error", owner=permit.owner), recovery
        )
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("probe_dispatch_evidence_invalid") from exc
    # No secret, prompt or result content leaves this digest.
    return {
        "session_id": session_id,
        "seq": seq,
        "permit_id": permit_id,
        "identity_sha256": digest(
            {
                "session": agent.model_dump(),
                "turn": turn.model_dump(),
                "permit": permit.model_dump(),
                "observation": observed.model_dump(),
                "origin": starts[0].model_dump(),
            }
        ),
    }


def resolutions(db):
    """Only exact, still-valid audited proofs suppress the old unknown marker."""
    resolved = []
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(FactoryAudit.action == ACTION)
    ).all():
        try:
            saved = json.loads(raw)["proposal"]["proof"]
            if (
                proof(db, saved["session_id"], saved["seq"], saved["permit_id"])
                == saved
            ):
                resolved.append(saved["session_id"])
        except (ValueError, TypeError, KeyError):
            continue
    return resolved


def _preview(db, control, session_id, seq, permit_id, reason):
    reason = controls._text(reason, "reason")
    proposal = {
        "ok": True,
        "version": control.version,
        "generation": json.loads(control.policy_json).get("generation", 0),
        "reason": reason,
        "proof": proof(db, session_id, seq, permit_id),
        "effect": "resolve_only_this_undelivered_probe",
        "dispatch": False,
    }
    proposal["preview_sha256"] = digest(proposal)
    return proposal


def preview(session_id, seq, permit_id, reason):
    with controls._locked_session() as (db, control):
        admission.lock_pool(db)
        return _preview(db, control, session_id, seq, permit_id, reason)


def apply(
    actor, session_id, seq, permit_id, reason, request_key, expected_preview_sha256
):
    actor = controls._text(actor, "actor")
    request = dict(
        session_id=session_id,
        seq=seq,
        permit_id=permit_id,
        reason=reason,
        request_key=controls._text(request_key, "request_key"),
        expected_preview_sha256=expected_preview_sha256,
    )
    with controls._locked_session() as (db, control):
        admission.lock_pool(db)
        for raw in db.exec(
            select(FactoryAudit.detail_json).where(
                FactoryAudit.action == ACTION, FactoryAudit.actor == actor
            )
        ).all():
            old = json.loads(raw)
            if old["request"]["request_key"] == request_key:
                if old["request"] != request:
                    raise ValueError("probe_request_conflict")
                return old["result"]
        proposal = _preview(db, control, session_id, seq, permit_id, reason)
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("probe_preview_changed")
        if session_id in resolutions(db):
            raise ValueError("probe_already_resolved")
        control.version += 1
        control.actor, control.updated_at = actor, controls._now()
        result = {
            "ok": True,
            "version": control.version,
            "session_id": session_id,
            "seq": seq,
            "permit_id": permit_id,
            "resolved": True,
            "dispatched": False,
            "quota_refreshed": False,
        }
        controls._audit(
            db, actor, ACTION, request=request, proposal=proposal, result=result
        )
        return result
