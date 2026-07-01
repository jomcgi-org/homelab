"""Per-session goose sessions.db blob store (ADR 026 Phase 2).

The durable store for a thread's goose SQLite session, so a reply can resume the
prior conversation (Model A) instead of cold-rebuilding from the full transcript
(Model B). This is the single seam between the goosecracker runner and durable
storage: today it is a BYTEA column on the run ledger
(``claude_agent.agent_threads.session_db``); the ADR's S3 object store is deferred
(SeaweedFS S3 auth is currently disabled) and, when enabled, drops in here using
presigned URLs so the guest still holds no credential.

The blob is kilobytes (goose exits between turns, so sessions.db is consistent at
export). One row per session (``upsert_run`` created it before the run), keyed by
``session_id``. Every function opens its own Session, so an async caller must hand
these to ``asyncio.to_thread`` (a sync Session must not run on the event loop).
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlmodel import Session

from app.db import get_engine

logger = logging.getLogger(__name__)


def load(session_id: str) -> bytes | None:
    """Return the stored sessions.db blob for ``session_id``, or None.

    None means "no prior session" (a first/cold run, a legacy row, or a run that
    never persisted one), which the runner reads as "cold run, do not resume".
    """
    with Session(get_engine()) as session:
        row = session.execute(
            text(
                "SELECT session_db FROM claude_agent.agent_threads "
                "WHERE session_id = :s"
            ),
            {"s": session_id},
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return bytes(row[0])


def save(session_id: str, data: bytes) -> None:
    """Persist the sessions.db blob for ``session_id`` onto its ledger row.

    A no-op-safe UPDATE: if the row does not exist yet (it should, upsert_run runs
    first) nothing is written. Stamps last_active_at so the row's recency reflects
    the resume.
    """
    with Session(get_engine()) as session:
        session.execute(
            text(
                "UPDATE claude_agent.agent_threads "
                "SET session_db = :d, last_active_at = now() "
                "WHERE session_id = :s"
            ),
            {"d": data, "s": session_id},
        )
        session.commit()
