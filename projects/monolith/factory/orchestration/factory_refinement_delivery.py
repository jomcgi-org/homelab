"""Exact operator continuation of held, completed refinement into queued delivery.

The old receipt stays terminal. Restrictions are copied by the server, never
accepted from the caller, and checked against the audit again before admission.
"""

import hashlib
import json

from sqlmodel import select

from core.jsonio import dumps
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_validation as validation
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt

ACTION = "refinement_delivery_prepared"
MARKER = "refinement_delivery"


def _digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def _inherited(row):
    direction = json.loads(row.direction_json or "{}")
    hold = direction.get("landing_hold")
    gates = direction.get("conductor_gates")
    # This operation carries the refinement's staging restriction only. Other
    # hold types, delivery grants and unknown direction semantics need their owner.
    if (
        set(direction) != {"landing_hold", "conductor_gates"}
        or not isinstance(hold, dict)
        or hold.get("actor") != "factory:conductor"
        or hold.get("cause") != f"refine-gate:{row.task_id}"
        or not isinstance(hold.get("reason"), str)
        or not hold["reason"].strip()
        or not isinstance(gates, list)
        or not gates
        or any(not isinstance(gate, dict) for gate in gates)
        or not any(gate.get("leave_unmerged") is True for gate in gates)
    ):
        raise ValueError("refinement_delivery_restrictions_not_supported")
    return direction


def _preview(db, control, receipt_id, reason, observation):
    row = db.get(FactoryReceipt, receipt_id)
    if row is None or row.state != "succeeded" or row.task_class != "refine":
        raise ValueError("refinement_delivery_requires_completed_refinement")
    inherited = _inherited(row)
    result = validation._preview(
        db,
        control,
        receipt_id,
        reason,
        observation=observation,
        inherit_landing_hold=True,
    )
    result.pop("preview_sha256")
    result.update(
        operation=ACTION,
        source_receipt=row.model_dump(mode="json"),
        inherited_direction=inherited,
        delivery_task_class=controls.DEFAULT_TASK_CLASS,
        delivery_receipt_created=True,
        execution_authorized=False,
        landing_authorized=False,
    )
    result["preview_sha256"] = _digest(result)
    return result


def preview(receipt_id, reason):
    observation = validation._refinement_observation(
        receipt_id, inherit_landing_hold=True
    )
    with controls._locked_session() as (db, control):
        return _preview(db, control, receipt_id, reason, observation)


def _records(db):
    return [
        json.loads(raw)
        for raw in db.exec(
            select(FactoryAudit.detail_json).where(FactoryAudit.action == ACTION)
        ).all()
    ]


def _replay(db, actor, request):
    for detail in _records(db):
        if (
            detail["actor_identity"] == actor
            and detail["request"]["request_key"] == request["request_key"]
        ):
            if detail["request"] != request:
                raise ValueError("conflicting_refinement_delivery_request")
            return detail["result"]
    return None


def apply(receipt_id, reason, *, actor, request_key, expected_preview_sha256):
    from factory.orchestration.factory_intake import receive_issue

    actor = controls._text(actor, "actor")
    request = {
        "receipt_id": receipt_id,
        "reason": reason,
        "request_key": controls._text(request_key, "request_key"),
        "expected_preview_sha256": expected_preview_sha256,
    }
    with controls._read_session() as db:
        replay = _replay(db, actor, request)
        if replay is not None:
            return replay
    observation = validation._refinement_observation(
        receipt_id, inherit_landing_hold=True
    )
    with controls._locked_session() as (db, control):
        replay = _replay(db, actor, request)
        if replay is not None:
            return replay
        proposal = _preview(db, control, receipt_id, reason, observation)
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("refinement_delivery_preview_changed")
        configured = controls.set_control(
            "configure",
            actor,
            policy=proposal["policy_after"],
            session=db,
        )
        if not configured["ok"]:
            raise ValueError(configured["reason"])
        issue = observation["issue"]
        received = receive_issue(
            proposal["repo"],
            proposal["issue_number"],
            issue["title"],
            issue.get("body") or "",
            issue["html_url"],
            actor,
            generation=proposal["policy_after"]["generation"],
            task_class=proposal["delivery_task_class"],
            work_item_id=proposal["source_receipt"]["work_item_id"],
            session=db,
        )
        if not received.get("created"):
            raise ValueError("refinement_delivery_already_exists")
        destination = db.get(FactoryReceipt, received["receipt"]["id"])
        direction = {
            **proposal["inherited_direction"],
            MARKER: {
                "source_receipt_id": receipt_id,
                "preview_sha256": expected_preview_sha256,
            },
        }
        destination.direction_json = dumps(direction)
        result = {
            "ok": True,
            "source_receipt_id": receipt_id,
            "receipt_id": destination.id,
            "state": "queued",
            "control_state": "paused",
            "version": configured["version"],
            "execution_authorized": False,
            "landing_authorized": False,
            "preview_sha256": expected_preview_sha256,
        }
        controls._audit(
            db,
            actor,
            ACTION,
            task_id=proposal["task_id"],
            request=request,
            result=result,
            actor_identity=actor,
            source_receipt=proposal["source_receipt"],
            destination_identity={
                key: getattr(destination, key)
                for key in (
                    "repo",
                    "issue_number",
                    "generation",
                    "task_class",
                    "work_item_id",
                )
            },
            inherited_direction=direction,
            policy_after=proposal["policy_after"],
        )
        return result


def admission_refusal(db, row, policy):
    """Audit lookup also detects a stripped marker, before any new task exists."""
    direction = json.loads(row.direction_json or "{}")
    records = [
        detail for detail in _records(db) if detail["result"]["receipt_id"] == row.id
    ]
    if not records and MARKER not in direction:
        return None
    if len(records) != 1:
        return "refinement_delivery_provenance_missing"
    detail = records[0]
    source = db.get(FactoryReceipt, detail["source_receipt"]["id"])
    if (
        source is None
        or source.model_dump(mode="json") != detail["source_receipt"]
        or direction != detail["inherited_direction"]
        or any(
            getattr(row, key) != value
            for key, value in detail["destination_identity"].items()
        )
        or controls.validate_policy(policy) != detail["policy_after"]
    ):
        return "refinement_delivery_evidence_changed"
    return None
