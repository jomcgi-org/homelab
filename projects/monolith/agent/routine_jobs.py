"""Operations over ``claude_agent.routine_jobs``, delegated work for cloud Routines.

These functions back the ``monolith-agent-*-routine-job`` MCP tools and the
in-cluster Luna drainer. Unlike ``scheduler.api``'s ``scheduled_jobs``, these
rows use explicit SKIP LOCKED claims so either consumer can safely lease work.
Completing a one-shot row clears ``next_run_at``; ``trigger_job`` explicitly
re-arms it by setting ``next_run_at = now()``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, text
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


def list_jobs(
    due_only: bool = False,
    kind: str | None = None,
    kinds: tuple[str, ...] | list[str] | None = None,
) -> list[dict]:
    """Return routine_jobs rows, optionally filtered to due-only and/or by kind."""
    kind_filter = "routine_kind = ANY(:kinds)" if kinds else "TRUE"
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
           AND ("""
        + kind_filter
        + """)
         ORDER BY next_run_at NULLS LAST, name
        """
    )
    with Session(get_engine()) as session:
        params = {"due_only": due_only, "kind": kind, "kinds": list(kinds or [])}
        rows = session.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def claim_job(
    holder: str,
    ttl_secs: int,
    kind: str | None = None,
    kinds: tuple[str, ...] | list[str] | None = None,
    name: str | None = None,
) -> dict | None:
    """Claim a routine_jobs row, including its JSONB payload.

    If ``name`` is set, attempt to claim that specific row; returns None if it's
    held with a still-live lock. If ``name`` is None, claim the next due
    unclaimed row, optionally filtered by ``kind``. Uses ``SELECT FOR UPDATE
    SKIP LOCKED`` so concurrent claimers never block each other.
    """
    engine = get_engine()
    sqlite = engine.dialect.name == "sqlite"
    table = "routine_jobs" if sqlite else "claude_agent.routine_jobs"
    now_expr = "CURRENT_TIMESTAMP" if sqlite else "now()"
    expired_expr = (
        "datetime(locked_at, '+' || ttl_secs || ' seconds') < CURRENT_TIMESTAMP"
        if sqlite
        else "locked_at + (ttl_secs || ' seconds')::interval < now()"
    )
    if name is not None:
        select_sql = text(
            f"""
            SELECT name, locked_by, locked_at, ttl_secs
              FROM {table}
             WHERE name = :name
             {"" if sqlite else "FOR UPDATE"}
            """
        )
    else:
        if kinds and sqlite:
            kinds_filter = "routine_kind IN :kinds"
        elif kinds:
            kinds_filter = "routine_kind = ANY(:kinds)"
        else:
            kinds_filter = "TRUE"
        select_sql = text(
            f"""
            SELECT name, locked_by, locked_at, ttl_secs
              FROM {table}
             WHERE next_run_at IS NOT NULL
               AND next_run_at <= {now_expr}
               AND (
                    locked_by IS NULL
                    OR {expired_expr}
               )
               AND (CAST(:kind AS text) IS NULL OR routine_kind = CAST(:kind AS text))
               AND ("""
            + kinds_filter
            + f""")
             ORDER BY next_run_at ASC
             LIMIT 1
             {"" if sqlite else "FOR UPDATE SKIP LOCKED"}
            """
        )
        if kinds and sqlite:
            select_sql = select_sql.bindparams(bindparam("kinds", expanding=True))

    update_sql = text(
        f"""
        UPDATE {table}
           SET locked_by = :holder,
               locked_at = {now_expr},
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
            row = session.execute(
                select_sql, {"kind": kind, "kinds": list(kinds or [])}
            ).first()

        if row is None:
            session.rollback()
            return None

        # If the row exists but is still locked (live TTL), refuse.
        if (
            row.locked_by is not None
            and row.locked_at is not None
            and row.ttl_secs is not None
        ):
            if sqlite:
                still_live = session.execute(
                    text(
                        "SELECT datetime(:locked_at, '+' || :ttl || ' seconds') "
                        "> CURRENT_TIMESTAMP AS live"
                    ),
                    {"locked_at": row.locked_at, "ttl": row.ttl_secs},
                ).scalar()
            else:
                still_live = session.execute(
                    text(
                        "SELECT (:locked_at + (:ttl || ' seconds')::interval) "
                        "> now() AS live"
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
    on the row, advances ``next_run_at`` by that many seconds from now.
    One-shot rows clear ``next_run_at`` and remain idle until ``trigger_job``
    re-arms them.
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
                   ELSE NULL
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
    """Re-arm a row by setting ``next_run_at = now()`` so it is claimable."""
    sql = text(
        "UPDATE claude_agent.routine_jobs SET next_run_at = now() WHERE name = :name"
    )
    with Session(get_engine()) as session:
        result = session.execute(sql, {"name": name})
        session.commit()
    return result.rowcount > 0


def defer_job(name: str, seconds: int) -> bool:
    """Re-arm a job after ``seconds`` while clearing any active claim."""
    engine = get_engine()
    if engine.dialect.name == "sqlite":
        table = "routine_jobs"
        deferred_expr = "datetime(CURRENT_TIMESTAMP, '+' || :seconds || ' seconds')"
    else:
        table = "claude_agent.routine_jobs"
        deferred_expr = "now() + (:seconds || ' seconds')::interval"
    sql = text(
        f"""
        UPDATE {table}
           SET next_run_at = {deferred_expr},
               locked_by = NULL,
               locked_at = NULL
         WHERE name = :name
        """
    )
    with Session(get_engine()) as session:
        result = session.execute(sql, {"name": name, "seconds": seconds})
        session.commit()
    return result.rowcount > 0
