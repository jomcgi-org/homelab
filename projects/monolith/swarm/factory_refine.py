"""A bounded, server-verified issue briefing path for refine receipts."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
import logging
import os
from urllib.parse import quote

from sqlmodel import select

from swarm.factory_controls import (
    DEFAULT_TASK_CLASS,
    intake_policy,
    _audit,
    _locked_session,
    _read_session,
    finish_task,
    receipt_task_class,
    set_control,
    task_snapshot,
)
from swarm.factory_models import FactoryAudit, FactoryReceipt
from swarm.factory_conductor import (
    _artifact,
    _audit_once,
    _bounded_planner_text,
    github_get,
    github_list,
)
from swarm.model_pool import select_model, selection_reason

logger = logging.getLogger(__name__)

ACTOR = "factory:refine"
NODE_KEY = "refine_1"
MAX_ATTEMPTS = 2
# Comment pages read per settlement. The read is already narrowed by a
# since= filter, so this only bounds a thread that is busy after the brief
# was posted rather than the whole history before it.
COMMENT_PAGE_SIZE = 100
MAX_COMMENT_PAGES = 5
TASK_CLASS = "refine"
BRIEF_HEADING = "## Agent brief"
READY_LABEL = "agent-ready"
HUMAN_LABEL = "needs-human"
REJECT_OUTCOME = "reject"
STALE_OUTCOME = "stale"
# The two verdicts that close an issue. They are the only refine outcomes that
# remove something a person would have to restore by hand, which is why they
# sit behind their own flag and their own daily cap.
CLOSING_OUTCOMES = (REJECT_OUTCOME, STALE_OUTCOME)
OUTCOMES = (READY_LABEL, HUMAN_LABEL, *CLOSING_OUTCOMES)
# The label each verdict leaves on the issue, and so the label settlement
# demands back from GitHub before it believes the verdict.
OUTCOME_LABEL = {
    READY_LABEL: "agent-ready",
    HUMAN_LABEL: "needs-human",
    REJECT_OUTCOME: "wontfix",
    STALE_OUTCOME: "stale",
}
RECOMMENDATIONS = ("deliver", "close", "split", "defer")
# An issue carrying one of these, or any milestone, is never closed by the
# lane. Triage on work someone has already prioritised or flagged as a
# security finding is a judgment call that belongs to a person.
PROTECTED_LABELS = ("critical", "security-finding")

REFINE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["outcome", "comment_url"],
    "properties": {
        "outcome": {"enum": list(OUTCOMES)},
        "comment_url": {"type": "string", "minLength": 1, "maxLength": 512},
        "question": {"type": "string", "maxLength": 4000},
        "recommendation": {"enum": list(RECOMMENDATIONS)},
        "evidence": {"type": "string", "maxLength": 2000},
    },
}


def refine_prompt(task: dict, receipt: dict, *, closing: bool) -> str:
    """The brief the guest writes, and the four verdicts it may reach.

    ``closing`` says whether the lane may close an issue at all right now. It
    is false whenever the policy flag is off or the daily close cap is spent,
    and the prompt then offers three outcomes rather than four, so the guest
    is never asked to take an action the server would refuse.
    """
    body = _bounded_planner_text(receipt.get("body") or "", 12000)
    triage = (
        "`reject` when the issue clearly should not be done: it contradicts a "
        "recorded decision in an ARCHITECTURE.md **Why.** paragraph, it "
        "duplicates work already merged or closed, or its value is low for a "
        "reason you can state. Add a `### Why not` section citing the "
        "evidence by file, pull request number, or decision, apply `wontfix`, "
        "and close the issue with reason `not_planned`.\n"
        "`stale` when the premise no longer holds: the file, flag, or service "
        "it names is gone, the pull request it waited on merged, or the "
        "symptom no longer reproduces and you can show that. Add a "
        "`### Why stale` section, apply the `stale` label, creating it if the "
        "repository has none, and close the issue with reason `not_planned`.\n"
        if closing
        else "Closing is switched off for this run, so `reject` and `stale` "
        "are not available. An issue you would have closed is `needs-human` "
        "with `recommend: close` and the reason you would have cited.\n"
    )
    return (
        f"Brief repository {task['repo']} issue #{receipt['issue_number']} at "
        f"{receipt['url']}.\n\n"
        f"Title: {receipt['title']}\n\nIssue body:\n{body}\n\n"
        "Read the issue and the repository, then post EXACTLY ONE issue comment "
        f"whose first line is `{BRIEF_HEADING}`. Include `### Outcome`, "
        "`### Acceptance`, `### Files`, `### Evidence`, and `### Risks` in that "
        "order.\n\n"
        "Reach exactly one of these verdicts and act on it.\n"
        "`agent-ready` when the brief is actionable with no human decision "
        "left. Apply the `agent-ready` label.\n"
        "`needs-human` when value, scope, or staleness is unclear. Apply the "
        "`needs-human` label and make the LAST section `### Decision needed`, "
        "holding one line of the form `recommend: deliver` (or `close`, "
        "`split`, `defer`) followed by the single specific question a person "
        "must answer.\n"
        + triage
        + "\nThe bar for closing is evidence a reader can check, not a "
        "judgement you formed. If you are in any doubt, choose `needs-human` "
        "with a recommendation instead of closing: a wrong escalation costs "
        "someone a minute, and a wrong close costs them the issue. Never "
        "close an issue carrying `critical` or `security-finding`, or one "
        "assigned to a milestone; those can only be `agent-ready` or "
        "`needs-human`.\n\n"
        "Do not edit the issue title or body. Do not create or push a branch "
        "or open a pull request. Apply no label other than the one your "
        "verdict names. Use `gh issue comment`, `gh issue edit --add-label`, "
        "`gh label create` and `gh issue close --reason not_planned`. "
        '`gh auth status` reporting "not logged in" is expected and is not a '
        "problem.\n\n"
        "Return the typed artifact. `question` and `recommendation` are "
        "required for `needs-human` and omitted otherwise. `evidence` is "
        "required for `reject` and `stale`, and is the same citation your "
        "brief section gives."
    )


def closes_today() -> int:
    """Issues this lane has closed in the last 24 hours, from the audit trail."""
    from datetime import timedelta

    from swarm.factory_controls import _now

    cutoff = _now() - timedelta(hours=24)
    with _read_session() as db:
        return len(
            db.exec(
                select(FactoryAudit.id).where(
                    FactoryAudit.action == "intake_closed",
                    FactoryAudit.created_at >= cutoff,
                )
            ).all()
        )


def closing_allowed(policy: dict) -> tuple[bool, str]:
    """Whether a close verdict may be acted on, and why not when it may not.

    Read twice: once to shape the prompt, so the guest is never offered an
    action the server would refuse, and once at settlement, so a cap spent
    while the node was running still holds.
    """
    intake = intake_policy(policy)
    if not intake["close_enabled"]:
        return False, "close_disabled"
    if closes_today() >= intake["max_closes_per_day"]:
        return False, "close_cap"
    return True, "allowed"


def task_class_for(task_id: str) -> str:
    """The receipt's class, defaulting for a receipt that predates them."""
    with _read_session() as db:
        row = db.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task_id)
        ).first()
        return DEFAULT_TASK_CLASS if row is None else receipt_task_class(row)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _recorded_mismatch(task_id: str) -> str | None:
    """The stored verdict when this task has already failed verification.

    Settlement can be refused while a start is unresolved, so the reconciler
    reaches this branch again on the next tick. The verdict is a fact about
    GitHub that was established once, so a later tick retries the settlement
    and never re-reads the issue to re-derive an answer it already has.
    """
    with _read_session() as db:
        row = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action == "refine_mismatch",
            )
            .order_by(FactoryAudit.id.desc())
        ).first()
        return None if row is None else json.loads(row.detail_json).get("reason")


