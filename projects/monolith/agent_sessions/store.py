from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from agent_sessions.models import AgentSession, AgentTurn, PendingMessage


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
