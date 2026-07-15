"""Thin repository over sandbox.session: the handle -> EmberVM session mapping.

ORM-level upsert (not a dialect-specific INSERT ... ON CONFLICT) so it runs
identically on SQLite (unit tests, SQLModel.metadata.create_all) and Postgres
(production), mirroring faas/repository.py. Callers own the Session and commit
boundary.

The token stored here is a SECRET (the per-session capability). This module
never logs it; callers must not either.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from sandbox.models import SandboxSession


def get_session_row(session: Session, handle: str) -> SandboxSession | None:
    """Return the session row for a handle, or None if none is mapped."""
    return session.get(SandboxSession, handle)


def upsert_session_row(
    session: Session,
    *,
    handle: str,
    session_id: str,
    token: str,
    expires_at: datetime | None,
) -> SandboxSession:
    """Create or last-write-wins replace the session credentials for a handle.

    Re-mapping a handle (the transparent re-create after a 410) overwrites the
    id, token, and expiry so a stale terminal session is self-healing: the next
    use rebinds the handle to the fresh session with no separate reaper.
    """
    existing = session.get(SandboxSession, handle)
    if existing is None:
        row = SandboxSession(
            handle=handle,
            session_id=session_id,
            token=token,
            expires_at=expires_at,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row

    existing.session_id = session_id
    existing.token = token
    existing.expires_at = expires_at
    session.commit()
    session.refresh(existing)
    return existing


def delete_session_row(session: Session, handle: str) -> bool:
    """Delete the mapping for a handle. Returns whether a row was deleted."""
    row = session.get(SandboxSession, handle)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True
