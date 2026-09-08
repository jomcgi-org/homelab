"""Shared execution permits, independent of task budgets and physical VM fit.

The pool row serializes the original queue claim and its permit in one database
transaction. Expired observers and unknown network outcomes never free permits.
All helpers taking a Session leave commit/rollback to their domain caller.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, or_, update
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from agent_sessions.constants import DRAINER_NODE_KEY, KG_NODE_KEY, UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from core.db import get_engine

TOTAL_LIMIT = 4
BACKGROUND_LIMIT = 3
KG_LIMIT = 2
TIERS = frozenset({"interactive", "project", "kg", "probe"})
PRIORITY = {"interactive": 0, "project": 1, "kg": 2, "probe": 3}


def lock_pool(db: Session) -> None:
    """Acquire before any session/job row lock, including on SQLite."""
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    db.execute(
        insert(AgentCapacityPool.__table__).values(id=1).on_conflict_do_nothing()
    )
    db.execute(update(AgentCapacityPool).where(AgentCapacityPool.id == 1).values(id=1))


def reservation(db: Session, local_session_id: str, pending_seq: int = 1):
    return db.exec(
        select(AgentCapacityReservation).where(
            AgentCapacityReservation.local_session_id == local_session_id,
            AgentCapacityReservation.pending_seq == pending_seq,
        )
    ).first()


def _adopt(
    db: Session, agent: AgentSession, seq: int, state: str, owner=None, model=None
):
    row = reservation(db, agent.local_session_id, seq)
    if row is None:
        initial = reservation(db, agent.local_session_id)
        routine_name = initial.routine_job_name if initial else None
        if (
            routine_name is None
            and agent.workflow_id
            and agent.node_key in {KG_NODE_KEY, DRAINER_NODE_KEY}
        ):
            prefix = f"{agent.workflow_id}:{agent.node_key}:"
            if agent.local_session_id.startswith(prefix):
                routine_name = agent.local_session_id[len(prefix) :] or None
        row = AgentCapacityReservation(
            local_session_id=agent.local_session_id,
            pending_seq=seq,
            session_id=agent.id,
            tier=agent.admission_tier,
            model=model or agent.model,
            owner=owner,
            state=state,
            routine_job_name=routine_name,
        )
        db.add(row)
        db.flush()
    return row


def adopt_existing(db: Session) -> None:
    """Charge legacy attempts even above the cap; adoption cannot authorize work.

    Repeated scans also cover an old replica dispatching during rollout. A
    terminal transport error with a retained guest is unconfirmed execution.
    Clean completed turns and idle resident guests are not logical executions.
    """
    lock_pool(db)
    for agent, pending in db.exec(
        select(AgentSession, PendingMessage).where(
            PendingMessage.session_id == AgentSession.id,
            or_(
                PendingMessage.dispatch_count > 0,
                PendingMessage.claimed_by_replica.isnot(None),
                PendingMessage.last_dispatch_at.isnot(None),
            ),
        )
    ).all():
        _adopt(
            db, agent, pending.seq, "running", pending.claimed_by_replica, pending.model
        )
    later = aliased(AgentTurn)
    for agent, turn in db.exec(
        select(AgentSession, AgentTurn).where(
            AgentTurn.session_id == AgentSession.id,
            or_(
                AgentTurn.stop_reason == UNKNOWN_INVOCATION,
                (AgentTurn.terminal_reason == "error")
                & AgentSession.ember_session_id.isnot(None)
                & ~exists().where(
                    later.session_id == AgentTurn.session_id, later.seq > AgentTurn.seq
                ),
            ),
        )
    ).all():
        _adopt(db, agent, turn.seq, "uncertain")


def _higher_priority_waiting(db: Session, tier: str) -> bool:
    # Only an eligible lane head counts. Failed/held sessions and later queued
    # messages behind an existing attempted head cannot win a new start.
    candidates = db.exec(
        select(AgentSession, PendingMessage)
        .where(
            PendingMessage.session_id == AgentSession.id,
            AgentSession.admission_tier.in_(
                [
                    candidate
                    for candidate in TIERS
                    if PRIORITY[candidate] < PRIORITY[tier]
                ]
            ),
            AgentSession.status.notin_(("failed", "awaiting_login", "recovering")),
            AgentSession.result_receipt_fence_id.is_(None),
        )
        .order_by(PendingMessage.session_id, PendingMessage.seq)
    ).all()
    seen = set()
    for agent, pending in candidates:
        if agent.id in seen:
            continue
        seen.add(agent.id)
        if not pending.dispatch_count and pending.claimed_by_replica is None:
            if agent.local_session_id.startswith("factory:"):
                from swarm.factory_controls import can_start

                fields = agent.local_session_id.split(":")
                if len(fields) != 4 or not fields[3].isdigit():
                    continue
                try:
                    if not can_start(fields[1], session=db)["ok"]:
                        continue
                except (
                    Exception
                ):  # Inventory uncertainty cannot grant a lower-priority start.
                    return True
            if not db.exec(
                select(AgentTurn.id).where(
                    AgentTurn.session_id == agent.id,
                    AgentTurn.stop_reason == UNKNOWN_INVOCATION,
                )
            ).first():
                return True
    return False


def reserve_start(
    db: Session,
    local_session_id: str,
    *,
    tier: str,
    model: str | None,
    pending_seq: int = 1,
    daily_key: str | None = None,
    daily_limit: int | None = None,
    daily_used: int = 0,
    routine_job_name: str | None = None,
) -> bool:
    """Reserve a deterministic start, optionally before its session exists.

    daily_used counts all bound sessions in the caller's rolling window. Only
    unbound reservations are added, so binding does not double-charge allowance.
    """
    if tier not in TIERS or pending_seq < 1:
        raise ValueError("Invalid server admission identity")
    lock_pool(db)
    adopt_existing(db)
    existing = reservation(db, local_session_id, pending_seq)
    if existing is not None:
        return (
            existing.tier == tier
            and existing.model == model
            and existing.routine_job_name == routine_job_name
            and existing.state in {"reserved", "running"}
        )
    active = db.exec(
        select(AgentCapacityReservation).where(
            AgentCapacityReservation.state != "settled"
        )
    ).all()
    if routine_job_name is not None and any(
        row.routine_job_name == routine_job_name for row in active
    ):
        return False
    if len(active) >= TOTAL_LIMIT:
        return False
    if tier != "interactive":
        if sum(
            r.tier != "interactive" for r in active
        ) >= BACKGROUND_LIMIT or _higher_priority_waiting(db, tier):
            return False
    if tier == "kg" and sum(r.tier == "kg" for r in active) >= KG_LIMIT:
        return False
    if daily_key is not None:
        if daily_limit is None or daily_limit < 0 or daily_used < 0:
            raise ValueError(
                "Daily allowance requires an authoritative nonnegative count"
            )
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        unbound = db.exec(
            select(AgentCapacityReservation).where(
                AgentCapacityReservation.daily_key == daily_key,
                AgentCapacityReservation.session_id.is_(None),
                AgentCapacityReservation.state != "settled",
                AgentCapacityReservation.created_at >= cutoff,
            )
        ).all()
        if daily_used + len(unbound) >= daily_limit:
            return False
    db.add(
        AgentCapacityReservation(
            local_session_id=local_session_id,
            pending_seq=pending_seq,
            tier=tier,
            model=model,
            daily_key=daily_key,
            routine_job_name=routine_job_name,
        )
    )
    db.flush()
    return True


def bind_session(db: Session, agent: AgentSession) -> None:
    lock_pool(db)
    row = reservation(db, agent.local_session_id)
    if row is not None:
        if row.state != "reserved" or row.session_id not in (None, agent.id):
            raise ValueError("Start reservation cannot be rebound")
        agent.admission_tier = row.tier
        row.session_id = agent.id
        db.add_all([agent, row])


def claim_pending(
    db: Session, agent: AgentSession, pending: PendingMessage, owner: str
) -> bool:
    if agent.result_receipt_fence_id is not None:
        return False
    existing = reservation(db, agent.local_session_id, pending.seq)
    initial = existing or reservation(db, agent.local_session_id)
    if not reserve_start(
        db,
        agent.local_session_id,
        tier=agent.admission_tier,
        model=pending.model or agent.model,
        pending_seq=pending.seq,
        routine_job_name=initial.routine_job_name if initial else None,
    ):
        return False
    row = reservation(db, agent.local_session_id, pending.seq)
    row.session_id = agent.id
    row.owner = owner
    db.add(row)
    return True


def recheck(
    session_id: int, pending_seq: int, owner: str, workload: str | None = None
) -> bool:
    """Live, non-checkpointed fence immediately before a transport side effect."""
    with Session(get_engine()) as db:
        lock_pool(db)
        row = db.exec(
            select(AgentCapacityReservation).where(
                AgentCapacityReservation.session_id == session_id,
                AgentCapacityReservation.pending_seq == pending_seq,
            )
        ).first()
        pending = db.exec(
            select(PendingMessage).where(
                PendingMessage.session_id == session_id,
                PendingMessage.seq == pending_seq,
            )
        ).first()
        agent = db.get(AgentSession, session_id)
        unknown = db.exec(
            select(AgentTurn.id).where(
                AgentTurn.session_id == session_id,
                AgentTurn.stop_reason == UNKNOWN_INVOCATION,
            )
        ).first()
        if (
            row is None
            or row.state not in {"reserved", "running"}
            or row.owner != owner
            or pending is None
            or pending.claimed_by_replica != owner
            or agent is None
            or agent.status in {"failed", "awaiting_login"}
            or agent.result_receipt_fence_id is not None
            or unknown is not None
            or (workload is not None and row.workload not in (None, workload))
        ):
            return False
        row.state = "running"
        if workload is not None:
            row.workload = workload
        db.add(row)
        db.commit()
        return True


def recheck_prewarm(session_id: int) -> bool:
    """Best-effort wake may reuse an admitted turn, never mint an idle start."""
    with Session(get_engine()) as db:
        row = db.exec(
            select(AgentCapacityReservation).where(
                AgentCapacityReservation.session_id == session_id,
                AgentCapacityReservation.state.in_(("reserved", "running")),
                AgentCapacityReservation.owner.isnot(None),
            )
        ).first()
        if row is None:
            return False
        seq, owner = row.pending_seq, row.owner
    return recheck(session_id, seq, owner)


def settle(
    db: Session,
    agent: AgentSession,
    pending_seq: int,
    *,
    outcome: str,
    cessation_confirmed: bool,
) -> None:
    lock_pool(db)
    row = _adopt(db, agent, pending_seq, "uncertain")
    if row.state == "settled":
        return
    row.state = "settled" if cessation_confirmed else "uncertain"
    row.outcome = outcome
    row.settled_at = datetime.now(timezone.utc) if cessation_confirmed else None
    db.add(row)


def cancel_unbound(db: Session, local_session_id: str, pending_seq: int = 1) -> bool:
    """Cancel only a reservation whose session has provably never been created."""
    lock_pool(db)
    row = reservation(db, local_session_id, pending_seq)
    exists = db.exec(
        select(AgentSession.id).where(AgentSession.local_session_id == local_session_id)
    ).first()
    if (
        row is None
        or row.state != "reserved"
        or row.session_id is not None
        or exists is not None
    ):
        return False
    row.state = "settled"
    row.outcome = "cancelled_before_session"
    row.settled_at = datetime.now(timezone.utc)
    db.add(row)
    return True


def reserved_routine_jobs(db: Session) -> set[str]:
    """An expired routine lease cannot free an unresolved execution identity."""
    return set(
        db.exec(
            select(AgentCapacityReservation.routine_job_name).where(
                AgentCapacityReservation.state != "settled",
                AgentCapacityReservation.routine_job_name.isnot(None),
            )
        ).all()
    )


def confirm_guest_cessation(db: Session, agent: AgentSession) -> None:
    """Require exact terminal guest-cessation evidence from the domain caller.

    A DELETE acknowledgement or a cleared binding does not prove cessation.
    Do not release a claimed turn here: the same executor may be recovering a
    preempted guest and is still authorized to create its replacement.
    """
    lock_pool(db)
    for turn in db.exec(
        select(AgentTurn).where(
            AgentTurn.session_id == agent.id,
            AgentTurn.stop_reason == UNKNOWN_INVOCATION,
        )
    ).all():
        _adopt(db, agent, turn.seq, "uncertain")
    for row in db.exec(
        select(AgentCapacityReservation).where(
            AgentCapacityReservation.session_id == agent.id,
            AgentCapacityReservation.state == "uncertain",
        )
    ).all():
        settle(
            db,
            agent,
            row.pending_seq,
            outcome="guest_cessation_confirmed",
            cessation_confirmed=True,
        )


def cancel_unattempted(
    db: Session, agent: AgentSession, pending: PendingMessage
) -> bool:
    """Release only an exact queued message that could not have dispatched."""
    lock_pool(db)
    if (
        pending.session_id != agent.id
        or pending.dispatch_count
        or pending.claimed_by_replica is not None
        or pending.last_dispatch_at is not None
        or pending.partial_text
        or pending.partial_activities
    ):
        return False
    row = reservation(db, agent.local_session_id, pending.seq)
    if row is None:
        return True
    if row.state != "reserved" or row.owner is not None:
        return False
    settle(
        db,
        agent,
        pending.seq,
        outcome="cancelled_before_dispatch",
        cessation_confirmed=True,
    )
    return True
