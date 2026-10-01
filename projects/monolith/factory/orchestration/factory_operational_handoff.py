"""Hand outstanding live checks to a person without paging them.

A delivery task can find its repository scope already merged, with only live
or operational acceptance left (a production flag to enable, a check against
the hub). Building more would duplicate merged work, and enabling a production
flag stays a human decision, but that decision does not need a page: it needs
a record a person will pick up. With ``FACTORY_OPERATIONAL_HANDOFF_ENABLED``:

- the planner's ``repository_delivered`` gate is verified against GitHub (each
  named PR merged, in this repository, referencing the issue), the live checks
  move to a child issue labelled ``needs-human`` (never ``agent-ready``) or to
  an open issue the rescope already names, ``agent-ready`` comes off the
  original (closed as completed when the checks live elsewhere), one comment
  links it all, and the task settles ``cancelled`` with state
  ``repository_delivered``. No decision card, no Discord message: the
  ``repository_scope_delivered`` audit is what the daily digest lists.
- intake treats an issue with that audit as delivered, and so an issue whose
  earlier task recorded a ``live_validation`` rescope and whose factory PR has
  since merged, so a generation bump does not re-admit finished repository
  work (#6288, #5803, #5787, #5758, #5757, #5699, #4321 on 2026-09-30).
- a ``split`` decision marks an operational child ``needs-human`` so intake
  does not brief it at once (#6559, receipt 724).
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx
from sqlmodel import select

logger = logging.getLogger(__name__)

ENABLED_ENV = "FACTORY_OPERATIONAL_HANDOFF_ENABLED"
ACTOR = "factory:handoff"
DELIVERED = "repository_scope_delivered"
CHILD_CREATED = "operational_handoff_child_created"
REFUSED = "operational_handoff_refused"
HUMAN_LABEL = "needs-human"
READY_LABEL = "agent-ready"
# GitHub reads one intake sweep may spend proving earlier delivery.
INTAKE_READS_PER_SWEEP = 5
# A backstop for split children the brief did not mark: titles that name a
# live or operational step rather than repository work.
OPERATIONAL_TITLE = re.compile(
    r"\blive[- ]?(?:validat\w*|checks?|tests?)\b"
    r"|\boperational\s+(?:acceptance|checks?|validation)\b"
    r"|\b(?:validate|enable|flip|arm|roll\s*out)\b.*"
    r"\b(?:hub|gke|prod|production|cluster|pod|node|live)\b",
    re.IGNORECASE,
)


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "false").lower() == "true"


class HandoffRefused(ValueError):
    pass


def _github():
    from factory.orchestration.factory_conductor import github_get
    from factory.orchestration.factory_landing import github_write

    return github_get, github_write


def references_issue(text: object, repo: str, number: int) -> bool:
    """Whether a PR title or body names the issue as ``#N`` or by URL."""
    if not isinstance(text, str):
        return False
    url = rf"https?://github\.com/{re.escape(repo)}/issues/{number}\b"
    short = rf"(?<![\w/#])(?:{re.escape(repo)})?#{number}\b"
    return re.search(f"{url}|{short}", text, re.IGNORECASE) is not None


def merged_reference(pull: dict, repo: str, number: int) -> bool:
    """A merged PR in this repository that references the issue."""
    merged = pull.get("merged") is True or bool(pull.get("merged_at"))
    base_repo = ((pull.get("base") or {}).get("repo") or {}).get("full_name")
    return (
        merged
        and base_repo in (None, repo)
        and (
            references_issue(pull.get("body"), repo, number)
            or references_issue(pull.get("title"), repo, number)
        )
    )


def verify_delivered(repo: str, number: int, prs: list[int]) -> list[dict]:
    github_get, _write = _github()
    found = []
    for pr in prs:
        pull = github_get(repo, f"pulls/{pr}")
        if not merged_reference(pull, repo, number):
            raise HandoffRefused(
                f"PR #{pr} is not a merged pull request in {repo} referencing #{number}"
            )
        found.append({"number": pr, "merged_at": pull.get("merged_at")})
    return found


