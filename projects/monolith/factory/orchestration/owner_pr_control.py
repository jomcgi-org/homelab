"""Narrow operator control for the existing owner PR intake producer."""

import hashlib
import json

from core.jsonio import dumps
from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit

ACTION = "owner_pr_control_applied"


def _plan(control, owner, expected_version, reason):
    if type(expected_version) is not int or control.version != expected_version:
        raise ValueError("version_conflict")
    controls._text(reason, "reason", 4000)
    # The same validation as configure; numeric ID and login travel together.
    owner = controls._validate_intake({"owner_pr_author": owner})["owner_pr_author"]
    policy = json.loads(control.policy_json)
    intake = controls.intake_policy(policy)
    if owner is not None and (
        control.state != "enabled"
        or not intake["enabled"]
        or not intake["refine_enabled"]
        or intake["refinement_batch"]
    ):
        raise ValueError("owner_pr_intake_not_available")
    proposal = {
        "version": control.version,
        "state": control.state,
        "generation": policy.get("generation"),
        "before": intake["owner_pr_author"],
        "after": owner,
        "reason": reason,
        "policy_sha256": hashlib.sha256(control.policy_json.encode()).hexdigest(),
    }
    proposal["preview_sha256"] = hashlib.sha256(dumps(proposal).encode()).hexdigest()
    return proposal


def preview(owner, expected_version, reason):
    with controls._read_session() as db:
        from factory.orchestration.factory_models import FactoryControl

        proposal = _plan(
            db.get(FactoryControl, "factory"), owner, expected_version, reason
        )
        return {"ok": True, "preview": proposal}


def apply(actor, owner, expected_version, reason, request_key, expected_preview_sha256):
    controls._text(request_key, "request_key", 200)
    request = {
        "actor": actor,
        "owner": owner,
        "expected_version": expected_version,
        "reason": reason,
        "request_key": request_key,
        "expected_preview_sha256": expected_preview_sha256,
    }
    with controls._locked_session() as (db, control):
        # Replay is checked before version CAS, so a lost response is safe.
        events = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == ACTION)
        ).all()
        for event in events:
            detail = json.loads(event.detail_json)
            if detail["request"]["request_key"] == request_key:
                if detail["request"] != request:
                    raise ValueError("owner_pr_request_conflict")
                return detail["result"]
        proposal = _plan(control, owner, expected_version, reason)
        if proposal["preview_sha256"] != expected_preview_sha256:
            raise ValueError("owner_pr_preview_changed")
        policy = json.loads(control.policy_json)
        policy["intake"] = {
            **policy.get("intake", {}),
            "owner_pr_author": proposal["after"],
        }
        control.policy_json = dumps(policy)
        control.version += 1
        control.actor = actor
        control.updated_at = controls._now()
        result = {
            "ok": True,
            "version": control.version,
            "generation": policy.get("generation"),
            "dispatched": False,
        }
        controls._audit(
            db, actor, ACTION, request=request, proposal=proposal, result=result
        )
        return result
