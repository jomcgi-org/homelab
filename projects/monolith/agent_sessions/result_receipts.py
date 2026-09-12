"""Capture native results and validate consumption by the exact active executor.

The guest publishes before writing its synchronous response. A committed receipt
survives a lost response and deletion of the original pending row. It is evidence
only until the normal result writer validates and persists it. A consumed receipt
and its session fence then retain the exact guest cleanup identity and deadline;
the execution API owns remote destruction and recurring retry.
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

from sqlalchemy import delete, exists, or_, text, update
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
CLEANUP_BATCH = 100
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
            or agent.result_receipt_fence_id is not None
            or admission.cleanup_pending(db, agent)
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


def _receipt_metadata(db: Session, receipt_id: str) -> dict | None:
    # Polling must not load the native body or the callback credential hash.
    columns = [
        column
        for column in AgentResultReceipt.__table__.columns
        if column.name not in {"result_body", "token_sha256"}
    ]
    row = (
        db.execute(select(*columns).where(AgentResultReceipt.id == receipt_id))
        .mappings()
        .one_or_none()
    )
    return None if row is None else dict(row)


def _check_identity(receipt, session_id, claim_owner, dispatch_count, guest_id):
    if (
        type(session_id) is not int
        or session_id < 1
        or type(dispatch_count) is not int
        or dispatch_count < 1
        or not isinstance(claim_owner, str)
        or not claim_owner
        or not isinstance(guest_id, str)
        or not guest_id
        or any(
            receipt[key] != value
            for key, value in {
                "session_id": session_id,
                "claim_owner": claim_owner,
                "dispatch_count": dispatch_count,
                "guest_id": guest_id,
            }.items()
        )
    ):
        raise ReceiptRejected(409, "receipt_identity_changed")


def _newer_work_exists(db: Session, receipt: dict) -> bool:
    # Enqueue remains allowed while the observer fence holds. Only a dispatched
    # or claimed follow-up conflicts; an untouched queued message does not.
    return (
        db.exec(
            select(PendingMessage.id)
            .where(
                PendingMessage.session_id == receipt["session_id"],
                PendingMessage.seq >= receipt["seq"],
                or_(
                    PendingMessage.seq > receipt["seq"],
                    PendingMessage.dispatch_count != receipt["dispatch_count"],
                    PendingMessage.claimed_by_replica != receipt["claim_owner"],
                ),
                or_(
                    PendingMessage.dispatch_count > 0,
                    PendingMessage.claimed_by_replica.isnot(None),
                ),
            )
            .limit(1)
        ).first()
        is not None
        or db.exec(
            select(AgentTurn.id)
            .where(
                AgentTurn.session_id == receipt["session_id"],
                AgentTurn.seq > receipt["seq"],
            )
            .limit(1)
        ).first()
        is not None
    )


def _held_dispatch(db: Session, receipt: dict, now: datetime) -> bool:
    """Whether a live response-loss hold names this exact receipt.

    An executor that loses its synchronous response leaves the claim in place
    and stops refreshing its stamp, so the thirty-second liveness window is not
    the only thing that can authorize a read. The hold is a durable, bounded
    record written by that same executor for that exact dispatch, and it names
    the one receipt it is waiting for, so it authorizes reading that receipt
    and no other. Every other ownership condition above still applies.
    """
    from agent_sessions import store

    pending = db.exec(
        select(PendingMessage)
        .where(PendingMessage.session_id == receipt["session_id"])
        .order_by(PendingMessage.seq)
    ).first()
    if pending is None:
        return False
    turn = db.exec(
        select(AgentTurn).where(
            AgentTurn.session_id == receipt["session_id"],
            AgentTurn.seq == receipt["seq"],
        )
    ).one_or_none()
    hold = store._response_lost_hold(turn, pending, now)
    return hold is not None and hold["receipt_id"] == receipt["id"]


def _active_owner(db: Session, receipt: dict) -> AgentSession:
    from agent_sessions import store

    agent = db.get(AgentSession, receipt["session_id"], populate_existing=True)
    pending = db.exec(
        select(
            PendingMessage.seq,
            PendingMessage.claimed_by_replica,
            PendingMessage.dispatch_count,
            PendingMessage.claimed_at,
        )
        .where(PendingMessage.session_id == receipt["session_id"])
        .order_by(PendingMessage.seq)
        .limit(1)
    ).first()
    now = _now()
    if (
        receipt["superseded_at"] is not None
        or now >= _aware(receipt["retain_until"])
        or agent is None
        or agent.local_session_id != receipt["local_session_id"]
        or agent.ember_session_id != receipt["guest_id"]
        or agent.status in {"failed", "awaiting_login"}
        or agent.result_receipt_fence_id not in {None, receipt["id"]}
        or pending is None
        or pending.seq != receipt["seq"]
        or pending.claimed_by_replica != receipt["claim_owner"]
        or pending.dispatch_count != receipt["dispatch_count"]
        or pending.claimed_at is None
        or not (
            0 <= (now - _aware(pending.claimed_at)).total_seconds() < 30
            or _held_dispatch(db, receipt, now)
        )
        or store.has_unknown_outcome(db, receipt["session_id"])
        or _newer_work_exists(db, receipt)
    ):
        raise ReceiptRejected(409, "executor_ownership_changed")
    permit = db.exec(
        select(AgentCapacityReservation)
        .where(
            AgentCapacityReservation.session_id == receipt["session_id"],
            AgentCapacityReservation.pending_seq == receipt["seq"],
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    previous = db.exec(
        select(AgentTurn.id, AgentTurn.terminal_reason).where(
            AgentTurn.session_id == receipt["session_id"],
            AgentTurn.seq == receipt["seq"],
        )
    ).one_or_none()
    if (
        permit is None
        or permit.state != "running"
        or permit.owner != receipt["claim_owner"]
        or permit.local_session_id != receipt["local_session_id"]
        or (
            previous is not None
            and previous.terminal_reason not in INTERRUPTED_TERMINAL_REASONS
        )
    ):
        raise ReceiptRejected(409, "invocation_not_admitted")
    return agent


def _result(db: Session, receipt: dict) -> dict:
    body = db.exec(
        select(AgentResultReceipt.result_body).where(
            AgentResultReceipt.id == receipt["id"]
        )
    ).one_or_none()
    if (
        not isinstance(body, bytes)
        or len(body) > MAX_RESULT_BYTES
        or _sha(body) != receipt["result_sha256"]
        or receipt["received_at"] is None
    ):
        raise ReceiptRejected(409, "receipt_result_changed")
    provenance = {
        key: receipt[key]
        for key in (
            "session_id",
            "local_session_id",
            "seq",
            "claim_owner",
            "dispatch_count",
            "guest_id",
            "request_sha256",
            "result_sha256",
        )
    }
    provenance["receipt_id"] = receipt["id"]
    provenance["received_at"] = _aware(receipt["received_at"]).isoformat()
    if _held_dispatch(db, receipt, _now()):
        # Server-owned provenance, so a recovered turn reads as recovered
        # rather than as an ordinary synchronous result. Both the poll and the
        # writer derive it here, so they cannot disagree about one receipt.
        provenance["response_lost_recovery"] = True
    return {
        "result_body": body,
        "result_sha256": receipt["result_sha256"],
        "provenance": provenance,
    }


def _bound_observer_transaction(db: Session) -> None:
    # Optional observation must not monopolize an executor on a database lock.
    # SET LOCAL expires with this transaction and never changes the shared pool
    # connection's defaults. Connection establishment is bounded by the caller's
    # dedicated executor; these limits apply once PostgreSQL accepts the query.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
        db.execute(text("SET LOCAL statement_timeout = '3s'"))


def read_active_result(
    receipt_id: str,
    session_id: int,
    claim_owner: str,
    dispatch_count: int,
    guest_id: str,
    request_sha256: str,
) -> dict | None:
    """Poll committed evidence without locks; the writer must revalidate it.

    A missing or not-yet-captured receipt returns None. Changed ownership is an
    explicit refusal, including when no native body has arrived yet. Acceptance
    expiry governs capture; a committed body remains readable until retention.
    """
    with Session(get_engine()) as db:
        _bound_observer_transaction(db)
        receipt = _receipt_metadata(db, receipt_id)
        if receipt is None:
            return None
        _check_identity(receipt, session_id, claim_owner, dispatch_count, guest_id)
        if receipt["request_sha256"] != request_sha256:
            raise ReceiptRejected(409, "receipt_request_changed")
        _active_owner(db, receipt)
        if receipt["result_sha256"] is None:
            return None
        return _result(db, receipt)


def read_held_result(hold: dict) -> dict | None:
    """Read the committed result of a dispatch whose response was lost.

    Identical identity and ownership checks to the live observer's poll; the
    live claim stamp is simply replaced by the durable hold. A missing or
    not-yet-captured body returns None so the caller keeps waiting inside its
    bound, while changed ownership is an explicit refusal.
    """
    with Session(get_engine()) as db:
        _bound_observer_transaction(db)
        receipt = _receipt_metadata(db, hold["receipt_id"])
        if receipt is None:
            return None
        _check_identity(
            receipt,
            hold["session_id"],
            hold["claim_owner"],
            hold["dispatch_count"],
            hold["guest_id"],
        )
        if type(hold["seq"]) is not int or receipt["seq"] != hold["seq"]:
            raise ReceiptRejected(409, "receipt_identity_changed")
        # The same request check the live poll makes. A hold the lease backstop
        # reconstructed has no record of the request body, so it carries no
        # digest and selects its receipt by the full dispatch identity instead.
        if (
            hold.get("request_sha256") is not None
            and receipt["request_sha256"] != hold["request_sha256"]
        ):
            raise ReceiptRejected(409, "receipt_request_changed")
        _active_owner(db, receipt)
        if receipt["result_sha256"] is None:
            return None
        return _result(db, receipt)


def validate_active_result(
    db: Session,
    *,
    receipt_id: str,
    result_sha256: str,
    session_id: int,
    seq: int,
    claim_owner: str,
    dispatch_count: int,
    guest_id: str,
) -> dict:
    """Validate and stage the guest cleanup fence in the writer transaction.

    The caller holds pool, session and pending locks, and must parse/compare the
    returned body before committing its normal turn write. This function never
    commits, settles admission or changes the original turn history.
    """
    # Lock after execution rows. Capture and retention never lock those rows.
    db.execute(
        update(AgentResultReceipt)
        .where(AgentResultReceipt.id == receipt_id)
        .values(created_at=AgentResultReceipt.created_at)
    )
    receipt = _receipt_metadata(db, receipt_id)
    if receipt is None:
        raise ReceiptRejected(409, "receipt_unavailable")
    _check_identity(receipt, session_id, claim_owner, dispatch_count, guest_id)
    if type(seq) is not int or seq != receipt["seq"]:
        raise ReceiptRejected(409, "receipt_identity_changed")
    if result_sha256 is None or result_sha256 != receipt["result_sha256"]:
        raise ReceiptRejected(409, "receipt_result_changed")
    agent = _active_owner(db, receipt)
    result = _result(db, receipt)
    if receipt["response_observed_at"] is None and not _held_dispatch(
        db, receipt, _now()
    ):
        # The fence exists so a follow-up dispatch waits for the original POST's
        # own response. A hold means that POST's observer is already gone and no
        # one is left to clear the fence, so setting one here would strand the
        # session and its guest forever. The hold itself is the fence, and it is
        # consumed by this same transaction deleting the pending row.
        agent.result_receipt_fence_id = receipt_id
        db.add(agent)
    return result


def mark_response_observed(
    receipt_id: str,
    session_id: int,
    claim_owner: str,
    dispatch_count: int,
    guest_id: str,
) -> bool:
    """Record only the exact original POST's validated native response.

    The trusted transport calls this after parsing its response, never for an
    error, cancellation or CP observation. It may arrive after turn persistence
    deleted the pending row. The matching cleanup owner uses this timestamp as
    permission to destroy the held guest; observing a response alone never
    clears the binding or fence. Missing retained evidence returns False.
    """
    from agent_sessions import store

    with Session(get_engine()) as db, db.begin():
        _bound_observer_transaction(db)
        _agent = store._lock_session(db, session_id)
        db.execute(
            update(AgentResultReceipt)
            .where(AgentResultReceipt.id == receipt_id)
            .values(created_at=AgentResultReceipt.created_at)
        )
        receipt = _receipt_metadata(db, receipt_id)
        if receipt is None:
            return False
        _check_identity(receipt, session_id, claim_owner, dispatch_count, guest_id)
        if receipt["response_observed_at"] is None:
            db.execute(
                update(AgentResultReceipt)
                .where(AgentResultReceipt.id == receipt_id)
                .values(response_observed_at=_now())
            )
        return True


def held_guest_cleanup_candidates(
    *, receipt_id: str | None = None, now: datetime | None = None
) -> list[dict]:
    """Return exact receipt-held guests whose cleanup deadline has opened.

    A received result owns the guest only after the writer atomically installs
    its receipt fence. The original POST's observed response opens cleanup
    immediately; otherwise ``accept_until`` is the durable backstop. An
    unreceived live invoke can never appear here.
    """
    cleanup_now = now or _now()
    with Session(get_engine()) as db:
        query = (
            select(
                AgentResultReceipt.id,
                AgentResultReceipt.session_id,
                AgentResultReceipt.guest_id,
                AgentResultReceipt.accept_until,
                AgentResultReceipt.response_observed_at,
            )
            .join(
                AgentSession,
                AgentSession.result_receipt_fence_id == AgentResultReceipt.id,
            )
            .where(
                AgentSession.id == AgentResultReceipt.session_id,
                AgentSession.ember_session_id == AgentResultReceipt.guest_id,
                AgentResultReceipt.received_at.isnot(None),
                AgentResultReceipt.superseded_at.is_(None),
                or_(
                    AgentResultReceipt.response_observed_at.isnot(None),
                    AgentResultReceipt.accept_until <= cleanup_now,
                ),
            )
            .order_by(AgentResultReceipt.accept_until, AgentResultReceipt.id)
            .limit(CLEANUP_BATCH)
        )
        if receipt_id is not None:
            query = query.where(AgentResultReceipt.id == receipt_id)
        return [
            {
                "receipt_id": row.id,
                "session_id": row.session_id,
                "guest_id": row.guest_id,
                "accept_until": _aware(row.accept_until),
                "response_observed_at": (
                    None
                    if row.response_observed_at is None
                    else _aware(row.response_observed_at)
                ),
            }
            for row in db.exec(query).all()
        ]


def finish_held_guest_cleanup(
    *,
    receipt_id: str,
    session_id: int,
    guest_id: str,
    authorized_at: datetime,
) -> bool:
    """Clear one matching binding only after its exact guest is confirmed gone."""
    from agent_sessions import store

    with Session(get_engine()) as db, db.begin():
        agent = store._lock_session(db, session_id)
        receipt = _receipt_metadata(db, receipt_id)
        if (
            agent is None
            or receipt is None
            or agent.ember_session_id != guest_id
            or agent.result_receipt_fence_id != receipt_id
            or receipt["session_id"] != session_id
            or receipt["guest_id"] != guest_id
            or receipt["received_at"] is None
            or receipt["superseded_at"] is not None
            or (
                receipt["response_observed_at"] is None
                and _aware(receipt["accept_until"]) > _aware(authorized_at)
            )
            or store.has_unknown_outcome(db, session_id)
            or admission.cleanup_pending(db, agent)
        ):
            return False
        if agent.ember_lineage_id:
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
        return True


def release_abandoned_fence_locked(db: Session, agent, receipt_id: str) -> bool:
    """Release an eligible fence inside a cleanup owner's own transaction.

    The drainer decides whether its cleanup goes ahead while holding the pool
    and session locks, and ``begin_guest_cleanup`` refuses while any row bound
    to the guest still carries a fence. Releasing here rather than in a
    transaction of its own means a refused cleanup rolls the release back with
    everything else it was going to write, and a cleanup that does commit
    commits the release alongside the claim that replaces it as the thing
    keeping dispatch off the guest. A retained receipt is eligible only after
    its original response was observed or its acceptance deadline expired.
    """
    if (
        agent is None
        or not isinstance(receipt_id, str)
        or not receipt_id
        or agent.result_receipt_fence_id != receipt_id
    ):
        return False
    db.execute(
        update(AgentResultReceipt)
        .where(AgentResultReceipt.id == receipt_id)
        .values(created_at=AgentResultReceipt.created_at)
    )
    receipt = _receipt_metadata(db, receipt_id)
    if receipt is not None and (
        receipt["session_id"] != agent.id
        or agent.local_session_id != receipt["local_session_id"]
        or agent.ember_session_id != receipt["guest_id"]
        or receipt["received_at"] is None
        or receipt["superseded_at"] is not None
        or (
            receipt["response_observed_at"] is None
            and _now() < _aware(receipt["accept_until"])
        )
    ):
        return False
    agent.result_receipt_fence_id = None
    db.add(agent)
    # The caller reads the row back through its own locked query, so the
    # cleared fence has to be in the database before that read, not only in
    # this identity map.
    db.flush()
    return True


def _check_credential_format(receipt_id: str, token: str) -> None:
    if (
        not isinstance(receipt_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", receipt_id) is None
        or not isinstance(token, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token) is None
    ):
        raise ReceiptRejected(401, "invalid_receipt_token")


def authenticate_receipt(receipt_id: str, token: str) -> None:
    """Reject unknown credentials before the listener reads a native body."""
    _check_credential_format(receipt_id, token)
    with Session(get_engine()) as db:
        digest = db.exec(
            select(AgentResultReceipt.token_sha256).where(
                AgentResultReceipt.id == receipt_id
            )
        ).one_or_none()
        if digest is None or not hmac.compare_digest(digest, _sha(token.encode())):
            raise ReceiptRejected(401, "invalid_receipt_token")


def capture_result(receipt_id: str, token: str, body: bytes) -> dict:
    """Acknowledge only a committed, immutable body, including late callbacks."""
    _check_credential_format(receipt_id, token)
    if len(body) > MAX_RESULT_BYTES:
        raise ReceiptRejected(413, "result_too_large")
    try:
        # Release the decoded copy before persisting the original bytes.
        is_object = isinstance(json.loads(body), dict)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ReceiptRejected(422, "invalid_result_json") from None
    if not is_object:
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
        # Retries must not fetch a second full native body into this receiver.
        # Keep the existing row lock while checking metadata and exact bytes.
        receipt = db.exec(
            select(
                AgentResultReceipt.token_sha256,
                AgentResultReceipt.retain_until,
                AgentResultReceipt.accept_until,
                AgentResultReceipt.result_sha256,
            ).where(AgentResultReceipt.id == receipt_id)
        ).one_or_none()
        if receipt is None or not hmac.compare_digest(
            receipt.token_sha256, _sha(token.encode())
        ):
            raise ReceiptRejected(401, "invalid_receipt_token")
        now = _now()
        if now >= _aware(receipt.retain_until):
            raise ReceiptRejected(410, "receipt_expired")
        if receipt.result_sha256 is not None:
            if receipt.result_sha256 != digest:
                raise ReceiptRejected(409, "receipt_result_conflict")
            # Return only a boolean, avoiding PostgreSQL's encoded result and
            # the decoded stored body alongside the incoming bytes. A digest
            # match alone is insufficient, including a corrupt or NULL body.
            identical = db.exec(
                select(AgentResultReceipt.result_body == body).where(
                    AgentResultReceipt.id == receipt_id
                )
            ).one()
            if identical is not True:
                raise ReceiptRejected(409, "receipt_result_conflict")
        else:
            if now >= _aware(receipt.accept_until):
                raise ReceiptRejected(410, "receipt_acceptance_expired")
            db.execute(
                update(AgentResultReceipt)
                .where(AgentResultReceipt.id == receipt_id)
                .values(result_body=body, result_sha256=digest, received_at=now)
            )
        return {"receipt_id": receipt_id, "result_sha256": digest}


def prune_expired_receipts() -> int:
    """Bound retained bodies without deleting a durable guest cleanup owner."""
    with Session(get_engine()) as db, db.begin():
        ids = list(
            db.exec(
                select(AgentResultReceipt.id)
                .where(
                    AgentResultReceipt.retain_until <= _now(),
                    ~exists().where(
                        AgentSession.result_receipt_fence_id == AgentResultReceipt.id
                    ),
                )
                .order_by(AgentResultReceipt.retain_until, AgentResultReceipt.id)
                .limit(PRUNE_BATCH)
                # Serialize with the writer's no-op receipt update. If the
                # pruner wins, validation sees no receipt and installs no
                # fence; if the writer wins, this query sees the fence and
                # retains its durable cleanup identity.
                .with_for_update(of=AgentResultReceipt, skip_locked=True)
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
