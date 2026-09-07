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