def _finish_unverified(task_id: str, reason: str) -> None:
    result = finish_task(
        task_id,
        "failed",
        ACTOR,
        evidence={"state": "refine_unverified", "reason": reason},
    )
    if not result["ok"]:
        # An unresolved start blocks settlement until the reconciler charges
        # it. The verdict is already on record, so the next tick retries this
        # without another GitHub read.
        logger.info(
            "factory refine settlement deferred for %s: %s",
            task_id,
            result["reason"],
        )


def _mismatch(task_id: str, number: int, reason: str, label_present: bool) -> None:
    bounded = f"issue {number}: {reason}; label_present={label_present}"[:500]
    _audit_once(
        task_id,
        f"factory-refine:{task_id}",
        "refine_mismatch",
        {"issue_number": number, "reason": bounded, "label_present": label_present},
    )
    _finish_unverified(task_id, bounded)


def _comments_since(repo: str, number: int, admitted: datetime) -> list:
    """Issue comments posted at or after admission, paged.

    Reading only the first page would miss the brief entirely on a busy issue,
    and the settlement would then fail a task whose node did exactly what it
    was asked. The since filter is what keeps that bounded: the only comments
    that can carry this task's brief are the ones written after it started.
    """
    stamp = quote(
        admitted.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), safe=""
    )
    collected: list = []
    for page in range(1, MAX_COMMENT_PAGES + 1):
        rows = github_list(
            repo,
            f"issues/{number}/comments?per_page={COMMENT_PAGE_SIZE}"
            f"&since={stamp}&page={page}",
        )
        collected.extend(rows)
        if len(rows) < COMMENT_PAGE_SIZE:
            break
    return collected


