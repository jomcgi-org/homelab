"""Opportunistic TTL locks for ad-hoc dedup, keyed by free-form string.

Used by Routines that discover work themselves (e.g. "I'm fixing PR 123")
and need to ensure only one Routine works on a key at a time. For
scheduled work (routine_jobs rows) use the lock fields on that table
instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

from core.db import get_engine


@dataclass(frozen=True)
class AcquireResult:
    acquired: bool
    lock_id: UUID | None
    expires_at: datetime | None


def acquire(key: str, holder: str, ttl_secs: int) -> AcquireResult:
    """Take a lock keyed by `key`. Steals expired locks; refuses live ones."""
    sql = text(
        """
        INSERT INTO claude_agent.agent_locks (key, holder, expires_at)
        VALUES (:key, :holder, now() + (:ttl || ' seconds')::interval)
        ON CONFLICT (key) DO UPDATE
            SET holder = EXCLUDED.holder,
                acquired_at = now(),
                expires_at = EXCLUDED.expires_at,
                lock_id = gen_random_uuid()
            WHERE claude_agent.agent_locks.expires_at < now()
        RETURNING lock_id, expires_at
        """
    )
    with Session(get_engine()) as session:
        row = session.execute(
            sql, {"key": key, "holder": holder, "ttl": ttl_secs}
        ).first()
        session.commit()
    if row is None:
        return AcquireResult(acquired=False, lock_id=None, expires_at=None)
    return AcquireResult(acquired=True, lock_id=row[0], expires_at=row[1])


def extend(lock_id: UUID, ttl_secs: int) -> datetime | None:
    """Extend an existing lock by `ttl_secs` from now. Returns None if the
    lock no longer exists or has been re-acquired (different lock_id)."""
    sql = text(
        """
        UPDATE claude_agent.agent_locks
           SET expires_at = now() + (:ttl || ' seconds')::interval
         WHERE lock_id = :lock_id
        RETURNING expires_at
        """
    )
    with Session(get_engine()) as session:
        row = session.execute(sql, {"lock_id": str(lock_id), "ttl": ttl_secs}).first()
        session.commit()
    return row[0] if row else None


def release(lock_id: UUID) -> bool:
    """Release a lock by id. Returns True if a row was deleted."""
    sql = text("DELETE FROM claude_agent.agent_locks WHERE lock_id = :lock_id")
    with Session(get_engine()) as session:
        result = session.execute(sql, {"lock_id": str(lock_id)})
        session.commit()
    return result.rowcount > 0


def list_active(prefix: str | None = None) -> list[dict]:
    """List currently-held (unexpired) locks, optionally filtered by key prefix."""
    sql = text(
        """
        SELECT key, holder, acquired_at, expires_at
          FROM claude_agent.agent_locks
         WHERE expires_at > now()
           AND (CAST(:prefix AS text) IS NULL OR key LIKE CAST(:prefix AS text) || '%')
         ORDER BY expires_at
        """
    )
    with Session(get_engine()) as session:
        rows = session.execute(sql, {"prefix": prefix}).fetchall()
    return [
        {"key": r[0], "holder": r[1], "acquired_at": r[2], "expires_at": r[3]}
        for r in rows
    ]
