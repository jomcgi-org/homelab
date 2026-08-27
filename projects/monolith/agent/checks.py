"""Read-only cluster diagnostics for cloud Routines.

Each function returns a ``list[dict]`` describing some pathological state
in the cluster. The MCP surface composes them as separate tools so a
Routine can pull one signal at a time without a wrapping aggregate.

- ``check_stuck_jobs``    : ``scheduler.scheduled_jobs`` rows whose lock
                            has held longer than ``threshold_mins``.
- ``check_orphan_jobs``   : ``scheduler.scheduled_jobs`` rows whose
                            handler is not in the in-cluster registry.
- ``check_dead_letters``  : ``knowledge.atom_raw_provenance`` rows
                            indicating a raw input that exhausted all
                            retry attempts (mirrors ``GET /api/knowledge/dead-letter``).
- ``trigger_job``         : kick a ``scheduler.scheduled_jobs`` row to
                            run on the next tick by setting
                            ``next_run_at = now()``.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session

from core.db import get_engine


def check_stuck_jobs(threshold_mins: int = 10) -> list[dict]:
    """Return scheduler rows whose lock has held longer than ``threshold_mins``.

    A row is considered stuck when ``locked_by`` is set and ``locked_at``
    is older than ``threshold_mins`` minutes ago. The scheduler's own
    expiry uses ``ttl_secs``; this check is a coarser human-facing signal
    that surfaces lock-leaks regardless of TTL.
    """
    sql = text(
        """
        SELECT name, interval_secs, next_run_at, last_run_at, last_status,
               locked_by, locked_at, ttl_secs
          FROM scheduler.scheduled_jobs
         WHERE locked_by IS NOT NULL
           AND locked_at IS NOT NULL
           AND locked_at < now() - make_interval(mins => :threshold_mins)
         ORDER BY locked_at
        """
    )
    with Session(get_engine()) as session:
        rows = session.execute(sql, {"threshold_mins": threshold_mins}).fetchall()
    return [
        {
            "name": r[0],
            "interval_secs": r[1],
            "next_run_at": r[2],
            "last_run_at": r[3],
            "last_status": r[4],
            "locked_by": r[5],
            "locked_at": r[6],
            "ttl_secs": r[7],
        }
        for r in rows
    ]


def check_orphan_jobs() -> list[dict]:
    """Return scheduler rows whose ``name`` has no registered handler.

    Reads the in-process scheduler registry once (via
    ``scheduler.api.registered_names``), so this only sees handlers wired
    by the running monolith. Useful for spotting rows left behind by
    removed handlers.
    """
    from scheduler.api import registered_names

    registered = set(registered_names())
    sql = text(
        """
        SELECT name, interval_secs, next_run_at, last_run_at, last_status,
               locked_by, locked_at, ttl_secs
          FROM scheduler.scheduled_jobs
         ORDER BY name
        """
    )
    with Session(get_engine()) as session:
        rows = session.execute(sql).fetchall()
    return [
        {
            "name": r[0],
            "interval_secs": r[1],
            "next_run_at": r[2],
            "last_run_at": r[3],
            "last_status": r[4],
            "locked_by": r[5],
            "locked_at": r[6],
            "ttl_secs": r[7],
        }
        for r in rows
        if r[0] not in registered
    ]


def check_dead_letters(limit: int = 20) -> list[dict]:
    """Return raw inputs that exhausted all gardener retry attempts.

    Mirrors the query used by ``GET /api/knowledge/dead-letter`` and the
    ``debug-knowledge-ingest`` skill: a raw is dead-lettered when its
    ``atom_raw_provenance`` row has ``derived_note_id = 'failed'`` and
    ``retry_count >= MAX_GARDENER_RETRIES``.
    """
    from knowledge.api import MAX_GARDENER_RETRIES

    sql = text(
        """
        SELECT r.id, r.path, r.source, p.error, p.retry_count, p.created_at
          FROM knowledge.atom_raw_provenance p
          JOIN knowledge.raw_inputs r ON r.id = p.raw_fk
         WHERE p.derived_note_id = 'failed'
           AND p.retry_count >= :max_retries
         ORDER BY p.created_at DESC
         LIMIT :limit
        """
    )
    with Session(get_engine()) as session:
        rows = session.execute(
            sql,
            {"max_retries": MAX_GARDENER_RETRIES, "limit": limit},
        ).fetchall()
    return [
        {
            "id": r[0],
            "path": r[1],
            "source": r[2],
            "error": r[3],
            "retry_count": r[4],
            "last_failed_at": r[5],
        }
        for r in rows
    ]


def trigger_job(name: str) -> bool:
    """Kick a ``scheduler.scheduled_jobs`` row to run on the next tick.

    Sets ``next_run_at = now()`` for the named row. Returns True if a
    row was updated, False if no row by that name exists.
    """
    sql = text(
        "UPDATE scheduler.scheduled_jobs SET next_run_at = now() WHERE name = :name"
    )
    with Session(get_engine()) as session:
        result = session.execute(sql, {"name": name})
        session.commit()
    return result.rowcount > 0
