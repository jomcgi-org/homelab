"""Session-owned storage operations for explicit routine reconciliation.

The routine owner verifies exact fresh cessation evidence and holds the pool,
session and job locks. These operations never commit or infer cessation.
"""

from sqlmodel import Session, select

from agent_sessions import admission
from agent_sessions.models import AgentCapacityReservation, AgentSession


def _locked_session(db: Session, session_id: int) -> AgentSession:
    return db.exec(
        select(AgentSession)
        .where(AgentSession.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def lock_cessation_session(db: Session, session_id: int) -> dict:
    """After the caller's pool lock, lock and project only the required identity."""
    agent = _locked_session(db, session_id)
    return {
        "id": agent.id,
        "local_session_id": agent.local_session_id,
        "workflow_id": agent.workflow_id,
    }


def confirm_reconciled_guest_cessation(
    db: Session, session_id: int, routine_job_name: str
) -> None:
    """Settle and unbind the caller's verified ceased attempt in its transaction.

    The routine owner must already hold pool/session/job ownership and validate
    its fresh authoritative cessation proof. Claimed or newer execution remains
    fenced. The caller owns the audit and commits all changes together.
    """
    agent = _locked_session(db, session_id)
    admission.adopt_existing(db)
    if db.exec(
        select(AgentCapacityReservation).where(
            AgentCapacityReservation.routine_job_name == routine_job_name,
            AgentCapacityReservation.state != "settled",
            (
                (AgentCapacityReservation.session_id != agent.id)
                | AgentCapacityReservation.session_id.is_(None)
                | AgentCapacityReservation.state.in_(("reserved", "running"))
            ),
        )
    ).first():
        raise ValueError("A newer or still admitted execution owns this job")
    admission.confirm_guest_cessation(db, agent)
    if db.exec(
        select(AgentCapacityReservation).where(
            AgentCapacityReservation.session_id == agent.id,
            AgentCapacityReservation.state != "settled",
        )
    ).first():
        raise ValueError("Attempt still owns an execution permit")
    if agent.ember_lineage_id:
        agent.prior_ember_lineage_id = agent.ember_lineage_id
    if agent.cli_session_id:
        agent.prior_cli_session_id = agent.cli_session_id
    agent.ember_session_id = None
    agent.ember_session_token = None
    agent.ember_session_expires_at = None
    agent.ember_lineage_id = None
    agent.cli_session_id = None
    db.add(agent)


def _factory_owner(db: Session, pin: dict, session_id: int | None):
    from agent_sessions import normalize_model

    key = f"factory:{pin['task_id']}:{pin['node_key']}:{pin['attempt']}"
    owner = db.exec(
        select(AgentSession)
        .where(AgentSession.local_session_id == key)
        .execution_options(populate_existing=True)
    ).first()
    if owner is None:
        return None
    expected = {
        "workflow_id": pin["workflow_id"],
        "node_key": pin["node_key"],
        "node_attempt": pin["attempt"],
        "repo": pin["repo"],
        "branch": pin.get("hydration_branch", pin["branch"]),
        "model": normalize_model(pin["model"]),
        "admission_tier": "project",
    }
    if (session_id is not None and owner.id != session_id) or any(
        getattr(owner, field) != value for field, value in expected.items()
    ):
        raise ValueError("factory session ownership conflict")
    return owner


def read_factory_dispatch(db: Session, pin: dict, session_id: int) -> dict:
    """Project persisted claim evidence without treating a missing row as queued."""
    from agent_sessions.models import PendingMessage

    owner = _factory_owner(db, pin, session_id)
    pending = (
        db.exec(
            select(PendingMessage).where(PendingMessage.session_id == session_id)
        ).all()
        if owner is not None
        else []
    )
    unknown = {"state": "unconfirmed", "started_at": None}
    if len(pending) != 1 or pending[0].seq != 1:
        return unknown
    row = pending[0]
    if row.last_dispatch_at is not None and row.dispatch_count == 1:
        return {"state": "dispatched", "started_at": row.last_dispatch_at.isoformat()}
    if (
        row.dispatch_count
        or row.last_dispatch_at is not None
        or row.claimed_by_replica is not None
        or row.claimed_at is not None
        or row.partial_text
        or row.partial_activities
    ):
        return unknown
    return {"state": "queued", "started_at": None}


def cancel_queued_factory_attempt(db: Session, pin: dict, session_id: int | None):
    """Terminate an exact never-dispatched first turn, without committing.

    The factory caller holds its control lock and composes graph/start outcomes
    in this transaction. The pool lock serializes against dispatch before any
    observations. Null guest bindings alone are never proof of no invocation.
    """
    import json
    from datetime import datetime, timezone
    from sqlalchemy import or_
    from agent_sessions.models import AgentTurn, PendingMessage

    admission.lock_pool(db)
    owner = _factory_owner(db, pin, session_id)
    if owner is None:
        return None
    owner = _locked_session(db, owner.id)
    pending = db.exec(
        select(PendingMessage)
        .where(PendingMessage.session_id == owner.id)
        .execution_options(populate_existing=True)
    ).all()
    if len(pending) != 1 or pending[0].seq != 1:
        return None
    row = pending[0]
    if (
        db.exec(select(AgentTurn.id).where(AgentTurn.session_id == owner.id)).first()
        is not None
    ):
        return None
    if any(
        (
            owner.ember_session_id,
            owner.ember_session_token,
            owner.ember_lineage_id,
            owner.cli_session_id,
            owner.prior_ember_lineage_id,
            owner.prior_cli_session_id,
            owner.ember_session_expires_at,
            owner.recovery_workspace_loss,
        )
    ):
        return None
    if owner.status not in {"running", "warn"}:
        return None
    if (
        row.dispatch_count != 0
        or row.claimed_by_replica is not None
        or row.claimed_at is not None
        or row.last_dispatch_at is not None
        or row.partial_text
        or row.partial_activities
    ):
        return None

    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    # Initial creation precedes enqueue. A later last_turn_at is valid only
    # when the existing one-shot recovery wrote its matching completion marker;
    # that path never resets dispatch_count and refuses attempted messages.
    if aware(owner.last_turn_at) > aware(row.created_at) and (
        owner.recovery_completed_at is None
        or aware(owner.last_turn_at) != aware(owner.recovery_completed_at)
    ):
        return None
    permits = db.exec(
        select(AgentCapacityReservation)
        .where(
            or_(
                AgentCapacityReservation.local_session_id == owner.local_session_id,
                AgentCapacityReservation.session_id == owner.id,
            )
        )
        .execution_options(populate_existing=True)
    ).all()
    if len(permits) > 1 or any(
        permit.local_session_id != owner.local_session_id
        or permit.session_id not in (None, owner.id)
        or permit.pending_seq != 1
        or permit.state != "reserved"
        or permit.owner is not None
        or permit.workload is not None
        or permit.outcome is not None
        or permit.settled_at is not None
        or permit.tier != "project"
        for permit in permits
    ):
        return None
    if not admission.cancel_unattempted(db, owner, row):
        return None
    db.add(
        AgentTurn(
            session_id=owner.id,
            seq=1,
            prompt=row.message_text,
            model=None,
            cost_usd=None,
            terminal_reason="error",
            stop_reason="cancelled_before_dispatch",
            result_text="Factory reconciliation cancelled this queued attempt after its workflow timed out. No agent invocation was dispatched.",
            usage_json=json.dumps(
                {
                    "source": "factory_reconciliation",
                    "reason": "cancelled_before_dispatch",
                }
            ),
        )
    )
    owner.status = "warn"
    owner.last_turn_at = datetime.now(timezone.utc)
    db.add(owner)
    db.delete(row)
    db.flush()
    return owner.id
