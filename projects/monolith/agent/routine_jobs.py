"""Operations over ``claude_agent.routine_jobs`` — delegated work for cloud Routines.

These functions back the ``monolith-agent-*-routine-job`` MCP tools. Unlike
``scheduler.api``'s ``scheduled_jobs`` (which is polled by the in-cluster
tick loop), ``routine_jobs`` rows are only ever read or written by the MCP
surface; cloud Routines claim, run, and complete them.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from core.db import get_engine


_ROW_COLUMNS = (
    "name",
    "routine_kind",
    "interval_secs",
    "next_run_at",
    "last_run_at",
    "last_status",
    "last_summary",
    "locked_by",
    "locked_at",
    "ttl_secs",
    "payload",
    "created_by",
    "created_at",
)


def _row_to_dict(row: Any) -> dict:
    return {col: getattr(row, col) for col in _ROW_COLUMNS}


def list_jobs(due_only: bool = False, kind: str | None = None) -> list[dict]:
    """Return routine_jobs rows, optionally filtered to due-only and/or by kind."""
    sql = text(
        """
        SELECT name, routine_kind, interval_secs, next_run_at, last_run_at,
               last_status, last_summary, locked_by, locked_at, ttl_secs,
               payload, created_by, created_at
          FROM claude_agent.routine_jobs
         WHERE (:due_only IS FALSE
                OR (next_run_at IS NOT NULL
                    AND next_run_at <= now()
                    AND (locked_by IS NULL
                         OR locked_at + (ttl_secs || ' seconds')::interval < now())))
           AND (CAST(:kind AS text) IS NULL OR routine_kind = CAST(:kind AS text))
         ORDER BY next_run_at NULLS LAST, name
        """
    )
    with Session(get_engine()) as session:
        rows = session.execute(sql, {"due_only": due_only, "kind": kind}).fetchall()
    return [_row_to_dict(r) for r in rows]


def claim_job(
    holder: str,
    ttl_secs: int,
    kind: str | None = None,
    name: str | None = None,
) -> dict | None:
    """Claim a routine_jobs row.

    If ``name`` is set, attempt to claim that specific row; returns None if it's
    held with a still-live lock. If ``name`` is None, claim the next due
    unclaimed row, optionally filtered by ``kind``. Uses ``SELECT FOR UPDATE
    SKIP LOCKED`` so concurrent claimers never block each other.
    """
    if name is not None:
        select_sql = text(
            """
            SELECT name, locked_by, locked_at, ttl_secs
              FROM claude_agent.routine_jobs
             WHERE name = :name
             FOR UPDATE
            """
        )
    else:
        select_sql = text(
            """
            SELECT name, locked_by, locked_at, ttl_secs
              FROM claude_agent.routine_jobs
             WHERE next_run_at IS NOT NULL
               AND next_run_at <= now()
               AND (
                    locked_by IS NULL
                    OR locked_at + (ttl_secs || ' seconds')::interval < now()
               )
               AND (CAST(:kind AS text) IS NULL OR routine_kind = CAST(:kind AS text))
             ORDER BY next_run_at ASC
             LIMIT 1
             FOR UPDATE SKIP LOCKED
            """
        )

    update_sql = text(
        """
        UPDATE claude_agent.routine_jobs
           SET locked_by = :holder,
               locked_at = now(),
               ttl_secs = :ttl
         WHERE name = :name
        RETURNING name, routine_kind, interval_secs, next_run_at, last_run_at,
                  last_status, last_summary, locked_by, locked_at, ttl_secs,
                  payload, created_by, created_at
        """
    )

    with Session(get_engine()) as session:
        if name is not None:
            row = session.execute(select_sql, {"name": name}).first()
        else:
            row = session.execute(select_sql, {"kind": kind}).first()

        if row is None:
            session.rollback()
            return None

        # If the row exists but is still locked (live TTL), refuse.
        if (
            row.locked_by is not None
            and row.locked_at is not None
            and row.ttl_secs is not None
        ):
            still_live = session.execute(
                text(
                    "SELECT (:locked_at + (:ttl || ' seconds')::interval) > now() AS live"
                ),
                {"locked_at": row.locked_at, "ttl": row.ttl_secs},
            ).scalar()
            if still_live:
                session.rollback()
                return None

        claimed = session.execute(
            update_sql, {"holder": holder, "ttl": ttl_secs, "name": row.name}
        ).first()
        session.commit()

    return _row_to_dict(claimed) if claimed else None


def complete_job(name: str, status: str, summary: str | None = None) -> bool:
    """Mark a job complete.

    Sets ``last_run_at = now()``, ``last_status``, and (if provided)
    ``last_summary``; clears the lock fields. If ``interval_secs`` is non-null
    on the row, advances ``next_run_at`` by that many seconds from now;
    otherwise leaves ``next_run_at`` unchanged.
    """
    sql = text(
        """
        UPDATE claude_agent.routine_jobs
           SET last_run_at = now(),
               last_status = :status,
               last_summary = COALESCE(:summary, last_summary),
               locked_by = NULL,
               locked_at = NULL,
               next_run_at = CASE
                   WHEN interval_secs IS NOT NULL
                   THEN now() + (interval_secs || ' seconds')::interval
                   ELSE next_run_at
               END
         WHERE name = :name
        """
    )
    with Session(get_engine()) as session:
        result = session.execute(
            sql, {"name": name, "status": status, "summary": summary}
        )
        session.commit()
    return result.rowcount > 0


def register_job(
    name: str,
    kind: str,
    interval_secs: int | None = None,
    payload: dict | None = None,
    next_run_at: datetime | None = None,
    created_by: str = "unknown",
) -> bool:
    """Insert a new routine_jobs row. Raises ``IntegrityError`` if name exists."""
    sql = text(
        """
        INSERT INTO claude_agent.routine_jobs
            (name, routine_kind, interval_secs, next_run_at, payload, created_by)
        VALUES
            (:name, :kind, :interval_secs, :next_run_at,
             CAST(:payload AS JSONB), :created_by)
        """
    )
    payload_json = json.dumps(payload) if payload is not None else None
    with Session(get_engine()) as session:
        session.execute(
            sql,
            {
                "name": name,
                "kind": kind,
                "interval_secs": interval_secs,
                "next_run_at": next_run_at,
                "payload": payload_json,
                "created_by": created_by,
            },
        )
        session.commit()
    return True


def deregister_job(name: str) -> bool:
    """Delete a routine_jobs row. Returns True if a row was deleted."""
    sql = text("DELETE FROM claude_agent.routine_jobs WHERE name = :name")
    with Session(get_engine()) as session:
        result = session.execute(sql, {"name": name})
        session.commit()
    return result.rowcount > 0


def trigger_job(name: str) -> bool:
    """Set ``next_run_at = now()`` so the row becomes immediately claimable."""
    sql = text(
        "UPDATE claude_agent.routine_jobs SET next_run_at = now() WHERE name = :name"
    )
    with Session(get_engine()) as session:
        result = session.execute(sql, {"name": name})
        session.commit()
    return result.rowcount > 0
