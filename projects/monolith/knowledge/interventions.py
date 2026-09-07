"""Durable intervention inbox lifecycle operations."""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def create_intervention(session: Session, raw_id: str) -> bool:
    """Create the open inbox row, returning true only for the insert winner."""
    row = session.execute(
        text(
            "INSERT INTO knowledge.interventions (raw_id) VALUES (:raw_id) "
            "ON CONFLICT (raw_id) DO NOTHING RETURNING raw_id"
        ),
        {"raw_id": raw_id},
    ).first()
    return row is not None


def decision_reference(session: Session, decision_id: int) -> dict | None:
    """Return an exact decision identity and state without mutating swarm."""
    from swarm.api import get_decision_reference

    return get_decision_reference(session, decision_id)
