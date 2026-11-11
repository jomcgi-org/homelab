"""Retire a delivered Claude quota probe's already-destroyed guest binding.

This is a bounded cleanup retry, never an invocation or outcome reconciler. The
receipt fence remains until a fresh positive guest read and an atomic, exact
database recheck can retire it with its binding. No guest DELETE is performed.
"""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import or_
from sqlmodel import Session, select

from factory.execution import admission, result_receipts, store
from factory.execution.constants import exact_dispatch_id
from factory.execution.models import (
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from factory.execution.review_leases import aware, digest
from factory.execution.transport import parse_native_turn
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit
from factory.probe_reconciliation import PREFIX

logger = logging.getLogger(__name__)
BATCH_SIZE = 4
MAX_EVIDENCE_AGE_SECONDS = 30
ACTION = "quota_probe_completed_binding_retired"


def _snapshot(db, session_id, *, lock=False):
    agent = (
        store._lock_session(db, session_id)
        if lock
        else db.get(AgentSession, session_id)
    )
    if (
        agent is None
        or agent.admission_tier != "probe"
        or agent.model != "haiku"
        or not agent.local_session_id.startswith(PREFIX)
        or agent.status != "completed"
        or agent.workflow_id is not None
        or agent.node_key is not None
        or agent.node_attempt is not None
        or not agent.ember_session_id
        or agent.ember_lineage_id != agent.ember_session_id
        or agent.prior_ember_lineage_id is not None
        or agent.prior_cli_session_id is not None
        or not agent.result_receipt_fence_id
        or admission.cleanup_pending(db, agent)
        or store.has_unknown_outcome(db, session_id)
    ):
        raise ValueError("probe_not_completed_bound")
    guest = agent.ember_session_id
    if (
        db.exec(
            select(AgentSession.id)
            .where(
                AgentSession.id != session_id,
                or_(
                    AgentSession.ember_session_id == guest,
                    AgentSession.ember_lineage_id == guest,
                    AgentSession.prior_ember_lineage_id == guest,
                ),
            )
            .limit(1)
        ).first()
        is not None
    ):
        raise ValueError("probe_alias_present")
    if (
        db.exec(
            select(PendingMessage.id)
            .where(PendingMessage.session_id == session_id)
            .limit(1)
        ).first()
        is not None
    ):
        raise ValueError("probe_pending_work")
    turns = db.exec(
        select(AgentTurn).where(AgentTurn.session_id == session_id).limit(2)
    ).all()
    query = (
        select(AgentCapacityReservation)
        .where(
            or_(
                AgentCapacityReservation.session_id == session_id,
                AgentCapacityReservation.local_session_id == agent.local_session_id,
            )
        )
        .limit(2)
    )
    permits = db.exec(query.with_for_update() if lock else query).all()
    query = (
        select(AgentResultReceipt)
        .where(AgentResultReceipt.session_id == session_id)
        .limit(2)
    )
    receipts = db.exec(query.with_for_update() if lock else query).all()
    if len(turns) != 1 or len(permits) != 1 or len(receipts) != 1:
        raise ValueError("probe_ambiguous_attempt")
    turn, permit, receipt = turns[0], permits[0], receipts[0]
    if (
        turn.seq != 1
        or turn.terminal_reason != "completed"
        or permit.pending_seq != 1
        or permit.tier != "probe"
        or permit.session_id != session_id
        or permit.local_session_id != agent.local_session_id
        or permit.model != "haiku"
        or permit.workload != "claude-runtime"
        or permit.state != "settled"
        or permit.outcome != "completed"
        or permit.settled_at is None
        or permit.routine_job_name is not None
        or receipt.id != agent.result_receipt_fence_id
        or receipt.local_session_id != agent.local_session_id
        or receipt.seq != 1
        or receipt.claim_owner != permit.owner
        or not receipt.claim_owner
        or receipt.dispatch_count != 1
        or receipt.guest_id != guest
        or receipt.superseded_at is not None
        or receipt.received_at is None
        or not isinstance(receipt.result_body, bytes)
        or len(receipt.result_body) > 65536
    ):
        raise ValueError("probe_identity_changed")
    metadata = receipt.model_dump(exclude={"result_body", "token_sha256"})
    captured = result_receipts._result(db, metadata)
    saved = json.loads(turn.usage_json or "{}").get("native_result_receipt")
    if not isinstance(saved, dict) or any(
        saved.get(k) != v for k, v in captured["provenance"].items()
    ):
        raise ValueError("probe_result_provenance_changed")
    native = parse_native_turn(json.loads(captured["result_body"]), guest)
    if (
        native.is_error
        or native.result != turn.result_text
        or native.terminal_reason != turn.terminal_reason
        or native.stop_reason != turn.stop_reason
        or native.session_id != agent.cli_session_id
    ):
        raise ValueError("probe_saved_result_changed")
    starts = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.action == "quota_probe_started",
            FactoryAudit.actor == "factory:quota-probe",
            FactoryAudit.detail_json.contains(agent.local_session_id, autoescape=True),
        )
        .limit(2)
    ).all()
    if (
        len(starts) != 1
        or json.loads(starts[0].detail_json).get("session_key")
        != agent.local_session_id
        or json.loads(starts[0].detail_json).get("model") != "haiku"
    ):
        raise ValueError("probe_origin_invalid")
    binding = {
        key: getattr(agent, key)
        for key in (
            "id",
            "local_session_id",
            "status",
            "model",
            "admission_tier",
            "workflow_id",
            "node_key",
            "node_attempt",
            "ember_session_id",
            "ember_session_token",
            "ember_session_expires_at",
            "ember_lineage_id",
            "cli_session_id",
            "prior_ember_lineage_id",
            "prior_cli_session_id",
            "result_receipt_fence_id",
            "guest_cleanup_id",
        )
    }
    return {
        "session_id": session_id,
        "seq": 1,
        "permit_id": permit.id,
        "receipt_id": receipt.id,
        "guest_id": guest,
        "claim_owner": receipt.claim_owner,
        "dispatch_count": receipt.dispatch_count,
        "origin_audit_id": starts[0].id,
        "receipt_created_at": aware(receipt.created_at).isoformat(),
        "turn_created_at": aware(turn.created_at).isoformat(),
        "settled_at": aware(permit.settled_at).isoformat(),
        "identity_sha256": digest(
            {
                "binding": binding,
                "turn": turn.model_dump(),
                "permit": permit.model_dump(),
                "receipt": metadata,
                "origin": starts[0].model_dump(),
            }
        ),
    }


