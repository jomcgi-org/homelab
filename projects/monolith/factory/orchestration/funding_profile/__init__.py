"""Server-selected funding execution. Never a caller-selectable node capability."""

import json

from factory.orchestration import factory_controls as controls

KEY = "x-factory-funding-decision"
NAME = "factory-funding-v1"
MODELS = frozenset({"opus", "sonnet", "fable"})


def profile(request):
    from factory.orchestration import factory_funding as funding

    if request.get("model") not in MODELS or not request.get("repair_recovery"):
        raise ValueError("unsupported_funding_profile")
    return {
        "name": NAME,
        "request_id": request["audit_id"],
        "request_sha256": controls.digest(request),
        "schema_sha256": controls.digest(funding.SCHEMA),
        "model": request["model"],
    }


def schema(task_id, node_key, *, session=None):
    from factory.orchestration import factory_funding as funding

    if session is None:
        with controls._read_session() as db:
            return schema(task_id, node_key, session=db)
    row = controls._receipt(session, task_id)
    from factory.orchestration import backlog_refill as refill

    if row is None or row.actor != refill.ACTOR:
        return funding.SCHEMA
    request = funding.latest(session, task_id, "funding_review_requested")
    if not request or request["node_key"] != node_key:
        raise ValueError("missing_funding_profile_request")
    return {**funding.SCHEMA, KEY: profile(request)}


def allowed(db, task_id, node_key, attempt, *, pin=None):
    from factory.orchestration import backlog_refill_repair as repair
    from factory.orchestration import factory_funding as funding
    from factory.orchestration.factory_funding_limits import review_authority

    row = controls._receipt(db, task_id)
    request = funding.latest(db, task_id, "funding_review_requested")
    if (
        type(attempt) is not int
        or attempt != 1
        or not request
        or request["node_key"] != node_key
        or not request.get("repair_recovery")
        or repair.source(db, row) is None
        or review_authority(db, task_id, request["start_key"]) != request
        or not funding._repair_review_valid(db, task_id, request["repair_recovery"])
        or funding.routine_hold(db, row, task_id)
        or funding._recovery_held(db, task_id)
        or controls.decision_in_flight(db, row.id)
    ):
        return False
    try:
        expected = schema(task_id, node_key, session=db)
    except ValueError:
        return False
    return pin is None or (
        pin.get("artifact_schema") == expected
        and pin.get("model") == request["model"]
        and controls.digest(pin.get("prompt")) == request.get("prompt_sha256")
        and pin.get("workflow_id") == request["start_key"]
        and pin.get("read_only") is not True
        and pin.get("task_id") == task_id
        and pin.get("node_key") == node_key
        and type(pin.get("attempt")) is int
        and pin["attempt"] == attempt
    )


def session_profile(local_id, *, model=None, message=None, session_id=None):
    """Resolve only a live, exact admitted run; unknown selectors fail closed."""
    from sqlmodel import select

    from factory.orchestration.models import SwarmNodeRun

    fields = (local_id or "").split(":")
    if (
        len(fields) != 4
        or fields[0] != "factory"
        or not fields[2].startswith("conductor_funding_")
        or not fields[3].isdigit()
    ):
        return None
    task_id, key, attempt = fields[1], fields[2], int(fields[3])
    with controls._read_session() as db:
        run = db.exec(
            select(SwarmNodeRun).where(
                SwarmNodeRun.task_id == task_id,
                SwarmNodeRun.node_key == key,
                SwarmNodeRun.attempt == attempt,
            )
        ).first()
        if run is None or run.status not in {"admitted", "dispatched"}:
            return None
        pin = json.loads(run.pin_json)
        if KEY not in (pin.get("artifact_schema") or {}):
            return None
        if run.model != pin.get("model") or run.dispatch_key != pin.get("workflow_id"):
            return None
        if not allowed(db, task_id, key, attempt, pin=pin):
            return None
        if model is not None and model != pin["model"]:
            return None
        if message is not None:
            from knowledge.api import matches_message_recall

            from factory.orchestration.node_workflows import _node_prompt

            expected = _node_prompt(
                pin["prompt"], pin["artifact_path"], pin["artifact_schema"]
            )
            if not matches_message_recall(message, expected):
                raise ValueError("funding profile message mismatch")
        if session_id is not None:
            from factory.execution.models import AgentSession
            from factory.orchestration.node_workflows import _expected_session_identity

            agent = db.get(AgentSession, session_id)
            if agent is None or any(
                getattr(agent, key) != value
                for key, value in _expected_session_identity(pin).items()
            ):
                raise ValueError("funding profile session mismatch")
        return pin["artifact_schema"][KEY]
