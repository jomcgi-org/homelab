"""A bounded, server-verified issue briefing path for refine receipts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os

from sqlmodel import select

from swarm.factory_controls import (
    REFINE,
    _audit,
    _locked_session,
    _read_session,
    finish_task,
    receipt_kind,
    set_control,
    task_snapshot,
)
from swarm.factory_models import FactoryAudit, FactoryReceipt
from swarm.factory_conductor import (
    _artifact,
    _bounded_planner_text,
    github_get,
    github_list,
)
from swarm.model_pool import select_model, selection_reason

logger = logging.getLogger(__name__)

ACTOR = "factory:refine"
NODE_KEY = "refine_1"
MAX_ATTEMPTS = 2
BRIEF_HEADING = "## Agent brief"
READY_LABEL = "agent-ready"
HUMAN_LABEL = "needs-human"

REFINE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["outcome", "comment_url"],
    "properties": {
        "outcome": {"enum": [READY_LABEL, HUMAN_LABEL]},
        "comment_url": {"type": "string", "minLength": 1, "maxLength": 512},
        "question": {"type": "string", "maxLength": 4000},
    },
}


def refine_prompt(task: dict, receipt: dict) -> str:
    body = _bounded_planner_text(receipt.get("body") or "", 12000)
    return (
        f"Brief repository {task['repo']} issue #{receipt['issue_number']} at "
        f"{receipt['url']}.\n\n"
        f"Title: {receipt['title']}\n\nIssue body:\n{body}\n\n"
        "Read the issue and the repository. Post EXACTLY ONE issue comment whose "
        f"first line is `{BRIEF_HEADING}`. Include `### Outcome`, "
        "`### Acceptance`, `### Files`, `### Evidence`, and `### Risks` in that "
        "order. Apply the `agent-ready` label when the brief is actionable with no "
        "human decision left. Otherwise apply `needs-human` and make the LAST "
        "section `### Question`, containing the single specific question a human "
        "must answer. Do not edit the issue title or body. Do not create or push a "
        "branch, open a pull request, close the issue, or apply any label other "
        "than the one you chose. Use `gh issue comment` and "
        '`gh issue edit --add-label`. `gh auth status` reporting "not logged in" '
        "is expected and is not a problem. Return the typed artifact "
        '`{"outcome": ..., "comment_url": ..., "question": ...}`. '
        "`question` is required for `needs-human` and must be omitted otherwise."
    )


def is_refine_task(task_id: str) -> bool:
    with _read_session() as db:
        row = db.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task_id)
        ).first()
        return row is not None and receipt_kind(row) == REFINE


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _mismatch(task_id: str, number: int, reason: str, label_present: bool) -> None:
    bounded = f"issue {number}: {reason}; label_present={label_present}"[:500]
    with _locked_session() as (db, _control):
        _audit(
            db,
            ACTOR,
            "refine_mismatch",
            task_id=task_id,
            issue_number=number,
            reason=bounded,
            label_present=label_present,
        )
    finish_task(
        task_id,
        "failed",
        ACTOR,
        evidence={"state": "refine_unverified", "reason": bounded},
    )


def _notify_once(task_id: str, repo: str, number: int, question: str) -> None:
    with _locked_session() as (db, _control):
        existing = db.exec(
            select(FactoryAudit.id).where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action == "refine_needs_human_notified",
            )
        ).first()
        if existing is not None:
            return
        _audit(
            db,
            ACTOR,
            "refine_needs_human_notified",
            task_id=task_id,
            issue_number=number,
        )
    try:
        from agent.notify import notify

        asyncio.run(
            notify(
                f"Factory refine needs a human on {repo}#{number}: {question[:500]}",
                level="warn",
            )
        )
    except Exception:  # noqa: BLE001 - notification is best effort
        logger.warning("factory refine human notification failed", exc_info=True)
        with _locked_session() as (db, _control):
            _audit(
                db,
                ACTOR,
                "refine_notify_failed",
                task_id=task_id,
                issue_number=number,
            )


def _settle(task: dict, run: dict) -> None:
    receipt = task_snapshot(task["id"])
    number = receipt["issue_number"]
    repo = task["repo"]
    artifact = _artifact(run)
    outcome = artifact.get("outcome") if isinstance(artifact, dict) else None
    if outcome not in (READY_LABEL, HUMAN_LABEL):
        _mismatch(task["id"], number, "artifact outcome is invalid", False)
        return
    question = artifact.get("question")
    if (outcome == HUMAN_LABEL and not isinstance(question, str)) or (
        outcome == READY_LABEL and question is not None
    ):
        _mismatch(task["id"], number, "artifact question does not match outcome", False)
        return
    issue = github_get(repo, f"issues/{number}")
    issue_labels = {
        str(label.get("name", "")).lower()
        for label in issue.get("labels") or []
        if isinstance(label, dict)
    }
    label_present = outcome.lower() in issue_labels
    if not label_present:
        _mismatch(task["id"], number, "claimed outcome label is absent", False)
        return
    comments = github_list(repo, f"issues/{number}/comments?per_page=100")
    admitted = datetime.fromisoformat(receipt["admitted_at"].replace("Z", "+00:00"))
    if admitted.tzinfo is None:
        admitted = admitted.replace(tzinfo=timezone.utc)
    executor = os.environ.get("FACTORY_EXECUTOR_LOGIN", "").strip().lower()
    matching = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        created = comment.get("created_at")
        if not isinstance(body, str) or not body.lstrip().startswith(BRIEF_HEADING):
            continue
        if not isinstance(created, str):
            continue
        try:
            created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if _aware(created_at) < admitted:
            continue
        login = (comment.get("user") or {}).get("login")
        if executor and (not isinstance(login, str) or login.lower() != executor):
            continue
        matching.append(comment)
    comment_url = artifact.get("comment_url")
    match = next(
        (
            comment
            for comment in matching
            if isinstance(comment_url, str) and comment.get("html_url") == comment_url
        ),
        None,
    )
    if match is None:
        reason = (
            "comment page is full and has no matching brief"
            if len(comments) >= 100 and not matching
            else "no matching verified brief comment"
        )
        _mismatch(task["id"], number, reason, label_present)
        return
    if outcome == HUMAN_LABEL:
        if not question.strip():
            _mismatch(task["id"], number, "needs-human question is empty", True)
            return
        _notify_once(task["id"], repo, number, question.strip())
    # A needs-human outcome succeeds because the refine task produced a verified
    # brief and escalated the one decision that remains.
    state = "refine_agent_ready" if outcome == READY_LABEL else "refine_needs_human"
    finish_task(
        task["id"],
        "succeeded",
        ACTOR,
        evidence={"state": state, "reason": match["html_url"]},
    )
    with _locked_session() as (db, _control):
        _audit(
            db,
            ACTOR,
            "refine_settled",
            task_id=task["id"],
            outcome=outcome,
            comment_url=match["html_url"],
        )


def reconcile(
    task: dict,
    policy: dict,
    nodes: list[dict],
    runs: list[dict],
    expected_version: int,
) -> None:
    from swarm import factory_conductor

    if not nodes:
        receipt = task_snapshot(task["id"])
        choice = select_model("conductor", policy)
        cause = f"factory-refine:{NODE_KEY}"
        result = factory_conductor._add(
            task,
            policy,
            NODE_KEY,
            refine_prompt(task, receipt),
            [],
            choice["model"],
            cause,
            selection_reason("Brief the issue for autonomous delivery", choice),
            max_attempts=MAX_ATTEMPTS,
            expected_version=expected_version,
            refine=True,
        )
        if result.ok:
            factory_conductor._record_allowance(task["id"], policy, cause)
        else:
            set_control("pause_task", ACTOR, task_id=task["id"])
        return
    succeeded = next(
        (
            run
            for run in runs
            if run["node_key"] == NODE_KEY and run["status"] == "succeeded"
        ),
        None,
    )
    if succeeded is not None:
        _settle(task, succeeded)
        return
    node = next((node for node in nodes if node["node_key"] == NODE_KEY), None)
    attempts = [run for run in runs if run["node_key"] == NODE_KEY]
    if (
        node is not None
        and len(attempts) >= node["max_attempts"]
        and all(
            run["status"] in factory_conductor.graph.TERMINAL_RUN_STATUSES
            for run in attempts
        )
    ):
        finish_task(
            task["id"],
            "failed",
            ACTOR,
            evidence={
                "state": "refine_failed",
                "reason": "Two refine attempts did not produce a verified brief.",
            },
        )
        with _locked_session() as (db, _control):
            _audit(db, ACTOR, "refine_failed", task_id=task["id"])
