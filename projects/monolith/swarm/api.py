"""Public factory admission fence used by the shared session executor."""

from __future__ import annotations


def factory_session_allowed(local_session_id: str | None) -> bool:
    """Apply current factory controls only to factory-owned session identities."""
    if not local_session_id or not local_session_id.startswith("factory:"):
        return True
    fields = local_session_id.split(":")
    if len(fields) != 4 or not fields[1] or not fields[2] or not fields[3].isdigit():
        return False
    from swarm.factory_controls import can_start

    return can_start(fields[1])["ok"]


def get_decision_reference(session, decision_id: int) -> dict | None:
    """Read one exact decision for knowledge association."""
    from swarm.models import SwarmDecision

    row = session.get(SwarmDecision, decision_id, populate_existing=True)
    if row is None:
        return None
    return {
        "decision_id": row.id,
        "workflow_id": row.workflow_id,
        "node_key": row.node_key,
        "state": "open" if row.decided_at is None else "decided",
    }
