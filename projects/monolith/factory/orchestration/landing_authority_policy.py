"""Read-only compatibility proofs for existing standing landing decisions.

Every intervening successful transition must retain the same landing terms.
Unknown historical evidence refuses compatibility; restoring bytes is not a
new operator decision. No grants, expiry or accounting are rewritten here.
"""

import json
import re

from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit, FactoryControl

FAMILIES = ("repair_landing", "delivery_landing", "owner_pr_landing")


def terms(policy, state, family):
    repo = policy.get("repo")
    stored = controls._stored_repos(policy).get(repo, {})
    merge = controls.repo_auto_merge_enabled(policy, repo)
    value = {
        "repo": repo,
        "base_branch": policy.get("base_branch"),
        "enabled": state == "enabled",
        "auto_merge": merge,
        "repo_enabled": stored.get("enabled", True),
        "repo_paused": stored.get("paused", False),
        "landing": stored.get("landing", "merge_queue" if merge else "none"),
    }
    if family == "owner_pr_landing":
        intake = controls.intake_policy(policy)
        value["intake"] = {
            key: intake[key]
            for key in (
                "owner_pr_author",
                "enabled",
                "refine_enabled",
                "refinement_batch",
                "labels",
                "exclude_labels",
            )
        }
        for key in ("labels", "exclude_labels"):
            value["intake"][key] = stored.get(key, intake[key])
    return value


def evidence(control):
    policy = json.loads(control.policy_json or "{}")
    return {
        "schema_version": 1,
        "policy_sha256": controls.digest(policy),
        "intake_policy_sha256": controls.digest(policy.get("intake") or {}),
        "terms": {name: terms(policy, control.state, name) for name in FAMILIES},
    }


def compatible(db, event, proposal, family):
    try:
        return _compatible(db, event, proposal, family)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _compatible(db, event, proposal, family):
    from factory.orchestration.factory_landing_release import POLICY_TRANSITIONS

    control = db.get(FactoryControl, "factory")
    current = evidence(control)
    binding = proposal.get("binding", proposal)
    digest = binding["policy_sha256"]
    saved = proposal.get("landing_policy_evidence")
    transitions = db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action.in_(POLICY_TRANSITIONS), FactoryAudit.id > event.id)
        .order_by(FactoryAudit.id)
    ).all()
    # Existing grants remain valid without reinterpretation at their original
    # digest. Older configure journals can prove their original policy too.
    if saved is None:
        if digest == current["policy_sha256"]:
            saved = current
        else:
            previous = db.exec(
                select(FactoryAudit)
                .where(FactoryAudit.action == "configure", FactoryAudit.id < event.id)
                .order_by(FactoryAudit.id.desc())
            ).all()
            for audit in previous:
                detail = json.loads(audit.detail_json)
                policy = detail.get("policy")
                if (
                    detail.get("ok") is True
                    and isinstance(policy, dict)
                    and controls.digest(policy) == digest
                ):
                    saved = {
                        "schema_version": 1,
                        "policy_sha256": digest,
                        "intake_policy_sha256": controls.digest(
                            policy.get("intake") or {}
                        ),
                        "terms": {family: terms(policy, "enabled", family)},
                    }
                    break
    if (
        not isinstance(saved, dict)
        or type(saved.get("schema_version")) is not int
        or saved.get("schema_version") != 1
        or saved.get("policy_sha256") != digest
    ):
        return False
    original = saved["terms"][family]
    if original != current["terms"][family]:
        return False
    if family == "owner_pr_landing":
        if set(binding) != {
            "owner",
            "repo",
            "base_branch",
            "policy_sha256",
            "intake_policy_sha256",
            "configuration_audit_id",
        } or binding["intake_policy_sha256"] != saved.get("intake_policy_sha256"):
            return False
        from factory.orchestration.factory_landing_release import (
            latest_policy_transition,
        )

        prior = latest_policy_transition(db, before=event.id)
        if binding["configuration_audit_id"] != (prior.id if prior else None):
            return False
    last_digest = digest
    for audit in transitions:
        detail = json.loads(audit.detail_json)
        outcome = detail.get("result", detail)
        if outcome.get("ok") is False:
            continue
        if (
            audit.action == "set_auto_merge"
            and outcome.get("ok") is True
            and outcome.get("changed") is False
        ):
            continue
        proof = detail.get("landing_policy_evidence")
        # A legacy full configure record is sufficient; partial legacy control
        # records, missing outcomes and malformed snapshots are not.
        if (
            proof is None
            and audit.action == "configure"
            and outcome.get("ok") is True
            and isinstance(detail.get("policy"), dict)
        ):
            proof = {
                "schema_version": 1,
                "policy_sha256": controls.digest(detail["policy"]),
                "terms": {family: terms(detail["policy"], "enabled", family)},
            }
        if (
            outcome.get("ok") is not True
            or not isinstance(proof, dict)
            or type(proof.get("schema_version")) is not int
            or proof.get("schema_version") != 1
            or proof.get("terms", {}).get(family) != original
            or not isinstance(proof.get("policy_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", proof["policy_sha256"]) is None
        ):
            return False
        last_digest = proof["policy_sha256"]
    return last_digest == current["policy_sha256"]


def previously_enabled(db, action):
    """A deliberately unconfigured family is not an intervention request."""
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(FactoryAudit.action == action)
    ).all():
        try:
            result = json.loads(raw)["result"]
            if result.get("ok") is False:
                continue
            if result.get("ok") is not True or type(result.get("enabled")) is not bool:
                return True
            if result.get("ok") is True and result.get("enabled") is True:
                return True
        except (AttributeError, KeyError, TypeError, ValueError):
            # A configured record of unknown shape cannot prove deliberate
            # non-configuration. Its refusal must remain visible too.
            return True
    return False
