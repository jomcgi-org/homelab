"""Database projections used by the swarm run views."""

from __future__ import annotations

import json
import re
from typing import Any

from sqlmodel import Session, func, select

from agent_sessions.models import AgentSession, AgentTurn, PendingMessage


def _decode(value: str | None, default: Any):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _compact_input(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    text = re.sub(r"\s+", " ", text)
    return text[:110] + "…" if len(text) > 110 else text


def _activity_line(activity: Any) -> str:
    if isinstance(activity, str):
        return activity
    if not isinstance(activity, dict):
        return "step"
    kind = str(
        activity.get("type") or activity.get("tool") or activity.get("name") or ""
    ).lower()
    if kind in ("edit", "write"):
        detail = activity.get("file_path") or activity.get("path") or ""
        return f"{kind} {detail}".rstrip()
    if kind in ("bash", "shell"):
        detail = activity.get("command") or _compact_input(activity.get("input"))
        return f"run {detail}".rstrip()
    if activity.get("name"):
        return f"{activity['name']} {_compact_input(activity.get('input'))}".rstrip()
    return f"{kind or 'step'} {_compact_input(activity.get('input'))}".rstrip()


def swarm_session_views(
    session: Session, workflow_id: str | None = None
) -> dict[str, list[dict]]:
    """Return the enriched, plain-dict session rows consumed by swarm views."""
    totals = (
        select(
            AgentTurn.session_id,
            func.coalesce(func.sum(AgentTurn.cost_usd), 0).label("total_cost_usd"),
        )
        .group_by(AgentTurn.session_id)
        .subquery()
    )
    statement = (
        select(AgentSession, func.coalesce(totals.c.total_cost_usd, 0))
        .outerjoin(totals, totals.c.session_id == AgentSession.id)
        .where(AgentSession.workflow_id.is_not(None))
    )
    if workflow_id is not None:
        statement = statement.where(AgentSession.workflow_id == workflow_id)
    rows = session.exec(statement).all()
    session_ids = [row[0].id for row in rows if row[0].id is not None]

    pending_by_session: dict[int, list[PendingMessage]] = {}
    if session_ids:
        pending_rows = session.exec(
            select(PendingMessage)
            .where(PendingMessage.session_id.in_(session_ids))
            .order_by(PendingMessage.session_id, PendingMessage.seq)
        ).all()
        for pending in pending_rows:
            pending_by_session.setdefault(pending.session_id, []).append(pending)

    result: dict[str, list[dict]] = {}
    for row, total_cost in rows:
        pending = pending_by_session.get(row.id, [])
        claimed = [item for item in pending if item.claimed_by_replica is not None]
        current = (
            max(
                claimed,
                key=lambda item: (item.claimed_at or item.created_at, item.seq),
            )
            if claimed
            else (pending[0] if pending else None)
        )
        activities = _decode(current.partial_activities, []) if current else []
        activity = (
            activities[-1] if isinstance(activities, list) and activities else None
        )
        observed_at = (current.claimed_at or current.created_at) if current else None
        view = {
            "id": row.id,
            "local_session_id": row.local_session_id,
            "workflow_id": row.workflow_id,
            "node_key": row.node_key,
            "node_attempt": row.node_attempt,
            "status": row.status,
            "model": row.model,
            "total_cost_usd": float(total_cost or 0),
            "created_at": row.created_at,
            "last_turn_at": row.last_turn_at,
            "activity": _activity_line(activity) if activity is not None else None,
            "activity_observed_at": observed_at,
        }
        result.setdefault(row.workflow_id, []).append(view)
    return result
