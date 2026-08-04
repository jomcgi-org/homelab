from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, func, select

from agent_sessions import model_family, store
from agent_sessions.models import AgentSession, AgentTurn, PendingMessage
from agent_sessions.mcp import (
    _clear_ember_bindings_for,
    _load_session_row,
    _persist_pending_message,
    _persist_session,
    _schedule_next_message,
    _set_session_status,
    _transport,
)
from core.db import get_session
from faas.embervm_client import EmberVMTransportError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agents", tags=["agents"])


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _as_utc(value)
    return value.isoformat() if value is not None else None


def _decode(value: str | None, default):
    if not value:
        return default
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    return decoded


def _aggregate_statement(status: str | None = None):
    turns = (
        select(
            AgentTurn.session_id,
            func.count(AgentTurn.id).label("turn_count"),
            func.coalesce(func.sum(AgentTurn.cost_usd), 0).label("total_cost_usd"),
        )
        .group_by(AgentTurn.session_id)
        .subquery()
    )
    pending = (
        select(
            PendingMessage.session_id,
            func.count(PendingMessage.id).label("pending_count"),
        )
        .group_by(PendingMessage.session_id)
        .subquery()
    )
    statement = (
        select(
            AgentSession,
            func.coalesce(turns.c.turn_count, 0),
            func.coalesce(turns.c.total_cost_usd, 0),
            func.coalesce(pending.c.pending_count, 0),
        )
        .outerjoin(turns, turns.c.session_id == AgentSession.id)
        .outerjoin(pending, pending.c.session_id == AgentSession.id)
        .order_by(AgentSession.last_turn_at.desc())
    )
    if status is not None:
        statement = statement.where(AgentSession.status == status)
    return statement


def _session_payload(
    row: AgentSession, turn_count: int, total_cost_usd: float, pending_count: int
) -> dict:
    return {
        "id": row.id,
        "local_session_id": row.local_session_id,
        "workspace": row.workspace,
        "branch": row.branch,
        "model": row.model,
        "status": row.status,
        "created_at": _iso(row.created_at),
        "last_turn_at": _iso(row.last_turn_at),
        "voice_summary": row.voice_summary,
        "turn_count": int(turn_count),
        "total_cost_usd": float(total_cost_usd or 0),
        "pending_count": int(pending_count),
    }


def _rows(session: Session, status: str | None = None, limit: int | None = None):
    statement = _aggregate_statement(status)
    if limit is not None:
        statement = statement.limit(limit)
    results = session.exec(statement).all()
    return [
        _session_payload(result[0], result[1], result[2], result[3])
        for result in results
    ]


class StartRequest(BaseModel):
    prompt: str
    model: str | None = None
    workspace: str = "<guest>"
    branch: str = "main"


class MessageRequest(BaseModel):
    prompt: str
    model: str | None = None


@router.get("/sessions")
def list_sessions(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    return _rows(session, status, limit)


@router.get("/sessions/{session_id}")
def get_session_detail(
    session_id: int,
    after_seq: int | None = Query(default=None, ge=0),
    session: Session = Depends(get_session),
) -> dict:
    result = session.exec(
        _aggregate_statement().where(AgentSession.id == session_id)
    ).first()
    if result is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    row, turn_count, total_cost_usd, pending_count = result
    turns_statement = select(AgentTurn).where(AgentTurn.session_id == session_id)
    if after_seq is not None:
        turns_statement = turns_statement.where(AgentTurn.seq > after_seq)
    turns = session.exec(turns_statement.order_by(AgentTurn.seq)).all()
    pending = session.exec(
        select(PendingMessage)
        .where(PendingMessage.session_id == session_id)
        .order_by(PendingMessage.seq)
    ).all()
    return {
        "session": _session_payload(row, turn_count, total_cost_usd, pending_count),
        "turns": [
            {
                "seq": turn.seq,
                "prompt": turn.prompt,
                "model": turn.model,
                "result_text": turn.result_text,
                "voice_summary": turn.voice_summary,
                "terminal_reason": turn.terminal_reason,
                "stop_reason": turn.stop_reason,
                "permission_denials": _decode(turn.permission_denials, []),
                "commit_sha": turn.commit_sha,
                "usage": _decode(turn.usage_json, {}),
                "cost_usd": turn.cost_usd,
                "created_at": _iso(turn.created_at),
            }
            for turn in turns
        ],
        "pending_queue": [
            {
                "seq": message.seq,
                "prompt": message.message_text,
                "claimed_by_replica": message.claimed_by_replica,
                "claimed_at": _iso(message.claimed_at),
                "created_at": _iso(message.created_at),
            }
            for message in pending
        ],
    }


@router.post("/sessions")
async def start_session(request: StartRequest) -> dict:
    try:
        model_family(request.model)
    except ValueError as exc:
        return {"accepted": False, "error": str(exc)}
    row = await asyncio.to_thread(
        _persist_session,
        str(uuid4()),
        request.workspace,
        request.branch,
        request.model,
    )
    turn = await asyncio.to_thread(
        _persist_pending_message, row.id, request.prompt, request.model
    )
    _schedule_next_message(row.id)
    return {"accepted": True, "session_id": row.id, "turn": turn}


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: int, request: MessageRequest) -> dict:
    row = await asyncio.to_thread(_load_session_row, session_id)
    if row is None:
        return {"accepted": False, "error": f"Unknown agent session {session_id}"}
    try:
        session_family = model_family(row.model)
        requested_family = (
            model_family(request.model) if request.model is not None else session_family
        )
    except ValueError as exc:
        return {"accepted": False, "error": str(exc)}
    if requested_family != session_family:
        return {
            "accepted": False,
            "error": (
                f"Model family mismatch: session family is {session_family}, "
                f"requested model family is {requested_family}"
            ),
        }
    effective_model = request.model or row.model
    turn = await asyncio.to_thread(
        _persist_pending_message, session_id, request.prompt, effective_model
    )
    await asyncio.to_thread(_set_session_status, session_id, "running")
    _schedule_next_message(session_id)
    return {"accepted": True, "session_id": session_id, "turn": turn}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int) -> dict:
    try:
        row = await asyncio.to_thread(_load_session_row, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent session not found")
        if row.ember_session_id is None:
            return {}
        result = await _transport.destroy_session(row.ember_session_id)
        result["cleared_bindings"] = await asyncio.to_thread(
            _clear_ember_bindings_for, row.ember_session_id
        )
        return result
    except EmberVMTransportError as exc:
        return {"error": str(exc)}


@router.get("/search")
def search_sessions(
    q: str = Query(...),
    limit: int = Query(default=20, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict:
    results = store.lexical_search(session, q, limit)
    return {
        "results": [
            {
                "session_id": result["session_id"],
                "local_session_id": result["local_session_id"],
                "workspace": result["workspace"],
                "seq": result["seq"],
                "created_at": _iso(result["created_at"]),
                "rank": float(result["rank"]),
                "snippet": result["snippet"],
            }
            for result in results
        ]
    }
