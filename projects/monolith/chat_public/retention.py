"""Public chat retention and takedown purge (ADR security/005).

Sync cores taking an explicit sqlmodel ``Session``. They flush and leave the
commit to the caller (the jobs CLI), so a dry run can roll back.

``chat_public.shared_snapshots.source_session_id`` is ON DELETE SET NULL, so
deleting a session first would orphan its snapshots and a later takedown could
never find them. Every purge therefore resolves snapshots through
``source_session_id`` and deletes them BEFORE the session rows, in one
transaction. Retention keeps a shared session until its snapshot expires so a
takedown can always resolve it.

Every purge writes one ``chat_public.purge_audit`` row: counts and a sha256
digest of the takedown selector only, never the raw id or ip_hash.
"""

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, text

logger = logging.getLogger("monolith.chat_public.retention")

# Reversible defaults; override with the env vars below (read at call time).
SESSION_RETENTION_DAYS = 30
SNAPSHOT_RETENTION_DAYS = 365

SESSION_RETENTION_ENV = "CHAT_PUBLIC_SESSION_RETENTION_DAYS"
SNAPSHOT_RETENTION_ENV = "CHAT_PUBLIC_SNAPSHOT_RETENTION_DAYS"


@dataclass(frozen=True)
class PurgeReport:
    action: str
    sessions_deleted: int = 0
    messages_deleted: int = 0
    snapshots_deleted: int = 0


def _window_days(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name)
    if raw is None or raw.strip() == "":
        return default
    days = int(raw)
    if days < 1:
        raise ValueError(f"{env_name} must be >= 1, got {days}")
    return days


def session_retention_days() -> int:
    return _window_days(SESSION_RETENTION_ENV, SESSION_RETENTION_DAYS)


def snapshot_retention_days() -> int:
    return _window_days(SNAPSHOT_RETENTION_ENV, SNAPSHOT_RETENTION_DAYS)


def _write_audit(
    session: Session, report: PurgeReport, selector: str | None = None
) -> None:
    digest = hashlib.sha256(selector.encode()).hexdigest() if selector else None
    session.execute(
        text(
            "INSERT INTO chat_public.purge_audit "
            "(action, selector_sha256, sessions_deleted, messages_deleted, "
            "snapshots_deleted) "
            "VALUES (:action, :selector, :sessions, :messages, :snapshots)"
        ),
        {
            "action": report.action,
            "selector": digest,
            "sessions": report.sessions_deleted,
            "messages": report.messages_deleted,
            "snapshots": report.snapshots_deleted,
        },
    )
    session.flush()


def _delete_sessions(session: Session, session_ids: list[str]) -> tuple[int, int, int]:
    """Delete snapshots, then messages, then sessions for ``session_ids``.

    Returns ``(sessions, messages, snapshots)`` deleted. Snapshots go first:
    once the session row is gone the SET NULL FK has erased the link.
    """
    if not session_ids:
        return 0, 0, 0
    params = {"ids": session_ids}
    snapshots = session.execute(
        text(
            "DELETE FROM chat_public.shared_snapshots "
            "WHERE source_session_id = ANY(:ids)"
        ),
        params,
    ).rowcount
    messages = session.execute(
        text("DELETE FROM chat_public.messages WHERE session_id = ANY(:ids)"),
        params,
    ).rowcount
    sessions = session.execute(
        text("DELETE FROM chat_public.sessions WHERE id = ANY(:ids)"), params
    ).rowcount
    return sessions, messages, snapshots


def purge_expired(session: Session, now: datetime | None = None) -> PurgeReport:
    """Delete expired snapshots, then idle sessions with no remaining snapshot."""
    now = now or datetime.now(timezone.utc)
    snapshot_cutoff = now - timedelta(days=snapshot_retention_days())
    session_cutoff = now - timedelta(days=session_retention_days())

    expired_snapshots = session.execute(
        text("DELETE FROM chat_public.shared_snapshots WHERE created_at < :cutoff"),
        {"cutoff": snapshot_cutoff},
    ).rowcount

    # A shared session is kept until its snapshot expires, so a takedown can
    # still resolve it through source_session_id.
    idle_ids = list(
        session.execute(
            text(
                "SELECT s.id FROM chat_public.sessions s "
                "WHERE s.last_seen_at < :cutoff "
                "AND NOT EXISTS (SELECT 1 FROM chat_public.shared_snapshots n "
                "WHERE n.source_session_id = s.id) "
                "FOR UPDATE OF s"
            ),
            {"cutoff": session_cutoff},
        ).scalars()
    )
    sessions, messages, _ = _delete_sessions(session, idle_ids)

    report = PurgeReport(
        action="retention",
        sessions_deleted=sessions,
        messages_deleted=messages,
        snapshots_deleted=expired_snapshots,
    )
    _write_audit(session, report)
    logger.info(
        "chat-public retention: sessions=%d messages=%d snapshots=%d",
        report.sessions_deleted,
        report.messages_deleted,
        report.snapshots_deleted,
    )
    return report


def takedown(
    session: Session, *, session_id: str | None = None, ip_hash: str | None = None
) -> PurgeReport:
    """Delete one session, or every session with one stored ip_hash.

    Takes the STORED ip_hash (the salt lives only in monolith-public), never a
    raw IP. A selector that matches nothing still writes a zero-count audit row.
    """
    if bool(session_id) == bool(ip_hash):
        raise ValueError("takedown needs exactly one of session_id or ip_hash")

    if session_id:
        action, selector = "takedown_session", session_id
        ids = [session_id]
    else:
        action, selector = "takedown_ip_hash", ip_hash
        ids = list(
            session.execute(
                text("SELECT id FROM chat_public.sessions WHERE ip_hash = :sel"),
                {"sel": selector},
            ).scalars()
        )

    sessions, messages, snapshots = _delete_sessions(session, ids)
    report = PurgeReport(
        action=action,
        sessions_deleted=sessions,
        messages_deleted=messages,
        snapshots_deleted=snapshots,
    )
    _write_audit(session, report, selector)
    logger.info(
        "chat-public takedown: action=%s sessions=%d messages=%d snapshots=%d",
        action,
        sessions,
        messages,
        snapshots,
    )
    return report
