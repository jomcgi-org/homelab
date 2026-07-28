"""The goosecracker run/result ledger over ``claude_agent.agent_threads``.

Since the goose cutover (PR C) the monolith runs each goose turn by calling the
fc-invoke daemon and awaiting the result inline, so this table is no longer a
Firecracker placement registry: it is a run ledger keyed by ``session_id``, one
active row per session, carrying the run's state, captured result, and error.

The writer here is the executor (``goosecracker.dispatch`` + ``goosecracker.runner``);
``list_runs`` / ``get_run`` are the read surface the ``monolith-agent-*-agent-thread``
MCP tools poll to fetch a run's state + result. Every function opens its own
session (the executor's delivery half runs off the request loop), so callers on
the event loop must hand these to ``asyncio.to_thread``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import Session

from core.db import get_engine

# Columns this ledger reads/serializes. The legacy placement columns
# (node/arch/base_snapshot_ref/...) remain on the table but are unused here.
_ROW_COLUMNS = (
    "thread_id",
    "state",
    "session_id",
    "recipe",
    "tier",
    "task",
    "discord_thread",
    "result",
    "result_error",
    "created_at",
    "last_active_at",
    "completed_at",
)

_SELECT = (
    "SELECT thread_id, state, session_id, recipe, tier, task, discord_thread, "
    "result, result_error, created_at, last_active_at, completed_at "
    "FROM claude_agent.agent_threads"
)


def _row_to_dict(row: Any) -> dict:
    return {col: getattr(row, col) for col in _ROW_COLUMNS}


def _new_thread_id() -> str:
    return f"t-{uuid4().hex[:12]}"


def upsert_run(
    session_id: str,
    *,
    recipe: str,
    tier: str,
    task: str,
    discord_thread: str,
) -> dict:
    """Create or reset the RUNNING ledger row for ``session_id``.

    One row per session: a first submit inserts (action "create"), a later submit
    for the same session resets the existing row back to RUNNING and clears the
    prior result (action "resume"). A per-session Postgres advisory lock held for
    the transaction serializes concurrent submits for the same session, so the
    select-then-write can never race into two rows.
    """
    with Session(get_engine()) as session:
        # Transaction-scoped advisory lock keyed by the session id, so two
        # concurrent submits for the same session run this block one at a time.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:s))"), {"s": session_id}
        )
        existing = session.execute(
            text(
                "SELECT thread_id FROM claude_agent.agent_threads WHERE session_id = :s"
            ),
            {"s": session_id},
        ).fetchone()

        if existing is not None:
            session.execute(
                text(
                    """
                    UPDATE claude_agent.agent_threads
                       SET state = 'RUNNING',
                           recipe = :recipe,
                           tier = :tier,
                           task = :task,
                           discord_thread = :discord_thread,
                           result = NULL,
                           result_error = NULL,
                           completed_at = NULL,
                           last_active_at = now()
                     WHERE session_id = :s
                    """
                ),
                {
                    "recipe": recipe,
                    "tier": tier,
                    "task": task,
                    "discord_thread": discord_thread or None,
                    "s": session_id,
                },
            )
            session.commit()
            return {"thread_id": existing.thread_id, "action": "resume"}

        thread_id = _new_thread_id()
        session.execute(
            text(
                """
                INSERT INTO claude_agent.agent_threads
                    (thread_id, state, session_id, recipe, tier, task,
                     discord_thread)
                VALUES (:tid, 'RUNNING', :s, :recipe, :tier, :task,
                        :discord_thread)
                """
            ),
            {
                "tid": thread_id,
                "s": session_id,
                "recipe": recipe,
                "tier": tier,
                "task": task,
                "discord_thread": discord_thread or None,
            },
        )
        session.commit()
    return {"thread_id": thread_id, "action": "create"}


def mark_completed(session_id: str, result: str) -> None:
    """Stamp a session's run COMPLETED with its captured result."""
    with Session(get_engine()) as session:
        session.execute(
            text(
                """
                UPDATE claude_agent.agent_threads
                   SET state = 'COMPLETED',
                       result = :result,
                       result_error = NULL,
                       completed_at = now(),
                       last_active_at = now()
                 WHERE session_id = :s
                """
            ),
            {"result": result, "s": session_id},
        )
        session.commit()


def mark_failed(session_id: str, error: str) -> None:
    """Stamp a session's run FAILED with the error detail."""
    with Session(get_engine()) as session:
        session.execute(
            text(
                """
                UPDATE claude_agent.agent_threads
                   SET state = 'FAILED',
                       result_error = :error,
                       completed_at = now(),
                       last_active_at = now()
                 WHERE session_id = :s
                """
            ),
            {"error": error, "s": session_id},
        )
        session.commit()


def list_runs(state: str | None = None) -> list[dict]:
    """Return ledger rows, newest-active first, optionally filtered by state."""
    sql = text(
        _SELECT
        + """
         WHERE (CAST(:state AS text) IS NULL OR state = CAST(:state AS text))
         ORDER BY last_active_at DESC
        """
    )
    with Session(get_engine()) as session:
        rows = session.execute(sql, {"state": state}).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_run(thread_id: str) -> dict | None:
    """Return one ledger row by its thread id, or None."""
    sql = text(_SELECT + " WHERE thread_id = :tid")
    with Session(get_engine()) as session:
        row = session.execute(sql, {"tid": thread_id}).fetchone()
    return _row_to_dict(row) if row else None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def serialize(row: dict) -> dict:
    """Serialize a ledger row for JSON transport (datetimes to ISO 8601)."""
    out = dict(row)
    out["created_at"] = _iso(row.get("created_at"))
    out["last_active_at"] = _iso(row.get("last_active_at"))
    out["completed_at"] = _iso(row.get("completed_at"))
    return out
