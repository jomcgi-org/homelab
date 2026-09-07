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