def _audits(db, task_id: str, action: str) -> list[dict]:
    from factory.orchestration.factory_models import FactoryAudit

    rows = db.exec(
        select(FactoryAudit.detail_json).where(
            FactoryAudit.task_id == task_id, FactoryAudit.action == action
        )
    ).all()
    return [json.loads(raw) for raw in rows]


def _audit(task_id: str, action: str, **detail) -> None:
    from factory.orchestration.factory_controls import _audit, _locked_session

    with _locked_session() as (db, _control):
        _audit(db, ACTOR, action, task_id=task_id, **detail)


def _remove_label(repo: str, number: int, name: str) -> None:
    _get, github_write = _github()
    try:
        github_write(repo, f"issues/{number}/labels/{name}", {}, method="DELETE")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise


def _checklist(checks: list[str]) -> str:
    return "\n".join(f"- [ ] {check}" for check in checks)


def _child(task: dict, gate: dict, delivered: list[dict]) -> int:
    """The child issue holding the live checks, created once per task."""
    from factory.orchestration.factory_controls import _read_session

    with _read_session() as db:
        done = _audits(db, task["id"], CHILD_CREATED)
    if done:
        return done[0]["child_number"]
    repo, number = task["repo"], task["issue_number"]
    prs = ", ".join(f"#{item['number']}" for item in delivered)
    title = (
        f"Live checks for #{number}: {task.get('title') or 'operational acceptance'}"
    )
    body = (
        f"The repository work for #{number} is merged ({prs}). These live checks "
        "remain. Enabling a production flag or running them is a person's "
        "decision, so this issue carries `needs-human` and the factory will not "
        "pick it up on its own.\n\n"
        f"{_checklist(gate['live_checks'])}\n\n"
        f"Why the factory stopped: {gate['reason']}\n\n"
        f"<!-- factory-handoff-child:{task['id']} -->"
    )
    _get, github_write = _github()
    created = github_write(
        repo,
        "issues",
        {"title": title[:256], "body": body, "labels": [HUMAN_LABEL]},
    )
    child = created.get("number") if isinstance(created, dict) else None
    if not isinstance(child, int):
        raise HandoffRefused("GitHub did not return a child issue number")
    _audit(task["id"], CHILD_CREATED, child_number=child, issue_number=number)
    return child


def handoff(task: dict, gate: dict, cause: str) -> bool:
    """Settle a delivery whose repository scope is already merged.

    Returns True once the task is handed off (or was already), so the caller
    raises no card. False keeps the existing decision card, with the reason
    audited: a PR that is not merged or does not reference the issue, or a
    named tracking issue that is not an open issue.
    """
    if not enabled():
        return False
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration.factory_controls import _read_session, finish_task

    repo, number, task_id = task["repo"], task["issue_number"], task["id"]
    with _read_session() as db:
        recorded = _audits(db, task_id, DELIVERED)
    try:
        delivered = verify_delivered(repo, number, gate["delivered_prs"])
        tracked = gate.get("tracked_in")
        if tracked is not None:
            github_get, _write = _github()
            issue = github_get(repo, f"issues/{tracked}")
            if (
                tracked == number
                or issue.get("state") != "open"
                or "pull_request" in issue
            ):
                raise HandoffRefused(f"#{tracked} is not an open issue to hand to")
    except HandoffRefused as exc:
        with _read_session() as db:
            refused = any(
                a.get("cause") == cause for a in _audits(db, task_id, REFUSED)
            )
        if not refused:
            _audit(task_id, REFUSED, cause=cause, reason=str(exc)[:500])
        return False
    child = tracked if tracked is not None else _child(task, gate, delivered)
    prs = ", ".join(f"#{item['number']}" for item in delivered)
    if tracked is not None:
        text = (
            f"The repository work for this issue is merged ({prs}). The remaining "
            f"live checks are tracked in #{child}, so this issue closes as "
            "delivered.\n\n"
            f"{_checklist(gate['live_checks'])}"
        )
    else:
        text = (
            f"The repository work for this issue is merged ({prs}). Only live "
            f"checks remain, and they now live in #{child} with `needs-human`: "
            "enabling a production flag stays a person's decision. "
            "`agent-ready` comes off so the factory does not build this again."
        )
    conductor._post_decision_card(
        repo, number, f"<!-- factory-handoff:{task_id} -->", text
    )
    _remove_label(repo, number, READY_LABEL)
    if tracked is not None:
        _get, github_write = _github()
        github_write(
            repo,
            f"issues/{number}",
            {"state": "closed", "state_reason": "completed"},
            method="PATCH",
        )
    if not recorded:
        _audit(
            task_id,
            DELIVERED,
            source="planner",
            cause=cause,
            issue_number=number,
            delivered_prs=[item["number"] for item in delivered],
            live_checks=gate["live_checks"],
            child_number=None if tracked is not None else child,
            tracked_in=tracked,
            closed=tracked is not None,
        )
    settled = finish_task(
        task_id,
        "cancelled",
        ACTOR,
        evidence={
            "state": "repository_delivered",
            "reason": (
                f"Repository scope merged in {prs}; live checks handed to #{child}."
            )[:1024],
        },
    )
    if not settled["ok"]:
        # Unresolved starts, most often. Every write above is fenced, so the
        # next tick reaches this again and settles.
        logger.info("factory handoff settlement deferred for %s", task_id)
    return True


