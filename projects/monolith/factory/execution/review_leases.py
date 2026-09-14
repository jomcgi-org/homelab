"""Durable work-review leases, independent of executor heartbeats.

Expiry never refunds capacity or proves a guest stopped. Only a completed
supervisor decision for the exact dispatch can renew permission to continue.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel

INTERVAL_SECONDS = 1800
LEAD_SECONDS = 300
REVIEW_TIMEOUT_SECONDS = 240
RETRY_SECONDS = 300
PREFIX = "synthetic:factory-review:"


class ReservationReview(SQLModel, table=True):
    __tablename__ = "reservation_reviews"
    __table_args__ = {"schema": "agent_sessions"}

    permit_id: int = Field(primary_key=True)
    identity_sha256: str
    lease_expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    requested_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    completed_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    review_session_key: str | None = None
    evidence_sha256: str | None = None
    approved_evidence_sha256: str | None = None
    stop_intent_json: str | None = None
    guidance: str | None = None
    verdict: str | None = None
    rationale: str | None = None
    state: str = "pending"
    attempts: int = 0


def enabled() -> bool:
    return os.getenv("FACTORY_RESERVATION_REVIEW_ENABLED", "false").lower() == "true"


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def now() -> datetime:
    return datetime.now(timezone.utc)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def identity(permit) -> str:
    return digest(
        {
            key: getattr(permit, key)
            for key in (
                "id",
                "local_session_id",
                "session_id",
                "pending_seq",
                "owner",
                "model",
                "created_at",
            )
        }
    )


def deadline(db, permit) -> datetime:
    row = db.get(ReservationReview, permit.id)
    initial = aware(permit.created_at) + timedelta(seconds=INTERVAL_SECONDS)
    if row is None or row.identity_sha256 != identity(permit):
        return initial
    return aware(row.lease_expires_at)


def may_dispatch(db, permit) -> bool:
    if not enabled() or permit.local_session_id.startswith(PREFIX):
        return True
    return now() < deadline(db, permit)


def stop_requested(session_id: int, seq: int, owner: str, dispatch_count: int) -> bool:
    if not enabled():
        return False
    from core.db import get_engine
    from sqlmodel import Session, select
    from factory.execution.models import (
        AgentCapacityReservation,
        AgentSession,
        AgentTurn,
    )
    from factory.execution.constants import UNKNOWN_INVOCATION

    with Session(get_engine()) as db:
        rows = db.exec(
            select(ReservationReview)
            .join(
                AgentCapacityReservation,
                AgentCapacityReservation.id == ReservationReview.permit_id,
            )
            .where(
                AgentCapacityReservation.session_id == session_id,
                AgentCapacityReservation.pending_seq == seq,
                ReservationReview.state == "stopping",
            )
        ).all()
        agent = db.get(AgentSession, session_id)
        turn = db.exec(
            select(AgentTurn).where(
                AgentTurn.session_id == session_id, AgentTurn.seq == seq
            )
        ).first()
        for row in rows:
            intent = json.loads(row.stop_intent_json or "null")
            if (
                not intent
                or not agent
                or not turn
                or turn.stop_reason != UNKNOWN_INVOCATION
            ):
                continue
            before = intent["snapshot"]
            recovery = json.loads(turn.usage_json or "{}").get("recovery", {})
            if (
                before["owner"] == owner
                and before["dispatch_count"] == dispatch_count
                and recovery.get("claim_owner") == owner
                and recovery.get("dispatch_count") == dispatch_count
                and agent.workflow_id == before["workflow_id"]
                and agent.ember_session_id in (None, before["guest_id"])
            ):
                return True
    return False
