"""Observe failed synthetic probes until their exact guest is safely evicted.

This loop never stops, invokes, or retries a guest. Factory and routine outcomes
have separate owners. Legacy CP destroy can precede physical teardown, so only
eviction after the failed turn qualifies for this first supervision slice.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os

from sqlalchemy import BigInteger, DateTime, or_
from sqlmodel import Field, Session, SQLModel, select

from agent_sessions import admission
from agent_sessions.constants import SYNTHETIC_SESSION_PREFIX, UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityReservation,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from core.db import get_engine

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 15
BATCH_SIZE = 4
GET_TIMEOUT_SECONDS = 5
MAX_PROOF_AGE_SECONDS = 30


class ProbeObservation(SQLModel, table=True):
    __tablename__ = "probe_observations"
    __table_args__ = {"schema": "agent_sessions"}

    permit_id: int = Field(primary_key=True, sa_type=BigInteger)
    identity_sha256: str | None = None
    guest_id: str | None = None
    generation: int | None = Field(default=None, sa_type=BigInteger)
    invoke_started_at: int | None = Field(default=None, sa_type=BigInteger)
    cp_updated_at: int | None = Field(default=None, sa_type=BigInteger)
    reason: str = "awaiting_observation"
    evidence_json: str | None = None
    checked_at: datetime = Field(sa_type=DateTime(timezone=True))
    settled_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


def _now():
    return datetime.now(timezone.utc)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def _identity(db, permit):
    """Called under the capacity lock, before any observation or settlement."""
    agent = db.exec(
        select(AgentSession)
        .where(AgentSession.id == permit.session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if (
        agent is None
        or permit.state != "uncertain"
        or permit.tier != "probe"
        or permit.pending_seq != 1
        or permit.routine_job_name is not None
        or agent.admission_tier != "probe"
        or agent.workflow_id is not None
        or agent.node_key is not None
        or agent.local_session_id != permit.local_session_id
        or not agent.local_session_id.startswith(SYNTHETIC_SESSION_PREFIX)
        or not agent.ember_session_id
    ):
        raise ValueError("ineligible_probe")
    if (
        db.exec(
            select(PendingMessage.id).where(PendingMessage.session_id == agent.id)
        ).first()
        is not None
    ):
        raise ValueError("pending_executor")
    turns = db.exec(
        select(AgentTurn).where(AgentTurn.session_id == agent.id).limit(2)
    ).all()
    if len(turns) != 1 or turns[0].seq != permit.pending_seq:
        raise ValueError("changed_attempt")
    turn = turns[0]
    unknown = agent.status == "failed" and turn.stop_reason == UNKNOWN_INVOCATION
    legacy = (
        agent.status == "warn"
        and turn.stop_reason is None
        and permit.outcome == "delivery_error"
    )
    if turn.terminal_reason != "error" or not (unknown or legacy):
        raise ValueError("unrecognised_outcome")
    owners = db.exec(
        select(AgentSession.id)
        .where(AgentSession.ember_session_id == agent.ember_session_id)
        .limit(2)
    ).all()
    permits = db.exec(
        select(AgentCapacityReservation.id)
        .where(
            or_(
                AgentCapacityReservation.session_id == agent.id,
                AgentCapacityReservation.local_session_id == agent.local_session_id,
            )
        )
        .limit(2)
    ).all()
    if owners != [agent.id] or permits != [permit.id]:
        raise ValueError("ambiguous_ownership")
    # Hash the complete turn to detect intervening evidence without copying
    # prompts, results, artifacts, or credentials into lifecycle audit records.
    identity = _sha(
        {
            "permit": permit.model_dump(),
            "session_id": agent.id,
            "local_session_id": agent.local_session_id,
            "guest_id": agent.ember_session_id,
            "last_turn_at": agent.last_turn_at,
            "status": agent.status,
            "turn": turn.model_dump(),
        }
    )
    return agent, turn, identity


def _candidates():
    with Session(get_engine()) as db:
        return list(
            db.exec(
                select(AgentCapacityReservation.id)
                .outerjoin(
                    ProbeObservation,
                    ProbeObservation.permit_id == AgentCapacityReservation.id,
                )
                .where(
                    AgentCapacityReservation.tier == "probe",
                    AgentCapacityReservation.state == "uncertain",
                )
                .order_by(
                    ProbeObservation.checked_at.asc().nulls_first(),
                    AgentCapacityReservation.id,
                )
                .limit(BATCH_SIZE)
            ).all()
        )


def _prepare(permit_id):
    with Session(get_engine()) as db, db.begin():
        admission.lock_pool(db)
        audit = db.get(ProbeObservation, permit_id)
        if audit is not None and audit.settled_at is not None:
            return None
        if audit is None:
            audit = ProbeObservation(permit_id=permit_id, checked_at=_now())
        audit.checked_at = _now()
        db.add(audit)
        permit = db.get(AgentCapacityReservation, permit_id)
        if permit is None:
            audit.reason = "permit_missing"
            return None
        try:
            agent, _, identity = _identity(db, permit)
        except ValueError as exc:
            audit.reason = str(exc)
            return None
        if audit.identity_sha256 not in (None, identity):
            audit.reason = "identity_changed"
            return None
        audit.identity_sha256 = identity
        audit.guest_id = agent.ember_session_id
        audit.reason = "awaiting_observation"
        return {
            "permit_id": permit_id,
            "identity": identity,
            "guest_id": audit.guest_id,
        }


def _record(candidate, observed, observed_at):
    """Commit proof and exact permit settlement together, or retain the hold."""
    with Session(get_engine()) as db, db.begin():
        admission.lock_pool(db)
        audit = db.get(ProbeObservation, candidate["permit_id"])
        if audit is None or audit.settled_at is not None:
            return
        audit.checked_at = _now()
        permit = db.get(AgentCapacityReservation, candidate["permit_id"])
        try:
            if permit is None:
                raise ValueError("permit_missing")
            agent, turn, identity = _identity(db, permit)
            if identity != candidate["identity"] or identity != audit.identity_sha256:
                raise ValueError("identity_changed")
            if not 0 <= (_now() - observed_at).total_seconds() <= MAX_PROOF_AGE_SECONDS:
                raise ValueError("stale_observation")
            if not isinstance(observed, dict):
                raise ValueError("observation_unavailable")
            if observed.get("session_id") != candidate["guest_id"]:
                raise ValueError("guest_mismatch")
            fields = ("generation", "invoke_started_at", "updated_at")
            if any(type(observed.get(k)) is not int or observed[k] < 0 for k in fields):
                raise ValueError("malformed_identity")
            generation, started, updated = (observed[k] for k in fields)
            if started > int(_aware(turn.created_at).timestamp() * 1000):
                raise ValueError("invoke_after_failed_turn")
            # Idle banking increments generation on the same session. It does
            # not change invoke_started_at. Reject regressions and new invokes.
            if audit.generation is not None and (
                generation < audit.generation or started != audit.invoke_started_at
            ):
                raise ValueError("invocation_changed")
            if (
                audit.cp_updated_at is not None and updated < audit.cp_updated_at
            ) or not (started <= updated <= int(observed_at.timestamp() * 1000)):
                raise ValueError("reordered_observation")
            audit.generation = generation
            audit.invoke_started_at = started
            audit.cp_updated_at = updated
            audit.evidence_json = json.dumps(
                {
                    "session_id": candidate["guest_id"],
                    "generation": generation,
                    "invoke_started_at": started,
                    "updated_at": updated,
                    "observed_at": observed_at.isoformat(),
                    "response_sha256": _sha(observed),
                    "evicted": observed.get("state") == "evicted",
                    "last_invoke_at": observed.get("last_invoke_at")
                    if type(observed.get("last_invoke_at")) is int
                    else None,
                },
                sort_keys=True,
            )
            if observed.get("state") != "evicted":
                raise ValueError("awaiting_eviction")
            last_invoke = observed.get("last_invoke_at")
            if type(last_invoke) is not int or not started <= last_invoke <= updated:
                raise ValueError("missing_invoke_completion")
            if updated < int(_aware(turn.created_at).timestamp() * 1000):
                raise ValueError("cessation_precedes_turn")
        except ValueError as exc:
            audit.reason = str(exc)
            db.add(audit)
            return
        admission.confirm_guest_cessation(db, agent)
        audit.reason = "guest_cessation_confirmed"
        audit.settled_at = _now()
        db.add(audit)
        # Keep the failed turn, cost, binding and session history unchanged.
        # The next hourly probe receives its own identity via normal admission.


async def sweep_once(transport):
    for permit_id in await asyncio.to_thread(_candidates):
        try:
            candidate = await asyncio.to_thread(_prepare, permit_id)
            if candidate is None:
                continue
            try:
                observed = await asyncio.wait_for(
                    transport.get_session(candidate["guest_id"]), GET_TIMEOUT_SECONDS
                )
            except Exception:  # A missing or unavailable guest is not cessation.
                observed = None
            await asyncio.to_thread(_record, candidate, observed, _now())
        except Exception:  # One failed candidate must not stop the bounded sweep.
            logger.exception("Failed probe observation for permit %s", permit_id)


async def _loop():
    from agent_sessions.transport import EmberVmShimTransport

    transport = EmberVmShimTransport()
    while True:
        try:
            await sweep_once(transport)
        except Exception:
            logger.exception("Failed probe supervision sweep")
        await asyncio.sleep(INTERVAL_SECONDS)


def start_probe_supervision_loop():
    if os.getenv("AGENT_PROBE_SUPERVISION_ENABLED", "false").lower() != "true":
        return []
    from framework import log_task_exception

    task = asyncio.create_task(_loop(), name="agent-probe-supervision")
    task.add_done_callback(log_task_exception)
    return [task]
