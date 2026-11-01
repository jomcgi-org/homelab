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
"""Backstop for the proposed value; declared effects carry the operational claim."""

GATE_EFFECTS = (
    "spends_money",
    "deletes_data",
    "touches_external_account",
    "uses_credentials",
)

# Generated scope and checklist entries must each fit one Markdown line. Cover
# every splitlines separator, and reject HTML comments and leading whitespace.
_GATE_LINE_PATTERN = (
    r"^(?![\s\S]*(?:<!--|[\r\n\u000b\u000c\u001c-\u001e\u0085\u2028\u2029]))\S"
)

GATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "classification", "reason", "effects"],
    "properties": {
        "kind": {
            "enum": [
                "parameter",
                "live_validation",
                "delivery_target",
                "repository_delivered",
            ]
        },
        "classification": {
            "enum": ["reversible", "spending", "prod_deletion", "external_account"]
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
        "effects": {
            "type": "object",
            "additionalProperties": False,
            "required": list(GATE_EFFECTS),
            "properties": {effect: {"type": "boolean"} for effect in GATE_EFFECTS},
        },
        "value": {"type": "string", "minLength": 1, "maxLength": 1000},
        "scope": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000,
            "pattern": _GATE_LINE_PATTERN,
        },
        "live_checks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
                "pattern": _GATE_LINE_PATTERN,
            },
        },
        "delivered_prs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 1},
        },
        "tracked_in": {"type": "integer", "minimum": 1},
        "leave_unmerged": {"const": True},
    },
    "allOf": [
        {
            "if": {"required": ["leave_unmerged"]},
            "then": {"properties": {"kind": {"const": "live_validation"}}},
        },
        {
            "if": {"properties": {"kind": {"const": "repository_delivered"}}},
            "then": {"required": ["live_checks", "delivered_prs"]},
        },
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
    "When a decision would otherwise stop for a person, propose a `gate` with "
    "the would-be pause artifact so the server can continue it. Use kind "
    "`parameter` (`reversible`, concrete `value`) for a missing name, count, "
    "threshold or similar default. Use `live_validation` (`reversible`) with "
    "a repository-only default-off or staged `scope` when repository work can "
    "proceed but operational acceptance remains. Each `live_checks` entry is "
    "one check the factory can run after rollout and settle within 72 hours: "
    "a verify_deployment result, a status or audit read, a log query. Never a "
    "multi-day observation, a backlog triage or a person's judgement; the "
    "smallest live check is a verified rollout of the delivered revision. "
    "Carry it on ordinary plan and add_node decisions too. "
    "Use `delivery_target` to adopt an existing open PR for this issue, "
    "and `repository_delivered` when the repository scope is already merged "
    "and only live checks remain. Classify real spending, production "
    "deletion or external-account effects as such. Every gate declares "
    "`effects`, true for each effect that choosing or acting on it has, "
    "including credential use. Any true effect sends the gate to a person, "
    "and `reason` cannot override effects. Never weaken acceptance: state a "
    "rescope in the PR body and drop closing keywords while operational work "
    "remains. Set leave_unmerged: true only for explicitly unmerged delivery "
    "scope, not merely outstanding live checks. Existing holds remain binding; only the authorized landing owner "
    "may merge or deploy. "
)


def decisions(task: dict) -> list[dict]:
    return task.get("conductor_gates") or []


def live_checks(task: dict) -> list[str]:
    return list(
        dict.fromkeys(
            check for gate in decisions(task) for check in gate.get("live_checks", [])
        )
    )