def _read(session_id):
    with Session(controls.get_engine()) as db:
        return _snapshot(db, session_id)


def _terminal(snapshot, observed):
    """Only a positive original-generation destroyed record proves this cleanup."""
    if not isinstance(observed, dict):
        raise TypeError("probe_guest_unavailable")
    guest = snapshot["guest_id"]
    fields = (
        "generation",
        "turn_seq",
        "invoke_started_at",
        "last_invoke_at",
        "updated_at",
    )
    if (
        observed.get("session_id") != guest
        or observed.get("lineage_id") != guest
        or observed.get("workload") != "claude-runtime"
        or observed.get("state") != "destroyed"
        or observed.get("terminal_reason") != "destroyed"
        or any(type(observed.get(k)) is not int for k in fields)
        or observed["generation"] != 0
        or observed["turn_seq"] != 1
        or observed.get("stop_intent") is not None
        or observed.get("interrupted_turn") is not None
    ):
        raise ValueError("probe_guest_not_exact_terminal")
    started, last, updated = (observed[k] for k in fields[2:])
    milliseconds = lambda value: int(datetime.fromisoformat(value).timestamp() * 1000)
    if not (
        milliseconds(snapshot["receipt_created_at"])
        <= started
        <= last
        <= milliseconds(snapshot["turn_created_at"])
        <= milliseconds(snapshot["settled_at"])
        <= updated
        <= int(datetime.now(UTC).timestamp() * 1000)
    ):
        raise ValueError("probe_guest_timeline_changed")
    if observed.get("inflight_dispatch_id") not in {
        exact_dispatch_id(snapshot["session_id"], guest, 1, snapshot["claim_owner"], 1),
        f"{guest}:{started}",  # Original non-durable probe invoke identity.
    }:
        raise ValueError("probe_guest_dispatch_changed")
    return {
        key: observed[key]
        for key in (
            *fields,
            "session_id",
            "lineage_id",
            "state",
            "terminal_reason",
            "inflight_dispatch_id",
        )
    }


def _retire(snapshot, observed, read_started):
    with Session(controls.get_engine()) as db:
        result_receipts._bound_observer_transaction(db)
        with controls._locked_session(db):
            current = _snapshot(db, snapshot["session_id"], lock=True)
            if current != snapshot:
                raise ValueError("probe_snapshot_changed")
            if not 0 <= time.monotonic() - read_started <= MAX_EVIDENCE_AGE_SECONDS:
                raise ValueError("probe_guest_evidence_stale")
            evidence = _terminal(current, observed)
            agent = db.get(AgentSession, current["session_id"])
            agent.prior_ember_lineage_id = agent.ember_lineage_id
            if agent.cli_session_id:
                agent.prior_cli_session_id = agent.cli_session_id
            agent.ember_session_id = None
            agent.ember_session_token = None
            agent.ember_session_expires_at = None
            agent.ember_lineage_id = None
            agent.cli_session_id = None
            agent.result_receipt_fence_id = None
            db.add(agent)
            controls._audit(
                db,
                "factory:quota-probe-cleanup",
                ACTION,
                **current,
                terminal_evidence=evidence,
            )
        db.commit()
    return True


def _candidates(after_id):
    with Session(controls.get_engine()) as db:
        return db.exec(
            select(AgentSession.id)
            .where(
                AgentSession.id < after_id if after_id else True,
                AgentSession.local_session_id.startswith(PREFIX),
                AgentSession.admission_tier == "probe",
                AgentSession.status == "completed",
                AgentSession.workflow_id.is_(None),
                AgentSession.ember_session_id.isnot(None),
                AgentSession.result_receipt_fence_id.isnot(None),
            )
            .order_by(AgentSession.id.desc())
            .limit(BATCH_SIZE)
        ).all()


async def sweep_once(after_id=0):
    from factory.execution.execution_api import _transport

    candidates = await asyncio.to_thread(_candidates, after_id)
    for session_id in candidates:
        try:
            snapshot = await asyncio.to_thread(_read, session_id)
            started = time.monotonic()
            observed = await _transport.get_session(snapshot["guest_id"])
            await asyncio.to_thread(_retire, snapshot, observed, started)
        except Exception as exc:  # noqa: BLE001 - continue past held probes
            # Exception strings can contain database parameters or credentials.
            logger.info(
                "Completed quota probe cleanup deferred session=%s type=%s",
                session_id,
                type(exc).__name__,
            )
    return candidates[-1] if len(candidates) == BATCH_SIZE else 0
