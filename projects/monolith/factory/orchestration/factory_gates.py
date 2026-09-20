"""Conductor-owned reversible decisions, persisted before work continues."""

from __future__ import annotations

import hashlib
import json
import logging
import re

from sqlmodel import select

from factory.orchestration.factory_controls import _audit, _locked_session, _now
from factory.orchestration.factory_models import FactoryReceipt

logger = logging.getLogger(__name__)

HUMAN_GATE_BACKSTOP = re.compile(
    r"(\$|\busd\b|budget|spend|cost|delete|purge|\bprod\b|bucket|create|credential|token|secret|account)",
    re.IGNORECASE,
)
"""Heuristic backstop for human authority, not the classification."""

GATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "classification", "reason"],
    "properties": {
        "kind": {"enum": ["parameter", "live_validation", "delivery_target"]},
        "classification": {
            "enum": ["reversible", "spending", "prod_deletion", "external_account"]
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
        "value": {"type": "string", "minLength": 1, "maxLength": 1000},
        "scope": {"type": "string", "minLength": 1, "maxLength": 2000},
        "live_checks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
    },
    "allOf": [
        {
            "if": {"properties": {"kind": {"const": "parameter"}}},
            "then": {"required": ["value"]},
        },
        {
            "if": {"properties": {"kind": {"const": "live_validation"}}},
            "then": {"required": ["scope", "live_checks"]},
        },
    ],
}

GATE_PROMPT = (
    "Conductor policy for reversible gates: classify a missing name, retention "
    "count, threshold, schedule or similar parameter in `gate` with kind "
    "`parameter`, classification `reversible`, a concrete proposed `value`, and "
    "`reason`. The server records and chooses that default; no person is needed. "
    "Use classification `spending`, `prod_deletion`, or `external_account` only "
    "when choosing the value itself spends money, deletes production data, or "
    "touches an external account. Separate safe repository defaults from those "
    "operations. For acceptance requiring live validation this runner cannot "
    "perform, use kind `live_validation`, classification `reversible`, a "
    "repository-only default-off or staged `scope`, and `live_checks` naming "
    "the outstanding operational acceptance. The server appends that checklist "
    "to the issue. Never ask whether to weaken acceptance. State the rescope "
    "in the PR body; remove closing keywords while operational work remains. "
    "Required Linux CI and independent exact-head review still apply. For an "
    "existing open PR closing this issue, use kind `delivery_target`: the "
    "conductor adopts its branch, rebases onto main, repairs and re-reviews the "
    "same PR. A head outside `factory/` or a branch owned by another running "
    "task needs a person. "
    "Return the gate with the would-be needs-human/escalate/pause artifact, "
    "including the proposed default or scope, so the server can continue it. "
)


def decisions(task: dict) -> list[dict]:
    return task.get("conductor_gates") or []


def live_checks(task: dict) -> list[str]:
    return list(
        dict.fromkeys(
            check for gate in decisions(task) for check in gate.get("live_checks", [])
        )
    )


def guidance(task: dict) -> str:
    parts = []
    for gate in decisions(task):
        if gate["kind"] == "parameter":
            parts.append(
                f"Decided by the conductor: {gate['value']}, because {gate['reason']}; reversible"
            )
        elif gate["kind"] == "live_validation":
            parts.append(rescope_text(gate))
    if task.get("delivery_adoption"):
        parts.append(
            f"Adopted PR #{task['delivery_pr_number']} on {task['delivery_branch']}. "
            "Rebase onto main, repair, push to this branch and independently "
            "re-review the resulting head. Update this PR."
        )
    return "\n".join(parts)


def rescope_text(gate: dict) -> str:
    return (
        f"Conductor rescope: {gate['scope']}. Deliver repository changes default-off "
        "or staged. Operational acceptance remains on the issue; do not close it.\n"
        + "\n".join(f"- [ ] {check}" for check in gate["live_checks"])
    )


