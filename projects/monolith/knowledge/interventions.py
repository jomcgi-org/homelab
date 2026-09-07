"""Durable intervention inbox lifecycle operations."""

from __future__ import annotations

from sqlalchemy import text, update
from sqlmodel import Session

from knowledge.models import Intervention


def lock_intervention(session: Session, raw_id: str) -> Intervention | None:
    """Serialize lifecycle changes within the caller's transaction.

    A no-op write obtains a PostgreSQL row lock and a SQLite writer lock.
    Reload after locking so a cached, older revision cannot pass validation.
    This private operator path is separate from the insert-only agent creator.
    """
    table = Intervention.__table__
    locked = session.execute(
        update(table)
        .where(table.c.raw_id == raw_id)
        .values(revision=table.c.revision)
        .returning(table.c.raw_id)
    ).first()
    if locked is None:
        return None
    return session.get(Intervention, raw_id, populate_existing=True)


def create_intervention(session: Session, raw_id: str) -> bool:
    """Create the open inbox row, returning true only for the insert winner."""
    table = Intervention.__table__
    table_name = ".".join(
        part for part in (table.schema, table.name) if part is not None
    )
    row = session.execute(
        text(
            f"INSERT INTO {table_name} (raw_id) VALUES (:raw_id) "
            "ON CONFLICT (raw_id) DO NOTHING RETURNING raw_id"
        ),
        {"raw_id": raw_id},
    ).first()
    return row is not None


def decision_reference(session: Session, decision_id: int) -> dict | None:
    """Return an exact decision identity and state without mutating swarm."""
    from swarm.api import get_decision_reference

    return get_decision_reference(session, decision_id)
