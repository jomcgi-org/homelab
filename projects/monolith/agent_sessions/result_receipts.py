"""Capture exact native results without changing execution or accounting state.

The guest publishes before writing its synchronous response. A committed receipt
survives a lost response and deletion of the original pending row. It is evidence
only: no result adoption, capacity release, guest stop, or retry occurs here.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from uuid import uuid4

from sqlalchemy import delete, update
from sqlmodel import Session, select

from agent_sessions import admission
from agent_sessions.constants import INTERRUPTED_TERMINAL_REASONS
from agent_sessions.models import (
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from core.db import get_engine

MAX_RESULT_BYTES = 32 * 1024 * 1024
ACCEPT_HOURS = 13
RETAIN_DAYS = 7
PRUNE_BATCH = 100
PRUNE_INTERVAL_SECONDS = 300
logger = logging.getLogger(__name__)


class ReceiptRejected(ValueError):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status


def enabled() -> bool:
    return os.getenv("AGENT_RESULT_RECEIPTS_ENABLED", "false").lower() == "true"


def _now():
    return datetime.now(timezone.utc)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _sha(value: bytes):
    return hashlib.sha256(value).hexdigest()


def prepare_receipt(
    session_id: int,
    claim_owner: str,
    dispatch_count: int,
    guest_id: str,
    request_body: bytes,
) -> dict:
    """Pin one physical invoke while the exact executor still owns the lane.

    Called immediately before each POST, after admission. The request digest
    excludes the receipt credential subsequently added to the guest envelope.
    A new physical invoke supersedes earlier receipts for the same turn, even
    if transport recovery reuses its dispatch_count or Ember session.
    """
    if (
        type(session_id) is not int
        or session_id < 1
        or type(dispatch_count) is not int
        or dispatch_count < 1
        or not isinstance(claim_owner, str)
        or not claim_owner
        or not isinstance(guest_id, str)
        or not guest_id
        or not isinstance(request_body, bytes)
    ):
        raise ReceiptRejected(409, "invalid_invocation_identity")
    # Import lazily: the existing store imports the transport which calls here.
    from agent_sessions import store

    with Session(get_engine()) as db, db.begin():
        admission.lock_pool(db)
        db.execute(
            update(AgentSession)
            .where(AgentSession.id == session_id)
            .values(last_turn_at=AgentSession.last_turn_at)
        )
        agent = db.get(AgentSession, session_id, populate_existing=True)
        pending = db.exec(
            select(PendingMessage)
            .where(PendingMessage.session_id == session_id)
            .order_by(PendingMessage.seq)
            .limit(1)
        ).first()
        now = _now()
        if (
            agent is None
            or agent.status in {"failed", "awaiting_login"}
            or agent.ember_session_id != guest_id
            or pending is None
            or pending.claimed_by_replica != claim_owner
            or pending.dispatch_count != dispatch_count
            or pending.claimed_at is None
            or not 0 <= (now - _aware(pending.claimed_at)).total_seconds() < 30
        ):
            raise ReceiptRejected(409, "executor_ownership_changed")
        permit = db.exec(
            select(AgentCapacityReservation).where(
                AgentCapacityReservation.session_id == session_id,
                AgentCapacityReservation.pending_seq == pending.seq,
            )
        ).one_or_none()
        previous = db.exec(
            select(AgentTurn).where(
                AgentTurn.session_id == session_id,
                AgentTurn.seq == pending.seq,
            )
        ).one_or_none()
        if (
            permit is None
            or permit.state != "running"
            or permit.owner != claim_owner
            or permit.local_session_id != agent.local_session_id
            or store.has_unknown_outcome(db, session_id)
            or (
                previous is not None
                and previous.terminal_reason not in INTERRUPTED_TERMINAL_REASONS
            )
        ):
            raise ReceiptRejected(409, "invocation_not_admitted")
        db.execute(
            update(AgentResultReceipt)
            .where(
                AgentResultReceipt.session_id == session_id,
                AgentResultReceipt.seq == pending.seq,
                AgentResultReceipt.superseded_at.is_(None),
            )
            .values(superseded_at=now)
        )
        token = secrets.token_urlsafe(32)
        receipt = AgentResultReceipt(
            id=uuid4().hex,
            token_sha256=_sha(token.encode()),
            session_id=session_id,
            local_session_id=agent.local_session_id,
            seq=pending.seq,
            dispatch_count=dispatch_count,
            claim_owner=claim_owner,
            guest_id=guest_id,
            request_sha256=_sha(request_body),
            created_at=now,
            accept_until=now + timedelta(hours=ACCEPT_HOURS),
            retain_until=now + timedelta(days=RETAIN_DAYS),
        )
        db.add(receipt)
        return {"id": receipt.id, "token": token}


def capture_result(receipt_id: str, token: str, body: bytes) -> dict:
    """Acknowledge only a committed, immutable body, including late callbacks."""
    if (
        not isinstance(receipt_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", receipt_id) is None
        or not isinstance(token, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token) is None
    ):
        raise ReceiptRejected(401, "invalid_receipt_token")
    if len(body) > MAX_RESULT_BYTES:
        raise ReceiptRejected(413, "result_too_large")
    try:
        record = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ReceiptRejected(422, "invalid_result_json") from None
    if not isinstance(record, dict):
        raise ReceiptRejected(422, "result_must_be_object")
    digest = _sha(body)
    with Session(get_engine()) as db, db.begin():
        # A no-op row update serializes duplicates on PostgreSQL and file SQLite.
        # It acquires only the receipt row; callbacks never lock execution state.
        db.execute(
            update(AgentResultReceipt)
            .where(AgentResultReceipt.id == receipt_id)
            .values(created_at=AgentResultReceipt.created_at)
        )
        receipt = db.get(AgentResultReceipt, receipt_id, populate_existing=True)
        if receipt is None or not hmac.compare_digest(
            receipt.token_sha256, _sha(token.encode())
        ):
            raise ReceiptRejected(401, "invalid_receipt_token")
        now = _now()
        if now >= _aware(receipt.retain_until):
            raise ReceiptRejected(410, "receipt_expired")
        if receipt.result_sha256 is not None:
            if receipt.result_sha256 != digest or receipt.result_body != body:
                raise ReceiptRejected(409, "receipt_result_conflict")
        else:
            if now >= _aware(receipt.accept_until):
                raise ReceiptRejected(410, "receipt_acceptance_expired")
            receipt.result_body = body
            receipt.result_sha256 = digest
            receipt.received_at = now
            db.add(receipt)
        return {"receipt_id": receipt_id, "result_sha256": digest}


def prune_expired_receipts() -> int:
    """Bound retained bodies and credentials; expiry never reconciles execution."""
    with Session(get_engine()) as db, db.begin():
        ids = list(
            db.exec(
                select(AgentResultReceipt.id)
                .where(AgentResultReceipt.retain_until <= _now())
                .order_by(AgentResultReceipt.retain_until, AgentResultReceipt.id)
                .limit(PRUNE_BATCH)
            ).all()
        )
        if ids:
            db.execute(delete(AgentResultReceipt).where(AgentResultReceipt.id.in_(ids)))
        return len(ids)


def start_receipt_retention_loop():
    """Retain the expiry sweep even when new receipt minting is disabled."""
    from framework import log_task_exception

    async def run():
        while True:
            try:
                await asyncio.to_thread(prune_expired_receipts)
            except Exception as exc:
                # Database exception strings can contain bound result bodies.
                logger.error("Result receipt retention failed: %s", type(exc).__name__)
            await asyncio.sleep(PRUNE_INTERVAL_SECONDS)

    task = asyncio.create_task(run(), name="agent-result-receipt-retention")
    task.add_done_callback(log_task_exception)
    return [task]
