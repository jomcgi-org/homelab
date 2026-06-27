"""Operations over ``claude_agent.agent_threads`` - the Firecracker agent-thread
registry and catalog (ADR 022).

These functions back the ``monolith-agent-*-agent-thread`` MCP tools (the
catalog: list, get, resume). The controller (fc-agentd, a node-4 daemon) is the
writer of the lifecycle ``state`` and the snapshot refs; the catalog here is the
human/agent-facing read surface plus the one write a caller makes, requesting a
resume by stamping ``wake_requested_at`` on an IDLE thread for the reconcile loop
to pick up.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from app.db import get_engine

_ROW_COLUMNS = (
    "thread_id",
    "state",
    "repo",
    "branch",
    "node",
    "arch",
    "base_snapshot_ref",
    "thread_snapshot_ref",
    "size_bytes",
    "discord_thread",
    "created_at",
    "last_active_at",
    "ttl_secs",
    "wake_requested_at",
)

_SELECT = (
    "SELECT thread_id, state, repo, branch, node, arch, base_snapshot_ref, "
    "thread_snapshot_ref, size_bytes, discord_thread, created_at, "
    "last_active_at, ttl_secs, wake_requested_at FROM claude_agent.agent_threads"
)


def _row_to_dict(row: Any) -> dict:
    return {col: getattr(row, col) for col in _ROW_COLUMNS}


def list_threads(state: str | None = None, node: str | None = None) -> list[dict]:
    """Return agent_threads rows, newest-active first, optionally filtered."""
    sql = text(
        _SELECT
        + """
         WHERE (CAST(:state AS text) IS NULL OR state = CAST(:state AS text))
           AND (CAST(:node AS text) IS NULL OR node = CAST(:node AS text))
         ORDER BY last_active_at DESC
        """
    )
    with Session(get_engine()) as session:
        rows = session.execute(sql, {"state": state, "node": node}).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_thread(thread_id: str) -> dict | None:
    """Return a single thread by id, or None if absent."""
    sql = text(_SELECT + " WHERE thread_id = :thread_id")
    with Session(get_engine()) as session:
        row = session.execute(sql, {"thread_id": thread_id}).fetchone()
    return _row_to_dict(row) if row else None


def request_resume(thread_id: str) -> dict:
    """Ask the controller to restore an IDLE thread.

    Stamps ``wake_requested_at`` and bumps ``last_active_at`` (so the GC does not
    reclaim a thread that was just asked to resume). Only IDLE threads are
    resumable; returns ``ok=False`` with a reason otherwise.
    """
    sql = text(
        """
        UPDATE claude_agent.agent_threads
           SET wake_requested_at = now(), last_active_at = now()
         WHERE thread_id = :thread_id AND state = 'IDLE'
        RETURNING thread_id, state
        """
    )
    with Session(get_engine()) as session:
        row = session.execute(sql, {"thread_id": thread_id}).fetchone()
        session.commit()
    if row is not None:
        return {"ok": True, "thread_id": thread_id, "state": "IDLE", "wake_requested": True}

    current = get_thread(thread_id)
    if current is None:
        return {"ok": False, "thread_id": thread_id, "reason": "thread not found"}
    return {
        "ok": False,
        "thread_id": thread_id,
        "state": current["state"],
        "reason": f"thread is {current['state']}, only IDLE threads can be resumed",
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def serialize(row: dict) -> dict:
    """Serialize a thread row for JSON transport (datetimes to ISO 8601)."""
    out = dict(row)
    out["created_at"] = _iso(row.get("created_at"))
    out["last_active_at"] = _iso(row.get("last_active_at"))
    out["wake_requested_at"] = _iso(row.get("wake_requested_at"))
    return out
