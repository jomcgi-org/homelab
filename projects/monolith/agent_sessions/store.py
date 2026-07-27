from __future__ import annotations

import json
import subprocess
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update
from sqlmodel import Session, select

from agent_sessions.models import AgentSession, AgentTurn, PendingMessage
from agent_sessions.transport import Turn
from app.db import get_engine

logger = logging.getLogger(__name__)


def _commit_sha(workspace: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", workspace, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def create_session(
    session: Session, local_session_id: str, workspace: str, branch: str
) -> AgentSession:
    row = AgentSession(
        local_session_id=local_session_id, workspace=workspace, branch=branch
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_session(session: Session, session_id: int) -> AgentSession | None:
    return session.get(AgentSession, session_id)


def get_session_by_local_id(session: Session, local_id: str) -> AgentSession | None:
    return session.exec(
        select(AgentSession).where(AgentSession.local_session_id == local_id)
    ).first()


def create_turn(
    session: Session,
    session_id: int,
    seq: int,
    prompt: str,
    voice_summary: str | None,
    result_text: str,
    terminal_reason: str | None,
    stop_reason: str | None,
    permission_denials: list | None,
    commit_sha: str | None,
    usage: dict | None,
    cost_usd: float | None,
    cli_session_id: str | None = None,
) -> AgentTurn:
    row = AgentTurn(
        session_id=session_id,
        seq=seq,
        prompt=prompt,
        voice_summary=voice_summary,
        result_text=result_text,
        terminal_reason=terminal_reason,
        stop_reason=stop_reason,
        permission_denials=json.dumps(permission_denials or []),
        commit_sha=commit_sha,
        usage_json=json.dumps(usage or {}),
        cost_usd=cost_usd,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def update_session_status(
    session: Session, session_id: int, status: str, voice_summary: str | None = None
) -> AgentSession:
    row = session.get(AgentSession, session_id)
    if row is None:
        raise ValueError(f"Unknown agent session {session_id}")
    row.status = status
    row.last_turn_at = datetime.now(timezone.utc)
    if voice_summary is not None:
        row.voice_summary = voice_summary
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_turns(session: Session, session_id: int) -> list[AgentTurn]:
    return list(
        session.exec(
            select(AgentTurn)
            .where(AgentTurn.session_id == session_id)
            .order_by(AgentTurn.seq)
        ).all()
    )


def get_turn(session: Session, session_id: int, turn_seq: int) -> AgentTurn | None:
    return session.exec(
        select(AgentTurn).where(
            AgentTurn.session_id == session_id, AgentTurn.seq == turn_seq
        )
    ).first()


def create_pending_message(
    session: Session, session_id: int, message_text: str
) -> PendingMessage:
    """Enqueue a message durably and return its per-session turn sequence."""
    if session.get(AgentSession, session_id) is None:
        raise ValueError(f"Unknown agent session {session_id}")

    last_turn = session.exec(
        select(func.max(AgentTurn.seq)).where(AgentTurn.session_id == session_id)
    ).one()
    last_pending = session.exec(
        select(func.max(PendingMessage.seq)).where(
            PendingMessage.session_id == session_id
        )
    ).one()
    seq = max(last_turn or 0, last_pending or 0) + 1
    row = PendingMessage(
        session_id=session_id,
        seq=seq,
        message_text=message_text,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_pending_message(
    session: Session, session_id: int, turn_seq: int
) -> PendingMessage | None:
    return session.exec(
        select(PendingMessage).where(
            PendingMessage.session_id == session_id,
            PendingMessage.seq == turn_seq,
        )
    ).first()


def get_pending_message_sync(session_id: int, turn_seq: int) -> PendingMessage | None:
    """Fetch one pending message using a fresh synchronous database session."""
    with Session(get_engine()) as session:
        return get_pending_message(session, session_id, turn_seq)


def claim_pending_message_for_session_sync(
    session_id: int, replica_id: str
) -> int | None:
    """Atomically claim the lowest unclaimed seq for a session.

    Enforces FIFO ordering across replicas by always claiming the lowest
    unclaimed seq. This is a single atomic operation, so ordering is
    guaranteed at the database level and holds across all replicas.

    Returns the seq of the claimed message, or None if no unclaimed messages.
    """
    with Session(get_engine()) as session:
        # The lowest OUTSTANDING seq, claimed or not. Pending rows are deleted
        # once their turn completes, so anything still here is unfinished.
        #
        # Taking the minimum over unclaimed rows only would order assignment but
        # not execution: with seq 1 claimed and running, seq 2 would be the
        # lowest unclaimed and a second executor would claim it and run
        # concurrently. Serialising within a session means refusing to start a
        # later message while an earlier one is still outstanding.
        lowest_seq_result = session.execute(
            select(func.min(PendingMessage.seq)).where(
                PendingMessage.session_id == session_id,
            )
        ).scalar()

        if lowest_seq_result is None:
            return None

        # Claim it atomically
        result = session.execute(
            update(PendingMessage)
            .where(
                PendingMessage.session_id == session_id,
                PendingMessage.seq == lowest_seq_result,
                PendingMessage.claimed_by_replica.is_(None),
            )
            .values(claimed_by_replica=replica_id, claimed_at=func.now())
        )
        session.commit()

        # Return the seq if we successfully claimed it, None otherwise
        return lowest_seq_result if result.rowcount == 1 else None


def release_pending_message_claim_sync(
    session_id: int, turn_seq: int, replica_id: str
) -> None:
    """Release this replica's claim after execution completes."""
    with Session(get_engine()) as session:
        session.execute(
            update(PendingMessage)
            .where(
                PendingMessage.session_id == session_id,
                PendingMessage.seq == turn_seq,
                PendingMessage.claimed_by_replica == replica_id,
            )
            .values(claimed_by_replica=None, claimed_at=None)
        )
        session.commit()


def persist_turn_from_pending_sync(
    session_id: int,
    turn_seq: int,
    prompt: str,
    turn: Turn,
    voice_summary: str,
    status: str,
    cli_session_id: str | None = None,
) -> AgentTurn:
    """Persist the result of a queued message using a fresh database session."""
    with Session(get_engine()) as session:
        sess_row = get_session(session, session_id)
        if not sess_row:
            raise ValueError(f"Session {session_id} not found")
        usage = {**turn.usage, "activities": turn.activities}
        row = create_turn(
            session,
            session_id,
            turn_seq,
            prompt,
            voice_summary,
            turn.result,
            turn.terminal_reason,
            turn.stop_reason,
            turn.permission_denials,
            _commit_sha(sess_row.workspace),
            usage,
            turn.total_cost_usd,
            cli_session_id,
        )
        # Store CLI session_id if this is the first turn
        if cli_session_id and not sess_row.cli_session_id:
            sess_row.cli_session_id = cli_session_id
            session.add(sess_row)
            session.commit()
        update_session_status(session, session_id, status, voice_summary)
        return row


def delete_pending_message_sync(session_id: int, turn_seq: int) -> None:
    """Delete a pending message after its turn has been persisted."""
    with Session(get_engine()) as session:
        row = get_pending_message(session, session_id, turn_seq)
        if row:
            session.delete(row)
            session.commit()


def mark_turn_error_sync(session_id: int, turn_seq: int, error_msg: str) -> None:
    """Persist an error turn and delete the pending row to stop infinite retries.

    Semantics: A failed turn is terminal - the error is recorded durably in
    agent_turns as a failed attempt, and the pending row is deleted to prevent
    the sweep from re-dispatching it every 5 seconds. The caller can query
    session status to see the failure.
    """
    with Session(get_engine()) as session:
        row = get_pending_message(session, session_id, turn_seq)
        if row is None or get_turn(session, session_id, turn_seq) is not None:
            return
        create_turn(
            session,
            session_id,
            turn_seq,
            row.message_text,
            "Error: " + error_msg[:100],
            f"Error executing turn: {error_msg}",
            terminal_reason="error",
            stop_reason=None,
            permission_denials=[],
            commit_sha=None,
            usage={},
            cost_usd=0.0,
        )
        # Delete the pending row to stop infinite retries
        session.delete(row)
        session.commit()


def reclaim_stale_claims_sync(lease_interval_seconds: int = 30) -> int:
    """Reclaim pending messages whose claims have expired due to replica crash.

    A claim is considered stale if claimed_by_replica is not null and the
    claimed_at timestamp is older than lease_interval_seconds. A healthy
    replica refreshes claimed_at every 10 seconds during turn execution, so
    a claim that is not refreshed will be reclaimed within one lease interval.

    The lease interval is set as a multiple of the refresh interval
    (default 30s = 3x10s refresh), trading slow recovery for correctness:
    a crashed replica's claims are reclaimed within one lease interval, but
    an actively executing turn that refreshes its claim will never be
    double-executed (even if the turn takes many minutes).

    The comparison uses Python's datetime.now() as a cutoff. Since claimed_at
    is always set via the database (func.now()), the comparison is stable
    across clock skew and timezone differences.

    Returns the count of reclaimed messages.
    """
    with Session(get_engine()) as session:
        # Compute cutoff in Python, not as a SQL expression. SQLAlchemy cannot
        # reliably render timedelta subtraction across SQLite and Postgres dialects.
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=lease_interval_seconds)
        result = session.execute(
            update(PendingMessage)
            .where(
                PendingMessage.claimed_by_replica.isnot(None),
                PendingMessage.claimed_at.isnot(None),
                PendingMessage.claimed_at < cutoff,
            )
            .values(claimed_by_replica=None, claimed_at=None)
        )
        session.commit()
    return result.rowcount


def refresh_claim_sync(session_id: int, turn_seq: int, replica_id: str) -> bool:
    """Refresh the claim on a pending message to prevent expiry during execution.

    Called periodically during turn execution to keep the claim fresh. If the
    claim has already been reclaimed by another replica (claimed_by_replica no
    longer matches replica_id), returns False so the executor knows to abort.

    Returns True if claim is still held, False if claim was stolen.
    """
    with Session(get_engine()) as session:
        row = get_pending_message(session, session_id, turn_seq)
        if not row:
            return False
        if row.claimed_by_replica != replica_id:
            return False
        # Claim is still ours; refresh the timestamp using SQL so no Python
        # datetime crosses the boundary, avoiding SQLite/Postgres tz handling issues
        session.execute(
            update(PendingMessage)
            .where(
                PendingMessage.session_id == session_id,
                PendingMessage.seq == turn_seq,
            )
            .values(claimed_at=func.now())
        )
        session.commit()
        return True


def get_all_pending_messages_sync() -> list[PendingMessage]:
    """Fetch all pending messages using a fresh synchronous database session."""
    with Session(get_engine()) as session:
        return list(session.exec(select(PendingMessage)).all())
