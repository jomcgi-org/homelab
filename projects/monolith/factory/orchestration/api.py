"""Public factory admission fence used by the shared session executor."""

from __future__ import annotations

import json
import logging


def declared_artifact_path(
    session_id: int,
    local_session_id: str | None,
    workflow_id: str | None,
    node_key: str | None,
    node_attempt: int | None,
) -> str | None:
    """Resolve the declaration from this session's exact admitted attempt."""
    if not local_session_id or not local_session_id.startswith("factory:"):
        return None
    try:
        from sqlmodel import Session, select

        from core.db import get_engine
        from factory.orchestration.models import SwarmNodeRun
        from factory.orchestration.node_workflows import _check_relative_path

        fields = local_session_id.split(":")
        if len(fields) != 4 or not fields[1] or not fields[3].isdigit():
            raise ValueError("malformed factory session identity")
        task_id, identity_node, identity_attempt = fields[1:]
        if identity_node != node_key or int(identity_attempt) != node_attempt:
            raise ValueError("factory identity disagrees with session attempt")
        with Session(get_engine()) as db:
            run = db.exec(
                select(SwarmNodeRun).where(
                    SwarmNodeRun.task_id == task_id,
                    SwarmNodeRun.node_key == node_key,
                    SwarmNodeRun.attempt == node_attempt,
                )
            ).first()
            if run is None:
                raise ValueError("exact admitted attempt is absent")
            if run.dispatch_key != workflow_id:
                raise ValueError("admitted dispatch disagrees with session workflow")
            # Session creation schedules the first turn before graph binding.
            if run.session_id is not None and run.session_id != session_id:
                raise ValueError("admitted attempt is bound to another session")
            pin = json.loads(run.pin_json)
        path = pin.get("artifact_path")
        if not isinstance(path, str) or not path:
            raise ValueError("admitted pin has no non-empty artifact path")
        _check_relative_path(path)
        return path
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "No declared artifact for session %s: %s", session_id, exc
        )
        return None


def factory_session_allowed(local_session_id: str | None) -> bool:
    """Apply current factory controls only to factory-owned session identities."""
    if not local_session_id or not local_session_id.startswith("factory:"):
        return True
    fields = local_session_id.split(":")
    if len(fields) != 4 or not fields[1] or not fields[2] or not fields[3].isdigit():
        return False
    from factory.orchestration.factory_controls import can_start

    return can_start(fields[1], start_key="factory-node:" + ":".join(fields[1:]))["ok"]


def get_decision_reference(session, decision_id: int) -> dict | None:
    """Read one exact decision for knowledge association."""
    from factory.orchestration.models import SwarmDecision

    row = session.get(SwarmDecision, decision_id, populate_existing=True)
    if row is None:
        return None
    return {
        "decision_id": row.id,
        "workflow_id": row.workflow_id,
        "node_key": row.node_key,
        "state": "open" if row.decided_at is None else "decided",
    }


def read_factory_attempt_stop_request(
    session,
    task_id: str,
    session_id: int,
    seq: int,
    claim_owner: str,
    dispatch_count: int,
) -> dict | None:
    """Read committed factory stop authority and exact cessation evidence."""
    from factory.orchestration.factory_attempt_stop import (
        read_factory_attempt_stop_request as read,
    )

    return read(session, task_id, session_id, seq, claim_owner, dispatch_count)
