from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from sqlalchemy import func, update
from sqlmodel import Session, select

from agent_sessions.models import AgentSession, AgentTurn, PendingMessage
from agent_sessions.transport import Turn
from app.db import get_engine


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


def claim_pending_message_sync(session_id: int, turn_seq: int, replica_id: str) -> bool:
    """Atomically claim one queued message for execution on this replica."""
    with Session(get_engine()) as session:
        result = session.execute(
            update(PendingMessage)
            .where(
                PendingMessage.session_id == session_id,
                PendingMessage.seq == turn_seq,
                PendingMessage.claimed_by_replica.is_(None),
            )
            .values(claimed_by_replica=replica_id)
        )
        session.commit()
    return result.rowcount == 1


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
            .values(claimed_by_replica=None)
        )
        session.commit()


def persist_turn_from_pending_sync(
    session_id: int,
    turn_seq: int,
    prompt: str,
    turn: Turn,
    voice_summary: str,
    status: str,
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
        )
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
    """Persist an error turn while retaining the pending row for recovery."""
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


def get_all_pending_messages_sync() -> list[PendingMessage]:
    """Fetch all pending messages using a fresh synchronous database session."""
    with Session(get_engine()) as session:
        return list(session.exec(select(PendingMessage)).all())
