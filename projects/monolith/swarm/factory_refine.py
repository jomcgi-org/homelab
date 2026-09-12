"""A bounded, server-verified issue briefing path for refine receipts."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
import logging
import os
import re
from urllib.parse import quote

from sqlmodel import select

from swarm.factory_controls import (
    DEFAULT_TASK_CLASS,
    intake_policy,
    terminal_resolution,
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
# What an option does when an operator picks it. Every effect except `hold`
# writes to GitHub, and `hold` exists so "leave it exactly as it is" is a
# choice a person can record rather than a tab they close.
OPTION_EFFECTS = ("agent-ready", "close", "split", "defer", "hold")
# The recommendation line and the first option say the same thing in two
# places, so settlement checks they agree rather than trusting either alone.
RECOMMENDED_EFFECT = {
    "deliver": "agent-ready",
    "close": "close",
    "split": "split",
    "defer": "defer",
}
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_SPLIT_CHILDREN = 5
CLOSE_REASONS = ("not_planned", "completed")
DEFER_LABEL = "needs-thought"
# An issue carrying one of these, or any milestone, is never closed by the
# lane. Triage on work someone has already prioritised or flagged as a
# security finding is a judgment call that belongs to a person.
PROTECTED_LABELS = ("critical", "security-finding")

OPTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "label", "effect"],
    "properties": {
        "key": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,31}$"},
        "label": {"type": "string", "minLength": 1, "maxLength": 120},
        "effect": {"enum": list(OPTION_EFFECTS)},
        "detail": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "scope": {"type": "string", "maxLength": 2000},
                "reason": {"enum": list(CLOSE_REASONS)},
                "comment": {"type": "string", "maxLength": 2000},
                "children": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_SPLIT_CHILDREN,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["title", "body"],
                        "properties": {
                            "title": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 256,
                            },
                            "body": {"type": "string", "maxLength": 8000},
                        },
                    },
                },
            },
        },
    },
}

REFINE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["outcome", "comment_url"],
    "properties": {
        "outcome": {"enum": list(OUTCOMES)},
        "comment_url": {"type": "string", "minLength": 1, "maxLength": 512},
        "question": {"type": "string", "maxLength": 4000},
        "recommendation": {"enum": list(RECOMMENDATIONS)},
        "summary": {"type": "string", "maxLength": 2000},
        "options": {
            "type": "array",
            "minItems": MIN_OPTIONS,
            "maxItems": MAX_OPTIONS,
            "items": OPTION_SCHEMA,
        },
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
        + _chat_prompt(receipt)
        + "Read the issue and the repository, then post EXACTLY ONE issue comment "
        f"whose first line is `{BRIEF_HEADING}`. Include `### Outcome`, "
        "`### Acceptance`, `### Files`, `### Evidence`, and `### Risks` in that "
        "order.\n\n"
        "Reach exactly one of these verdicts and act on it.\n"
        "`agent-ready` when the brief is actionable with no human decision "
        "left. Apply the `agent-ready` label.\n"
        "`needs-human` when value, scope, or staleness is unclear. Apply the "
        "`needs-human` label and make the LAST section `### Decision needed`, "
        "holding one line of the form `recommend: deliver` (or `close`, "
        "`split`, `defer`), then the single specific question a person must "
        "answer, then the same options you return in the artifact as a "
        "numbered list, the recommended one first, each line reading "
        "`1. <label>`. A reader on GitHub decides from that list, and an "
        "operator decides from the same list on the console, so the two must "
        "say the same thing.\n"
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
        "Return the typed artifact. `question`, `recommendation`, `summary` "
        "and `options` are required for `needs-human` and omitted otherwise. "
        "`summary` is the `### Outcome` paragraph verbatim. `evidence` is "
        "required for `reject` and `stale`, and is the same citation your "
        "brief section gives.\n\n" + _options_prompt()
    )


def _chat_prompt(receipt: dict) -> str:
    """The operator's unanswered question, when this brief was asked for again.

    An escalation the operator answered with "needs more chat" re-queues the
    same receipt, so the next brief is written knowing exactly what was not
    answered the first time. Without this the re-run would produce the same
    brief and the loop would not converge.
    """
    escalation = receipt.get("escalation") or {}
    chat = escalation.get("chat") or []
    if not chat:
        return ""
    note = str((chat[-1] or {}).get("note") or "").strip()
    if not note:
        return ""
    return (
        "An operator read your previous brief on this issue and asked for "
        "more before deciding. Answer this directly in `### Outcome`, and let "
        "it shape your verdict and your options:\n"
        f"{_bounded_planner_text(note, 2000)}\n\n"
    )


def _options_prompt() -> str:
    """How to write the options, which is the half that decides whether a
    person can act on the escalation in one click.

    The labels are what an operator reads under time pressure, so the prompt
    asks for the concrete act ("Close as superseded by #5656") rather than the
    verb the effect already names. A screen of buttons reading close, defer,
    hold tells a reader nothing the effect field did not.
    """
    return (
        "`options` is two to four things a person could decide, ordered with "
        "the recommendation FIRST. Its effect must match your `recommend:` "
        "line: deliver means `agent-ready`, close means `close`, split means "
        "`split`, defer means `defer`.\n"
        "Each option is `{key, label, effect, detail}`.\n"
        "`key` is a short slug, lowercase letters, digits and hyphens.\n"
        "`label` is what the button says, and it must name the concrete act "
        'with the specifics in it: "Deliver the /invoke path first", "Close '
        'as superseded by #5656", "Split the operator UI out of the API". '
        'Never a bare verb such as "close" or "defer": the effect field '
        "already says that, and a label that only repeats it gives the person "
        "deciding nothing to decide on.\n"
        "`effect` is one of `agent-ready`, `close`, `split`, `defer`, "
        "`hold`.\n"
        "`detail` carries what that effect needs: `agent-ready` takes an "
        "optional `scope` note posted as a comment; `close` takes `reason` "
        "(`not_planned` or `completed`) and a `comment` saying why; `split` "
        "takes `children`, one to five `{title, body}` issues to open before "
        "the parent closes; `defer` takes a `comment` naming the condition "
        "that would make this worth doing; `hold` takes nothing and leaves "
        "the issue exactly as it is.\n"
        "Always include one option that is not the recommendation, so the "
        "person deciding has a real alternative rather than a confirmation."
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


ESCALATIONS_URL = "https://private.jomcgi.dev/agents/escalations"


def downgrade_options(artifact: dict) -> list[dict]:
    """The options a close verdict carries once the server refuses the close.

    A downgraded `reject` or `stale` never wrote options: the node reached a
    verdict it was allowed to act on, and the server is what turned it into an
    escalation. Synthesising them here is what keeps every escalation
    decidable in one click rather than leaving the downgraded ones as a
    question with no buttons under it.
    """
    evidence = (artifact.get("evidence") or "").strip()
    first = evidence.splitlines()[0][:100] if evidence else "the cited evidence"
    return [
        {
            "key": "close",
            "label": f"Close it: {first}",
            "effect": "close",
            "detail": {
                "reason": "not_planned",
                "comment": evidence[:2000] or "Closed on the refine pass evidence.",
            },
        },
        {
            "key": "hold",
            "label": "Leave it open and decide later",
            "effect": "hold",
            "detail": {},
        },
    ]


def _escalation_document(artifact: dict, comment_url: str, downgraded: bool) -> dict:
    options = artifact.get("options")
    if not isinstance(options, list) or not options:
        options = downgrade_options(artifact)
    return {
        "recommendation": artifact.get("recommendation")
        or ("close" if downgraded else "defer"),
        "question": (
            artifact.get("question") or artifact.get("evidence") or ""
        ).strip(),
        "summary": (artifact.get("summary") or "").strip(),
        "options": options,
        "comment_url": comment_url,
        "downgraded": downgraded,
        "resolved": None,
    }


def _record_escalation(
    task_id: str, artifact: dict, comment_url: str, *, downgraded: bool
) -> dict:
    """Write the escalation document onto the receipt.

    A resolved document is never overwritten: settlement is reached again on
    every tick until it takes, and re-writing one would discard a decision an
    operator had already recorded against an escalation whose task settlement
    was still deferred. The one exception is a dismiss, which is a cleared
    card rather than a decision and wrote nothing to GitHub, so a fresh brief
    replaces it and the escalation comes back carrying the answer.

    An UNRESOLVED document is replaced, and this is the case that matters.
    A receipt the operator sent back for another brief already carries the
    first brief's question and options, and the re-brief settles onto the same
    receipt. Keeping the old document would leave the page showing the first
    brief's options over the second brief's issue, so pressing 1 would apply
    an option written before the operator's question was answered. The chat
    history carries forward, because it is the record of what was asked rather
    than part of any one brief's answer.
    """
    document = _escalation_document(artifact, comment_url, downgraded)
    with _locked_session() as (db, _control):
        row = db.exec(
            select(FactoryReceipt)
            .where(FactoryReceipt.task_id == task_id)
            .execution_options(populate_existing=True)
        ).first()
        if row is None:
            return document
        stored = json.loads(row.escalation_json) if row.escalation_json else None
        # A dismiss is the one resolution a fresh brief replaces. It wrote
        # nothing to GitHub, so preserving it would leave an operator who
        # cleared a card and then asked for another brief with the answer
        # recorded on a receipt whose card never comes back.
        if stored is not None and terminal_resolution(stored.get("resolved")):
            return stored
        if stored is not None:
            document["chat"] = stored.get("chat") or []
        row.escalation_json = json.dumps(document)
        db.add(row)
    return document


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
                f" (recommend: {recommendation}): {question[:500]}\n"
                f"Decide at {ESCALATIONS_URL}",
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


_OPTION_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def _verify_option(option: object, index: int) -> str | None:
    """One option's shape, checked by the server rather than by the schema.

    The schema goes to the guest so it writes the right thing; this runs on
    what came back, because the artifact is a claim until the server has
    checked it. Each effect is checked on the fields it will actually use at
    apply time, so an operator never clicks a button whose effect has nothing
    to act with.
    """
    where = f"option {index + 1}"
    if not isinstance(option, dict):
        return f"{where} is not an object"
    unknown = set(option) - {"key", "label", "effect", "detail"}
    if unknown:
        return f"{where} carries unsupported fields"
    key, label = option.get("key"), option.get("label")
    if not isinstance(key, str) or not _OPTION_KEY.fullmatch(key):
        return f"{where} has no usable key"
    if not isinstance(label, str) or not label.strip() or len(label) > 120:
        return f"{where} has no usable label"
    effect = option.get("effect")
    if effect not in OPTION_EFFECTS:
        return f"{where} names no known effect"
    detail = option.get("detail")
    if detail is None:
        detail = {}
    if not isinstance(detail, dict):
        return f"{where} detail is not an object"
    if effect == "close" and detail.get("reason") not in CLOSE_REASONS:
        return f"{where} closes with no reason"
    if effect == "defer" and not str(detail.get("comment") or "").strip():
        return f"{where} defers with no wait condition"
    if effect == "split":
        children = detail.get("children")
        if not isinstance(children, list) or not 1 <= len(children) <= (
            MAX_SPLIT_CHILDREN
        ):
            return f"{where} splits into no children"
        for child in children:
            if not isinstance(child, dict):
                return f"{where} has a child that is not an object"
            title = child.get("title")
            if not isinstance(title, str) or not title.strip():
                return f"{where} has a child with no title"
            if not isinstance(child.get("body", ""), str):
                return f"{where} has a child with a non-text body"
    return None


def _verify_options(artifact: dict, recommendation: str) -> str | None:
    """The option list a needs-human escalation must carry, or why it does not.

    The first option is the recommendation. Keeping them in agreement is what
    lets the operator page render one primary button and the GitHub reader see
    the same choice as item one, from a single source.
    """
    options = artifact.get("options")
    if not isinstance(options, list):
        return "needs-human carries no options"
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        return f"needs-human carries {len(options)} options, not two to four"
    for index, option in enumerate(options):
        invalid = _verify_option(option, index)
        if invalid is not None:
            return invalid
    keys = [option["key"] for option in options]
    if len(set(keys)) != len(keys):
        return "needs-human repeats an option key"
    if options[0]["effect"] != RECOMMENDED_EFFECT[recommendation]:
        return (
            f"the first option is {options[0]['effect']}, which is not what "
            f"recommend: {recommendation} asks for"
        )
    return None


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
        return _verify_options(artifact, recommendation)
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
    escalation = None
    if effective == HUMAN_LABEL:
        escalation = _record_escalation(
            task["id"],
            artifact,
            match["html_url"],
            downgraded=downgrade is not None,
        )
        _notify_once(
            task["id"],
            repo,
            number,
            escalation["question"],
            escalation["recommendation"],
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
            # The options go in the audit as well as on the receipt: the
            # receipt holds the live document a decision mutates, and the
            # audit holds what was offered at the moment the escalation was
            # raised, which is what an operator reads back afterwards.
            recommendation=escalation["recommendation"] if escalation else None,
            options=[
                {"key": option["key"], "effect": option["effect"]}
                for option in (escalation or {}).get("options") or []
            ],
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