def pending_live_validation(body: str | None) -> bool:
    """Explicit rescope prose is a closure veto, never authority to run checks."""
    return bool(
        re.search(
            r"(?im)^(?:Conductor rescope:|#{1,6}\s+(?:live validation|operational acceptance)"
            r"\b[^\n]*(?:outstanding|pending|not claimed))",
            body or "",
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
    if task.get("delivery_adoption") and not task.get("dependency_review"):
        parts.append(
            f"Adopted PR #{task['delivery_pr_number']} on {task['delivery_branch']}. "
            "Rebase onto main, repair, push to this branch and independently "
            "re-review the resulting head. Update this PR."
        )
    return "\n".join(parts)


_SCOPE_TAILS = (
    "Operational acceptance remains on the issue; do not close it",
    "Operational acceptance remains on the issue",
    "Deliver repository changes default-off or staged",
)


def _scope_sentence(scope: str) -> str:
    """The planner's scope without the server's own suffix echoed back into it.

    Planners copy the previous contract from the issue, suffix included, so
    the rendered line read "...do not close it.. Deliver repository changes
    default-off or staged. Operational acceptance remains on the issue; do
    not close it." twice over.
    """
    text = scope.strip()
    while True:
        text = text.rstrip(" .")
        for tail in _SCOPE_TAILS:
            if text.endswith(tail):
                text = text[: -len(tail)]
                break
        else:
            return text


def rescope_text(gate: dict) -> str:
    return (
        f"Conductor rescope: {_scope_sentence(gate['scope'])}. Deliver repository changes default-off "
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


def restricted(gate: dict) -> bool:
    """Fail closed on undeclared effects, and never infer authority from reason."""
    effects = gate.get("effects")
    if (
        not isinstance(effects, dict)
        or set(effects) != set(GATE_EFFECTS)
        or any(type(effects[effect]) is not bool for effect in GATE_EFFECTS)
    ):
        return True
    value = gate.get("value")
    return (
        gate.get("classification") != "reversible"
        or any(effects.values())
        or (
            value is not None
            and (not isinstance(value, str) or bool(HUMAN_GATE_BACKSTOP.search(value)))
        )
    )


def resolve(task: dict, artifact: dict, cause: str) -> bool:
    """True only after a typed reversible gate has been durably decided."""
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_landing import github_write

    raw = artifact.get("gate")
    if raw is None:
        # Legacy artifacts and unrelated human questions retain their path.
        return False
    gate = validate_gate(raw)
    # Declared operational effects restrict every kind before adoption or handoff.
    # The unchanged value backstop also covers optional values on other kinds.
    # Reasons can explain safety without requesting operational authority.
    if restricted(gate):
        return False
    if gate["kind"] == "repository_delivered":
        # Delivery tasks only. A refine that resolves a gate goes on to apply
        # agent-ready, which is the opposite of handing the checks off.
        if cause.startswith("refine-gate:"):
            return False
        from factory.orchestration import factory_operational_handoff

        return factory_operational_handoff.handoff(task, gate, cause)
    if gate["kind"] == "delivery_target":
        # This operation chooses only a server-discovered delivery branch.
        # Free-text proposals cannot authorize spending or external operations;
        # adoption checks repository identity and active ownership below.
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
    # Persist the acceptance contract before GitHub writes. A failed comment or
    # PR update must not leave landing believing there are no live checks.
    with _locked_session() as (db, _control):
        row = db.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task["id"])
        ).one()
        direction = json.loads(row.direction_json) if row.direction_json else {}
        saved = direction.setdefault("conductor_gates", [])
        if gate not in saved:
            saved.append(gate)
        if gate.get("leave_unmerged"):
            direction.setdefault(
                "landing_hold",
                {
                    "actor": "factory:conductor",
                    "cause": cause,
                    "reason": gate["reason"],
                },
            )
        row.direction_json = json.dumps(direction)
        row.updated_at = _now()
        db.add(row)
    task["conductor_gates"] = saved
    task["landing_hold"] = "landing_hold" in direction
    conductor._post_decision_card(repo, number, marker, text)
    if gate["kind"] == "live_validation":
        from factory.orchestration.factory_gate_blocks import render, rewrite

        issue = conductor.github_get(repo, f"issues/{number}")
        body = issue.get("body") or ""
        replacement = render(task["id"], identity, text)
        try:
            updated, _ = rewrite(body, replacement)
        except ValueError as exc:
            # The durable gate and card still carry acceptance. Ambiguous human
            # text only prevents this body write, never the PR rescope or audit.
            conductor._audit_once(
                task["id"],
                cause,
                "conductor_gate_issue_body_skipped",
                {"cause": cause, "issue_number": number, "reason": str(exc)},
            )
        else:
            if updated != body:
                github_write(
                    repo,
                    f"issues/{number}",
                    {"body": updated},
                    method="PATCH",
                )
    if gate["kind"] == "live_validation":
        rescope_pr(
            task, artifact.get("pr_number") or task.get("delivery_pr_number"), gate
        )
    with _locked_session() as (db, _control):
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
    # Prefer an already granted target while it remains an open delivery.
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
    """Find closing PRs and recorded deliveries whose staged scope stays open."""
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_controls import (
        _read_session,
        granted_delivery_surface,
    )

    # Rescoping intentionally removes closing keywords. Preserve the issue's
    # delivery association from the receipt, never from a free-text reference
    # in an unrelated PR or the planner's proposed branch.
    recorded_targets = set()
    with _read_session() as db:
        rows = db.exec(
            select(FactoryReceipt).where(
                FactoryReceipt.repo == repo,
                FactoryReceipt.issue_number == issue_number,
            )
        ).all()
        for row in rows:
            direction = json.loads(row.direction_json) if row.direction_json else {}
            branch, number = granted_delivery_surface(direction)
            if branch:
                recorded_targets.add((branch, number))
            elif row.task_id:
                recorded_targets.add((f"factory/{row.task_id}", None))

    # Keep the 500-row coverage bound while fitting large PR bodies beneath
    # the shared 1 MB response cap. A capped scan never proves absence.
    page_size = 25
    candidates = []
    for page in range(1, 21):
        pulls = conductor.github_list(
            repo,
            f"pulls?state=open&sort=created&direction=asc&per_page={page_size}&page={page}",
        )
        candidates.extend(
            pr
            for pr in pulls
            if (pr.get("head", {}).get("repo") or {}).get("full_name") == repo
            and (
                conductor.closes_issue(pr.get("body"), repo, issue_number)
                or (pr.get("head", {}).get("ref"), pr.get("number")) in recorded_targets
                or (pr.get("head", {}).get("ref"), None) in recorded_targets
            )
        )
        if len(pulls) < page_size:
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
