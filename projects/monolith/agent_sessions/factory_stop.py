"""Session-owned identity, fencing and heartbeat checks for factory stops.

Factory owns stop authority and audit records. These endpoints own session,
turn and permit state, using the caller's transaction so intent and UNKNOWN
commit together. No endpoint here calls Ember or treats cancellation as proof
that execution has ceased.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

from sqlmodel import Session, select

from agent_sessions import admission
from agent_sessions.constants import INTERRUPTED_TERMINAL_REASONS, UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityReservation,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from agent_sessions.reconciliation import (
    _factory_owner,
    _locked_session,
    read_uncertain_factory_attempt,
)


def _stamp(value) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("missing_factory_dispatch_identity")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def inspect_factory_attempt_stop(
    db: Session, pin: dict, session_id: int
) -> tuple[dict, bool]:
    """Read the exact dispatch identity, returning values rather than ORM rows.

    Caller holds factory control; lock order is control, capacity pool, session.
    The caller owns commit/rollback and the factory's run/start validation.
    """
    admission.lock_pool(db)
    agent = _factory_owner(db, pin, session_id)
    if agent is None:
        raise ValueError("missing_factory_owner")
    agent = _locked_session(db, agent.id)
    if not agent.ember_session_id or agent.result_receipt_fence_id is not None:
        raise ValueError("pending_or_missing_factory_guest")
    owners = db.exec(
        select(AgentSession.id).where(
            AgentSession.ember_session_id == agent.ember_session_id
        )
    ).all()
    permits = db.exec(
        select(AgentCapacityReservation).where(
            AgentCapacityReservation.session_id == agent.id
        )
    ).all()
    pending = db.exec(
        select(PendingMessage).where(PendingMessage.session_id == agent.id)
    ).all()
    turns = db.exec(select(AgentTurn).where(AgentTurn.session_id == agent.id)).all()
    if owners != [agent.id] or len(permits) != 1 or len(pending) > 1 or len(turns) > 1:
        raise ValueError("ambiguous_factory_attempt")
    permit = permits[0]
    if (
        permit.pending_seq != 1
        or permit.local_session_id != agent.local_session_id
        or permit.tier != "project"
        or permit.routine_job_name is not None
        or not permit.owner
        or permit.state not in {"running", "uncertain"}
    ):
        raise ValueError("factory_attempt_not_uncertain_or_running")
    if pending:
        head = pending[0]
        if (
            head.seq != 1
            or head.claimed_by_replica != permit.owner
            or permit.state != "running"
            or (turns and turns[0].terminal_reason not in INTERRUPTED_TERMINAL_REASONS)
        ):
            raise ValueError("factory_pending_owner_changed")
        count, dispatched = head.dispatch_count, head.last_dispatch_at
    else:
        if (
            len(turns) != 1
            or turns[0].seq != 1
            or turns[0].terminal_reason != "error"
            or permit.state != "uncertain"
            or not (
                (
                    agent.status == "failed"
                    and turns[0].stop_reason == UNKNOWN_INVOCATION
                )
                or (
                    agent.status == "warn"
                    and turns[0].stop_reason is None
                    and permit.outcome == "delivery_error"
                )
            )
        ):
            raise ValueError("factory_attempt_not_uncertain")
        recovery = json.loads(turns[0].usage_json or "{}").get("recovery", {})
        if recovery.get("claim_owner") != permit.owner:
            raise ValueError("factory_pending_owner_changed")
        count, dispatched = (
            recovery.get("dispatch_count"),
            recovery.get("last_dispatch_at"),
        )
    if type(count) is not int or count < 1:
        raise ValueError("missing_factory_dispatch_identity")
    return {
        "session_id": agent.id,
        "permit_id": permit.id,
        "guest_id": agent.ember_session_id,
        "workflow_id": pin["workflow_id"],
        "seq": 1,
        "dispatch_count": count,
        "claim_owner": permit.owner,
        "dispatched_at": _stamp(dispatched),
    }, bool(pending)


def fence_factory_attempt_stop(
    db: Session, pin: dict, session_id: int, expected: dict
) -> dict:
    """Fence only the dispatch the factory observed, without committing.

    This endpoint verifies its own session identity even if the caller already
    read it. It leaves capacity uncertain until the separate exact-stop proof
    is consumed; the original turn result and cost remain unknown.
    """
    identity, pending = inspect_factory_attempt_stop(db, pin, session_id)
    if any(expected.get(key) != value for key, value in identity.items()):
        raise ValueError("factory_attempt_changed")
    if pending:
        from agent_sessions.store import finish_unknown_pending_in_session

        if not finish_unknown_pending_in_session(
            db,
            session_id,
            identity["seq"],
            identity["claim_owner"],
            identity["dispatch_count"],
            "factory_attempt_stop",
            expected_guest_id=identity["guest_id"],
            expected_workflow_id=identity["workflow_id"],
        ):
            raise ValueError("factory_attempt_changed")
    return read_uncertain_factory_attempt(db, pin, session_id)


def executor_stop_requested(
    session_id: int, seq: int, claim_owner: str, dispatch_count: int
) -> bool:
    """Match a committed factory request to this executor's UNKNOWN turn."""
    from core.db import get_engine
    from swarm.api import read_factory_attempt_stop_request

    with Session(get_engine()) as db:
        agent = db.get(AgentSession, session_id)
        if agent is None or not agent.local_session_id.startswith("factory:"):
            return False
        fields = agent.local_session_id.split(":")
        if len(fields) != 4:
            return False
        request = read_factory_attempt_stop_request(
            db, fields[1], session_id, seq, claim_owner, dispatch_count
        )
        if request is None:
            return False
        saved = request["identity"]
        turn = db.exec(
            select(AgentTurn).where(
                AgentTurn.session_id == session_id,
                AgentTurn.seq == seq,
            )
        ).one_or_none()
        if turn is None or turn.stop_reason != UNKNOWN_INVOCATION:
            return False
        recovery = json.loads(turn.usage_json or "{}").get("recovery", {})
        binding_matches = agent.ember_session_id == saved["guest_id"]
        if agent.ember_session_id is None:
            # Exact proof can retire the binding before this heartbeat sees
            # the request. Factory owns that proof's committed audit record.
            binding_matches = request["cessation_confirmed"]
        return (
            recovery.get("claim_owner") == claim_owner
            and recovery.get("dispatch_count") == saved["dispatch_count"]
            and agent.workflow_id == saved["workflow_id"]
            and binding_matches
        )
