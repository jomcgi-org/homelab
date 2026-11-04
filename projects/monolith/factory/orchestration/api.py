"""Public factory admission fence used by the shared session executor."""

from __future__ import annotations

import json
import logging
import posixpath

_UNSPECIFIED_MODEL = object()


def _check_relative_path(path: str) -> None:
    if "\x00" in path or "\\" in path:
        raise ValueError("pin['artifact_path'] must use relative POSIX syntax")
    if posixpath.isabs(path):
        raise ValueError("pin['artifact_path'] must be relative, not absolute")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError("pin['artifact_path'] must not escape its directory")
    if posixpath.normpath(path).startswith(".."):
        raise ValueError("pin['artifact_path'] must not escape its directory")


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


def factory_session_allowed(
    local_session_id: str | None, *, model: object = _UNSPECIFIED_MODEL
) -> bool:
    """Fence a factory session and, at physical dispatch, its actual turn model."""
    if not local_session_id or not local_session_id.startswith("factory:"):
        return True
    fields = local_session_id.split(":")
    if len(fields) != 4 or not fields[1] or not fields[2] or not fields[3].isdigit():
        return False
    from factory.orchestration.factory_controls import can_start

    allowed = can_start(fields[1], start_key="factory-node:" + ":".join(fields[1:]))[
        "ok"
    ]
    if not allowed:
        return False
    # Task-wide live transport fence: even an accidental generic funding node
    # cannot escape a scout's fixed two-node/model subscription contract.
    from factory.orchestration.factory_improvement_fence import transport_allowed
    from factory.orchestration.backlog_refill import dispatch_allowed as refill_allowed

    try:
        if model is not _UNSPECIFIED_MODEL:
            from factory.execution import normalize_model
            from factory.orchestration import factory_controls as controls

            with controls._read_session() as db:
                row = controls._receipt(db, fields[1])
                if row is None:
                    return False
                if json.loads(row.direction_json or "{}").get("model_restriction"):
                    if (
                        normalize_model(model)
                        not in controls._effective_policy(db, row)["allowed_models"]
                    ):
                        return False
        return transport_allowed(
            fields[1], fields[2], int(fields[3])
        ) and refill_allowed(fields[1], fields[2], int(fields[3]), physical=True)
    except Exception:  # unavailable evidence must not start speculative work
        return False


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