# ---------------------------------------------------------------------------
# Intake


def _gated_prs(db, rows) -> list[int]:
    """Factory PRs of earlier tasks that recorded a live-validation rescope."""
    prs: list[int] = []
    for row in rows:
        if not row.task_id:
            continue
        gates = _audits(db, row.task_id, "conductor_gate_decided")
        if not any(
            (detail.get("gate") or {}).get("kind") == "live_validation"
            for detail in gates
        ):
            continue
        for detail in _audits(db, row.task_id, "factory_pr_settlement_complete"):
            number = detail.get("pr_number")
            if type(number) is int and number > 0 and number not in prs:
                prs.append(number)
        direction = json.loads(row.direction_json) if row.direction_json else {}
        number = direction.get("delivery_pr_number")
        if type(number) is int and number > 0 and number not in prs:
            prs.append(number)
    return prs


def intake_exclusion(repo: str, number: int, rows, budget: dict) -> str | None:
    """``repository_delivered``, ``delivery_check_deferred`` or None.

    The database answers first: a recorded hand-off, or an earlier sweep's
    proof. Otherwise each earlier factory PR behind a live-validation rescope
    costs one bounded read, at most ``INTAKE_READS_PER_SWEEP`` per sweep. A
    candidate the budget or a failed read leaves unproven waits for the next
    sweep rather than being admitted on a guess.
    """
    from factory.orchestration.factory_controls import _read_session

    task_ids = [row.task_id for row in rows if row.task_id]
    if not task_ids:
        return None
    with _read_session() as db:
        if any(_audits(db, task_id, DELIVERED) for task_id in task_ids):
            return "repository_delivered"
        prs = _gated_prs(db, rows)
    if not prs:
        return None
    github_get, _write = _github()
    for pr in prs:
        if budget.get("reads", 0) <= 0:
            return "delivery_check_deferred"
        budget["reads"] -= 1
        try:
            pull = github_get(repo, f"pulls/{pr}")
        except Exception:  # noqa: BLE001 - unproven, not admitted
            logger.warning("factory intake delivery check failed for #%s", number)
            return "delivery_check_deferred"
        if merged_reference(pull, repo, number):
            latest = max(rows, key=lambda row: row.id)
            _audit(
                latest.task_id or task_ids[0],
                DELIVERED,
                source="intake",
                receipt_id=latest.id,
                issue_number=number,
                delivered_prs=[pr],
            )
            return "repository_delivered"
    return None


# ---------------------------------------------------------------------------
# Split children


def operational_child(child: dict) -> bool:
    """Whether a split child carries live or operational checks."""
    if child.get("operational") is True:
        return True
    return bool(OPERATIONAL_TITLE.search(str(child.get("title") or "")))