def validate_gate(gate: object) -> dict:
    # Artifacts are untrusted even if an upstream schema was supplied.
    import jsonschema

    try:
        jsonschema.validate(gate, GATE_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ValueError(f"invalid gate: {exc.message}") from exc
    return gate


def resolve(task: dict, artifact: dict, cause: str) -> bool:
    """True only after a typed reversible gate has been durably decided."""
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_landing import github_write

    raw = artifact.get("gate")
    if raw is None:
        # Legacy artifacts and unrelated human questions retain their path.
        return False
    gate = validate_gate(raw)
    if any(
        HUMAN_GATE_BACKSTOP.search(gate.get(field, "")) for field in ("value", "reason")
    ):
        return False
    if gate["classification"] != "reversible":
        return False
    if gate["kind"] == "delivery_target":
        task["delivery_target_checked"] = False
        adopted = adopt_delivery(task, cause=cause, refresh=True)
        adopted = adopted and bool(task.get("delivery_adoption"))
        if adopted:
            conductor._audit_once(
                task["id"],
                cause,
                "conductor_gate_decided",
                {"cause": cause, "gate": gate},
            )
        return adopted
    number, repo = task["issue_number"], task["repo"]
    identity = hashlib.sha256(json.dumps(gate, sort_keys=True).encode()).hexdigest()[
        :20
    ]
    marker = f"<!-- factory-gate:{task['id']}:{identity} -->"
    text = (
        f"Decided by the conductor: {gate['value']}, because {gate['reason']}; reversible"
        if gate["kind"] == "parameter"
        else rescope_text(gate)
    )
    conductor._post_decision_card(repo, number, marker, text)
    if gate["kind"] == "live_validation":
        issue = conductor.github_get(repo, f"issues/{number}")
        body = issue.get("body") or ""
        if marker not in body:
            github_write(
                repo,
                f"issues/{number}",
                {"body": body + "\n\n" + marker + "\n" + text},
                method="PATCH",
            )
    if gate["kind"] == "live_validation":
        rescope_pr(
            task, artifact.get("pr_number") or task.get("delivery_pr_number"), gate
        )
    with _locked_session() as (db, _control):
        row = db.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task["id"])
        ).one()
        direction = json.loads(row.direction_json) if row.direction_json else {}
        saved = direction.setdefault("conductor_gates", [])
        if gate not in saved:
            saved.append(gate)
        row.direction_json = json.dumps(direction)
        row.updated_at = _now()
        db.add(row)
        from factory.orchestration.factory_models import FactoryAudit

        prior = db.exec(
            select(FactoryAudit.detail_json).where(
                FactoryAudit.task_id == task["id"],
                FactoryAudit.action == "conductor_gate_decided",
            )
        ).all()
        if not any(json.loads(raw).get("cause") == cause for raw in prior):
            _audit(
                db,
                "factory:conductor",
                "conductor_gate_decided",
                task_id=task["id"],
                cause=cause,
                gate=gate,
            )
    task["conductor_gates"] = saved
    return True


def rescope_pr(task: dict, number: int | None, gate: dict) -> None:
    """Keep a retained operational issue open when an existing PR is delivered."""
    if not number:
        return
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_landing import github_write

    pr = conductor.github_get(task["repo"], f"pulls/{number}")
    if (
        pr.get("state") != "open"
        or pr.get("head", {}).get("ref") != conductor.delivery_branch(task)
        or (pr.get("head", {}).get("repo") or {}).get("full_name") != task["repo"]
    ):
        logger.warning(
            "rescope PR %s does not match the task %s delivery target",
            number,
            task["id"],
        )
        return
    body = pr.get("body") or ""
    repo, issue = task["repo"], task["issue_number"]
    reference = rf"(?:(?:https?://github\.com/{re.escape(repo)}/issues/)|(?:{re.escape(repo)})?#){issue}\b"
    pattern = rf"(?<![A-Za-z0-9_]){conductor._CLOSE_KEYWORD}\s*:?\s+({reference})"
    updated = re.sub(pattern, r"Refs \1", body, flags=re.IGNORECASE)
    text = rescope_text(gate)
    if text not in updated:
        updated += "\n\n" + text
    if body != updated:
        github_write(repo, f"pulls/{number}", {"body": updated}, method="PATCH")