def _notify_once(
    task_id: str, repo: str, number: int, question: str, recommendation: str
) -> None:
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
                f"Factory refine needs a human on {repo}#{number}"
                f" (recommend: {recommendation}): {question[:500]}",
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


def _verify_artifact(artifact: dict, outcome: str) -> str | None:
    """The fields each verdict must carry, or the reason it does not."""
    question = artifact.get("question")
    recommendation = artifact.get("recommendation")
    evidence = artifact.get("evidence")
    if outcome == HUMAN_LABEL:
        if not isinstance(question, str) or not question.strip():
            return "needs-human carries no question"
        if recommendation not in RECOMMENDATIONS:
            return "needs-human carries no recommendation"
        return None
    if outcome in CLOSING_OUTCOMES:
        if not isinstance(evidence, str) or not evidence.strip():
            return f"{outcome} cites no evidence"
        return None
    if question is not None or recommendation is not None:
        return "agent-ready carries a question or recommendation"
    return None


def _brief_comment(repo: str, number: int, admitted: datetime, claimed) -> tuple:
    """The brief this task's node posted, and how many comments were read."""
    comments = _comments_since(repo, number, admitted)
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
    match = next(
        (
            comment
            for comment in matching
            if isinstance(claimed, str) and comment.get("html_url") == claimed
        ),
        None,
    )
    return match, len(comments), bool(matching)