def adopt_delivery(
    task: dict, *, cause: str = "delivery-admission", refresh: bool = False
) -> bool:
    """Choose the oldest matching open PR before any node can own a branch.

    GitHub reads happen outside the control lock. The branch ownership check
    and receipt update share the admission lock, including operator admissions.
    An incomplete listing never authorizes a competing delivery.
    """
    from factory.orchestration.factory_controls import (
        delivery_branch_owner,
        validate_pr_branch,
    )

    if task.get("delivery_target_checked") and not refresh:
        return True
    candidates = matching_delivery_pulls(task["repo"], task["issue_number"])
    # Prefer an already granted target when it still closes the issue.
    candidates.sort(
        key=lambda pr: (pr["number"] != task.get("delivery_pr_number"), pr["number"])
    )
    pr = candidates[0] if candidates else None
    if pr:
        if not pr["head"]["ref"].startswith("factory/"):
            task["delivery_owner"] = (pr.get("user") or {}).get("login") or "a person"
            task["conflicting_pr"] = pr["number"]
            return False
        branch = validate_pr_branch(pr["head"]["ref"])
        if branch == task["base_branch"]:
            raise ValueError(
                "existing delivery PR has an unsafe head repository or branch"
            )
    with _locked_session() as (db, _control):
        row = db.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task["id"])
        ).one()
        direction = json.loads(row.direction_json) if row.direction_json else {}
        if direction.get("delivery_target_checked") and not refresh:
            task.update(direction)
            return True
        if pr:
            owner = delivery_branch_owner(
                db, row.repo, branch, exclude_receipt_id=row.id
            )
            if owner:
                task["delivery_owner"] = owner
                task["conflicting_pr"] = pr["number"]
                return False
            direction.update(
                delivery_branch=branch,
                delivery_pr_number=pr["number"],
                delivery_adoption=True,
            )
        direction["delivery_target_checked"] = True
        row.direction_json = json.dumps(direction)
        row.updated_at = _now()
        db.add(row)
        _audit(
            db,
            "factory:conductor",
            "delivery_target_adopted" if pr else "delivery_target_checked",
            task_id=task["id"],
            cause=cause,
            pr_number=pr["number"] if pr else None,
            branch=branch if pr else None,
        )
    task.update(direction)
    return True


def matching_delivery_pulls(repo: str, issue_number: int) -> list[dict]:
    """Oldest-first open same-repository PRs that close one issue."""
    from factory.orchestration import factory_conductor as conductor

    candidates = []
    for page in range(1, 6):
        pulls = conductor.github_list(
            repo,
            f"pulls?state=open&sort=created&direction=asc&per_page=100&page={page}",
        )
        candidates.extend(
            pr
            for pr in pulls
            if (pr.get("head", {}).get("repo") or {}).get("full_name") == repo
            and conductor.closes_issue(pr.get("body"), repo, issue_number)
        )
        if len(pulls) < 100:
            return sorted(candidates, key=lambda pr: pr["number"])
    raise ValueError("open PR discovery incomplete")


def receive_delivery_target(repo: str, issue_number: int) -> dict | None:
    """A safe linked PR grant for an operator or allowlist receipt.

    A person's branch and a branch held by another running task are not grants.
    They retain the ordinary first-reconcile conflict path, which can name the
    owner without letting receipt creation steal its delivery surface.
    """
    from factory.orchestration.factory_controls import (
        _read_session,
        delivery_branch_owner,
        validate_pr_branch,
    )

    candidates = matching_delivery_pulls(repo, issue_number)
    if not candidates:
        return None
    pull = candidates[0]
    branch = (pull.get("head") or {}).get("ref")
    if not isinstance(branch, str) or not branch.startswith("factory/"):
        return None
    branch = validate_pr_branch(branch)
    number = pull.get("number")
    if type(number) is not int or number <= 0:
        raise ValueError("existing delivery PR has an invalid number")
    with _read_session() as db:
        if delivery_branch_owner(db, repo, branch) is not None:
            return None
    return {
        "delivery_branch": branch,
        "delivery_pr_number": number,
        "delivery_adoption": True,
    }