def _settle(task: dict, run: dict, policy: dict) -> None:
    recorded = _recorded_mismatch(task["id"])
    if recorded is not None:
        # The verdict is already established. Retry only the settlement, so a
        # task waiting on an unresolved start costs no GitHub reads per tick.
        _finish_unverified(task["id"], recorded)
        return
    receipt = task_snapshot(task["id"])
    number = receipt["issue_number"]
    repo = task["repo"]
    artifact = _artifact(run)
    if not isinstance(artifact, dict):
        artifact = {}
    outcome = artifact.get("outcome")
    if outcome not in OUTCOMES:
        _mismatch(task["id"], number, "artifact outcome is invalid", False)
        return
    invalid = _verify_artifact(artifact, outcome)
    if invalid is not None:
        _mismatch(task["id"], number, invalid, False)
        return

    issue = github_get(repo, f"issues/{number}")
    issue_labels = {
        str(label.get("name", "")).lower()
        for label in issue.get("labels") or []
        if isinstance(label, dict)
    }
    protected = bool(issue_labels & set(PROTECTED_LABELS)) or bool(
        issue.get("milestone")
    )
    # A close verdict is downgraded rather than refused when the lane may not
    # close right now. The guest was told the same thing before it ran, so a
    # correct node has already taken the needs-human path and this is the
    # server refusing to be talked into the close by the artifact alone.
    effective, downgrade = outcome, None
    if outcome in CLOSING_OUTCOMES:
        allowed, why = closing_allowed(policy)
        if protected:
            effective, downgrade = HUMAN_LABEL, "protected_issue"
        elif not allowed:
            effective, downgrade = HUMAN_LABEL, why

    expected = OUTCOME_LABEL[effective]
    label_present = expected in issue_labels
    if not label_present:
        _mismatch(task["id"], number, f"the {expected} label is absent", False)
        return
    closed = issue.get("state") == "closed"
    if effective in CLOSING_OUTCOMES and not closed:
        _mismatch(task["id"], number, f"{effective} left the issue open", True)
        return
    if effective not in CLOSING_OUTCOMES and closed:
        _mismatch(task["id"], number, f"{effective} left the issue closed", True)
        return

    admitted = datetime.fromisoformat(receipt["admitted_at"].replace("Z", "+00:00"))
    if admitted.tzinfo is None:
        admitted = admitted.replace(tzinfo=timezone.utc)
    match, read, any_brief = _brief_comment(
        repo, number, admitted, artifact.get("comment_url")
    )
    if match is None:
        reason = (
            f"no matching verified brief in {read} comments since admission"
            if not any_brief
            else "no brief comment matches the claimed comment url"
        )
        _mismatch(task["id"], number, reason, label_present)
        return

    if downgrade is not None:
        _audit_once(
            task["id"],
            f"factory-refine-downgrade:{task['id']}",
            "refine_close_downgraded",
            {
                "issue_number": number,
                "claimed": outcome,
                "reason": downgrade,
                "recommendation": artifact.get("recommendation"),
            },
        )
    if effective == HUMAN_LABEL:
        _notify_once(
            task["id"],
            repo,
            number,
            (artifact.get("question") or artifact.get("evidence") or "").strip(),
            artifact.get("recommendation") or "close",
        )
    if effective in CLOSING_OUTCOMES:
        _audit_once(
            task["id"],
            f"factory-refine-close:{task['id']}",
            "intake_closed",
            {
                "issue_number": number,
                "outcome": effective,
                "evidence": (artifact.get("evidence") or "")[:500],
                "comment_url": match["html_url"],
            },
        )
    # A needs-human outcome succeeds because the refine task produced a
    # verified brief and escalated the one decision that remains. A close
    # succeeds because the issue is verifiably closed with its reason on
    # record.
    state = {
        READY_LABEL: "refine_agent_ready",
        HUMAN_LABEL: "refine_needs_human",
        REJECT_OUTCOME: "refine_rejected",
        STALE_OUTCOME: "refine_stale",
    }[effective]
    settled = finish_task(
        task["id"],
        "succeeded",
        ACTOR,
        evidence={"state": state, "reason": match["html_url"]},
    )
    if not settled["ok"]:
        logger.info(
            "factory refine settlement deferred for %s: %s",
            task["id"],
            settled["reason"],
        )
        return
    with _locked_session() as (db, _control):
        _audit(
            db,
            ACTOR,
            "refine_settled",
            task_id=task["id"],
            outcome=effective,
            claimed=outcome,
            comment_url=match["html_url"],
        )


def reconcile(
    task: dict,
    policy: dict,
    nodes: list[dict],
    runs: list[dict],
    expected_version: int,
    *,
    task_class: str = TASK_CLASS,
) -> None:
    from swarm import factory_conductor

    if task_class != TASK_CLASS:
        # Phase 3 fills this hook. Pausing is deliberate so an unreachable
        # advisory class parks visibly instead of silently planning a DAG.
        set_control("pause_task", ACTOR, task_id=task["id"])
        with _locked_session() as (db, _control):
            _audit(
                db,
                ACTOR,
                "advisory_class_unimplemented",
                task_id=task["id"],
                task_class=task_class,
            )
        return

    if not nodes:
        receipt = task_snapshot(task["id"])
        # The refine pool, not the conductor's: a brief is advisory output a
        # person reads, so it runs on the cheap lane unless the policy says
        # otherwise, and an unconfigured policy still lands on the conductor
        # pool through the pool default.
        choice = select_model("refine", policy)
        cause = f"factory-refine:{NODE_KEY}"
        result = factory_conductor._add(
            task,
            policy,
            NODE_KEY,
            refine_prompt(task, receipt, closing=closing_allowed(policy)[0]),
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
        _settle(task, succeeded, policy)
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
