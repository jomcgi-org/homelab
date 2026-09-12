"""A bounded, server-verified issue briefing path for refine receipts."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
import logging
import os
from urllib.parse import quote

import httpx
from sqlalchemy import or_
from sqlmodel import select

from swarm.factory_controls import (
    DEFAULT_TASK_CLASS,
    ESCALATED,
    MAX_OPTIONS,
    MIN_OPTIONS,
    OPTION_SCHEMA,
    intake_policy,
    terminal_resolution,
    _audit,
    _locked_session,
    _read_session,
    finish_task,
    receipt_task_class,
    set_control,
    task_snapshot,
    verify_option_list,
    _ACTIVE,
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
DEFER_OUTCOME = "defer"
DEFER_LABEL = "needs-thought"
REJECT_OUTCOME = "reject"
STALE_OUTCOME = "stale"
# The two verdicts that close an issue. They are the only refine outcomes that
# remove something a person would have to restore by hand, which is why they
# sit behind their own flag and their own daily cap.
CLOSING_OUTCOMES = (REJECT_OUTCOME, STALE_OUTCOME)
OUTCOMES = (READY_LABEL, HUMAN_LABEL, DEFER_OUTCOME, *CLOSING_OUTCOMES)
# The label each verdict leaves on the issue, and so the label settlement
# demands back from GitHub before it believes the verdict.
OUTCOME_LABEL = {
    READY_LABEL: "agent-ready",
    HUMAN_LABEL: "needs-human",
    DEFER_OUTCOME: DEFER_LABEL,
    REJECT_OUTCOME: "wontfix",
    STALE_OUTCOME: "stale",
}
RECOMMENDATIONS = ("deliver", "close", "split")
# The recommendation line and the first option say the same thing in two
# places, so settlement checks they agree rather than trusting either alone.
RECOMMENDED_EFFECT = {
    "deliver": "agent-ready",
    "close": "close",
    "split": "split",
}
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
        "summary": {"type": "string", "maxLength": 2000},
        "options": {
            "type": "array",
            "minItems": MIN_OPTIONS,
            "maxItems": MAX_OPTIONS,
            "items": OPTION_SCHEMA,
        },
        "evidence": {"type": "string", "maxLength": 2000},
        "supersedes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {"type": "integer", "minimum": 1},
        },
        "in_favour_of": {"type": "integer", "minimum": 1},
    },
}


def refine_prompt(task: dict, receipt: dict, *, closing: bool) -> str:
    """The brief the guest writes, and the verdicts it may reach.

    ``closing`` says whether the lane may close an issue at all right now. It
    is false whenever the policy flag is off or the daily close cap is spent,
    and the prompt then offers three outcomes rather than five, so the guest
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
    supersedes = (
        "`supersedes` and `in_favour_of` are only used with `reject`. When a "
        "reject finds that work lives in another issue, set `in_favour_of` "
        "to that issue and list the other open issues retired by the same "
        "finding in `supersedes`. Add `### Supersedes` to the brief with one "
        "line per number. Close only the issue you are briefing; the server "
        "checks and closes eligible siblings.\n\n"
        if closing
        else ""
    )
    human_examples = (
        ", whether the work is still wanted, or confirming that #z supersedes this"
        if closing
        else ", or whether the work is still wanted"
    )
    return (
        f"Brief repository {task['repo']} issue #{receipt['issue_number']} at "
        f"{receipt['url']}.\n\n"
        f"Title: {receipt['title']}\n\nIssue body:\n{body}\n\n"
        + _chat_prompt(receipt)
        + "Research before reading toward a verdict, and cite what you find "
        "under `### Evidence`, in this order. First, call `search_knowledge` "
        "on the `agents` MCP server with the issue title, then once for every "
        "issue, ADR, or pull request number the body references. Claude CLIs "
        "see it as `mcp__agents__search_knowledge`; Codex sees the `agents` "
        "server's `search_knowledge`. Each fact carries a "
        "`verification_state`; cite it as `KG: <fact title> (<state>)`. A "
        "`verified` fact saying the work merged, the file is gone, or a Why "
        "paragraph decided against it is closing evidence. Second, inspect "
        "the checkout: read the domain's ARCHITECTURE.md `**Why.**` paragraph, "
        "run `git log` for files the issue names, and check whether referenced "
        "pull requests merged. Cite this as `git: <sha or path>`. Third, only "
        "when the issue names an external product, version, or CVE, use the "
        "web search tool if this CLI has one and cite `web: <url>`. After the "
        "verdict, call `report_knowledge` on the same server with one fact: "
        "the issue number, verdict, and one-sentence reason, so a sibling's "
        "next brief recalls it. This is best effort; settlement does not "
        "verify the report.\n\n"
        + "Read the issue and the repository, then post EXACTLY ONE issue comment "
        f"whose first line is `{BRIEF_HEADING}`. Include `### Outcome`, "
        "`### Acceptance`, `### Files`, `### Evidence`, and `### Risks` in that "
        "order.\n\n"
        "Reach exactly one of these verdicts and act on it.\n"
        "`agent-ready` when the brief is actionable with no human decision "
        "left. Apply the `agent-ready` label.\n"
        "`defer` when the issue is worth doing but needs a decision, design, "
        "or event nobody can supply now. Add a `### Why defer` section naming "
        "the concrete condition that would make it actionable, apply the "
        "`needs-thought` label, and leave the issue open.\n"
        "`needs-human` is for a decision a person can make in about a minute "
        "and that unblocks the work, such as which of two scopes"
        + human_examples
        + ". Apply the "
        "`needs-human` label and make the LAST section `### Decision needed`, "
        "holding one line of the form `recommend: deliver` (or `close`, "
        "`split`), then the single specific question a person must "
        "answer, then the same options you return in the artifact as a "
        "numbered list, the recommended one first, each line reading "
        "`1. <label>`. A reader on GitHub decides from that list, and an "
        "operator decides from the same list on the console, so the two must "
        "say the same thing.\n"
        + triage
        + "\nThe bar for closing is evidence a reader can check, not a "
        "judgement you formed. A question that needs real thought, a design, "
        "or a window only the author can declare is the `defer` verdict, taken "
        "by the node itself. Never "
        "close an issue carrying `critical` or `security-finding`, or one "
        "assigned to a milestone; those can only be `agent-ready`, `defer`, "
        "or `needs-human`.\n\n"
        "Do not edit the issue title or body. Do not create or push a branch "
        "or open a pull request. Apply no label other than the one your "
        "verdict names. Use `gh issue comment`, `gh issue edit --add-label`, "
        "`gh label create` and `gh issue close --reason not_planned`. "
        '`gh auth status` reporting "not logged in" is expected and is not a '
        "problem.\n\n"
        "Return the typed artifact. `question`, `recommendation`, `summary` "
        "and `options` are required for `needs-human` and omitted otherwise. "
        "`summary` is the `### Outcome` paragraph verbatim. `evidence` is "
        "required for `reject`, `stale`, and `defer`. For a close it is the "
        "same citation your brief section gives; for `defer` it is the "
        "concrete condition that would make the issue actionable. "
        + supersedes
        + _options_prompt(closing=closing)
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


def _options_prompt(*, closing: bool) -> str:
    """How to write the options, which is the half that decides whether a
    person can act on the escalation in one click.

    The labels are what an operator reads under time pressure, so the prompt
    asks for the concrete act ("Close as superseded by #5656") rather than the
    verb the effect already names. A screen of buttons reading close, defer,
    hold tells a reader nothing the effect field did not.
    """
    supersede = (
        "A close recommendation may use `supersede` when one decision retires "
        "several issues. "
        if closing
        else ""
    )
    effect = ", `supersede`" if closing else ""
    detail = (
        "`supersede` takes `closes`, one to ten issue numbers, and "
        "`in_favour_of`, the surviving issue number; "
        if closing
        else ""
    )
    label_examples = (
        '"Deliver the /invoke path first", "Close as superseded by #5656", '
        '"Split the operator UI out of the API"'
        if closing
        else '"Deliver the /invoke path first", "Split the operator UI out of the API"'
    )
    return (
        "`options` is two to four things a person could decide, ordered with "
        "the recommendation FIRST. Its effect must match your `recommend:` "
        "line: deliver means `agent-ready`, close means `close`, split means "
        "`split`. " + supersede + "You may offer `defer` as an alternative, but "
        "never recommend it: if deferring is right, take the `defer` verdict "
        "yourself.\n"
        "Each option is `{key, label, effect, detail}`.\n"
        "`key` is a short slug, lowercase letters, digits and hyphens.\n"
        "`label` is what the button says, and it must name the concrete act "
        "with the specifics in it: " + label_examples + ". "
        'Never a bare verb such as "close" or "defer": the effect field '
        "already says that, and a label that only repeats it gives the person "
        "deciding nothing to decide on.\n"
        "`effect` is one of `agent-ready`, `close`"
        + effect
        + ", `split`, `defer`, `hold`.\n"
        "`detail` carries what that effect needs: `agent-ready` takes an "
        "optional `scope` note posted as a comment; `close` takes `reason` "
        "(`not_planned` or `completed`) and a `comment` saying why; `split` "
        "takes `children`, one to five `{title, body}` issues to open before "
        "the parent closes; " + detail + "`defer` "
        "takes a `comment` naming the condition "
        "that would make this worth doing; `hold` takes nothing and leaves "
        "the issue exactly as it is.\n"
        "Always include one option that is not the recommendation, so the "
        "person deciding has a real alternative rather than a confirmation."
    )


def closes_today(*, exclude_task_id: str | None = None) -> int:
    """Issues this lane has closed in the last 24 hours, from the audit trail."""
    from datetime import timedelta

    from swarm.factory_controls import _now

    cutoff = _now() - timedelta(hours=24)
    conditions = [
        FactoryAudit.action == "intake_closed",
        FactoryAudit.created_at >= cutoff,
    ]
    if exclude_task_id is not None:
        conditions.append(
            or_(
                FactoryAudit.task_id.is_(None),
                FactoryAudit.task_id != exclude_task_id,
            )
        )
    with _read_session() as db:
        return len(db.exec(select(FactoryAudit.id).where(*conditions)).all())


def closing_allowed(
    policy: dict, *, exclude_task_id: str | None = None
) -> tuple[bool, str]:
    """Whether a close verdict may be acted on, and why not when it may not.

    Read twice: once to shape the prompt, so the guest is never offered an
    action the server would refuse, and once at settlement, so a cap spent
    while the node was running still holds.
    """
    intake = intake_policy(policy)
    if not intake["close_enabled"]:
        return False, "close_disabled"
    if closes_today(exclude_task_id=exclude_task_id) >= intake["max_closes_per_day"]:
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


def _verify_options(artifact: dict, recommendation: str) -> str | None:
    """The option list a needs-human escalation must carry, or why it does not.

    The shape is the shared one in factory_controls, which a delivery pause
    raises too. What is refine's own is the cross-check below: the first
    option is the recommendation, and keeping the two in agreement is what
    lets the operator page render one primary button and the GitHub reader
    see the same choice as item one, from a single source.
    """
    options = artifact.get("options")
    invalid = verify_option_list(options, subject="needs-human")
    if invalid is not None:
        return invalid
    effect = options[0]["effect"]
    expected = RECOMMENDED_EFFECT[recommendation]
    allowed = ("close", "supersede") if recommendation == "close" else (expected,)
    if effect not in allowed:
        expected_text = "close or supersede" if recommendation == "close" else expected
        return (
            f"the first option is {effect}, which is not what recommend: "
            f"{recommendation} asks for (expected {expected_text})"
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
    if outcome in (*CLOSING_OUTCOMES, DEFER_OUTCOME):
        if not isinstance(evidence, str) or not evidence.strip():
            return f"{outcome} cites no evidence"
        if outcome == REJECT_OUTCOME:
            siblings = artifact.get("supersedes")
            if siblings is not None and (
                not isinstance(siblings, list)
                or not 1 <= len(siblings) <= 10
                or any(type(number) is not int or number < 1 for number in siblings)
            ):
                return "reject carries invalid superseded issue numbers"
            favoured = artifact.get("in_favour_of")
            if favoured is not None and (type(favoured) is not int or favoured < 1):
                return "reject carries an invalid issue in favour"
        return None
    if question is not None or recommendation is not None:
        return "agent-ready carries a question or recommendation"
    return None


def _verify_supersedes(
    repo: str, number: int, artifact: dict, outcome: str
) -> str | None:
    """Validate reject relationships before inspecting the primary issue."""
    if outcome != REJECT_OUTCOME:
        return None
    siblings = artifact.get("supersedes")
    favoured = artifact.get("in_favour_of")
    if siblings is not None and favoured is None:
        return "supersedes names no issue in favour"
    if favoured is None:
        return None
    if favoured == number:
        return "issue in favour is the briefed issue"
    if siblings is not None and favoured in siblings:
        return "supersedes includes the issue in favour"
    if siblings is not None and number in siblings:
        return "supersedes includes the briefed issue"
    try:
        issue = github_get(repo, f"issues/{favoured}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return "issue in favour is not an open issue"
        raise
    if (
        not isinstance(issue, dict)
        or issue.get("state") != "open"
        or "pull_request" in issue
    ):
        return "issue in favour is not an open issue"
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


def _github_write(repo: str, suffix: str, payload: dict, *, method: str = "POST"):
    """The bounded GitHub writer, imported lazily to keep board reads light."""
    from swarm.factory_landing import github_write

    return github_write(repo, suffix, payload, method=method)


def _supersede_marker(task_id: str, sibling: int) -> str:
    return f"<!-- factory-supersede:{task_id}:{sibling} -->"


def _has_supersede_marker(repo: str, sibling: int, marker: str) -> bool:
    for page in range(1, MAX_COMMENT_PAGES + 1):
        comments = github_list(
            repo,
            f"issues/{sibling}/comments?per_page={COMMENT_PAGE_SIZE}&page={page}",
        )
        if any(
            isinstance(comment, dict) and marker in str(comment.get("body") or "")
            for comment in comments
        ):
            return True
        if len(comments) < COMMENT_PAGE_SIZE:
            break
    return False


def _active_receipt(repo: str, sibling: int) -> bool:
    with _read_session() as db:
        return (
            db.exec(
                select(FactoryReceipt.id).where(
                    FactoryReceipt.repo == repo,
                    FactoryReceipt.issue_number == sibling,
                    FactoryReceipt.state.in_((*_ACTIVE, "queued", ESCALATED)),
                )
            ).first()
            is not None
        )


def _supersede_skip(task_id: str, number: int, sibling: int, reason: str) -> None:
    _audit_once(
        task_id,
        f"factory-refine-supersede-skip:{task_id}:{sibling}",
        "refine_supersede_skipped",
        {"issue_number": number, "sibling": sibling, "reason": reason},
    )


def _settle_superseded(
    task: dict, policy: dict, artifact: dict, comment_url: str
) -> None:
    """Close eligible siblings named by a verified primary reject."""
    number = task_snapshot(task["id"])["issue_number"]
    repo = task["repo"]
    favoured = artifact.get("in_favour_of") or number
    cap = intake_policy(policy)["max_closes_per_day"]
    for sibling in dict.fromkeys(artifact.get("supersedes") or []):
        try:
            issue = github_get(repo, f"issues/{sibling}")
            marker = _supersede_marker(task["id"], sibling)
            marked = _has_supersede_marker(repo, sibling, marker)
            if marked:
                issue = github_get(repo, f"issues/{sibling}")
            if marked and issue.get("state") == "closed":
                _audit_once(
                    task["id"],
                    f"factory-refine-supersede-close:{task['id']}:{sibling}",
                    "intake_closed",
                    {
                        "issue_number": sibling,
                        "outcome": REJECT_OUTCOME,
                        "superseded_by": favoured,
                        "comment_url": comment_url,
                    },
                )
                continue
            if "pull_request" in issue:
                _supersede_skip(task["id"], number, sibling, "pull_request")
                continue
            labels = {
                str(label.get("name") or "").lower()
                for label in issue.get("labels") or []
                if isinstance(label, dict)
            }
            if not marked and issue.get("state") != "open":
                _supersede_skip(task["id"], number, sibling, "not_open")
                continue
            if labels & set(PROTECTED_LABELS):
                _supersede_skip(task["id"], number, sibling, "protected_label")
                continue
            if issue.get("milestone"):
                _supersede_skip(task["id"], number, sibling, "milestone")
                continue
            if issue.get("assignees"):
                _supersede_skip(task["id"], number, sibling, "assigned")
                continue
            if _active_receipt(repo, sibling):
                _supersede_skip(task["id"], number, sibling, "active_receipt")
                continue
            if closes_today() >= cap:
                _supersede_skip(task["id"], number, sibling, "close_cap")
                continue

            if not marked:
                _github_write(
                    repo,
                    f"issues/{sibling}/comments",
                    {
                        "body": (
                            f"{marker}\nSuperseded by #{favoured}. See the brief "
                            f"on #{number}: {comment_url}"
                        )
                    },
                )
            _github_write(repo, f"issues/{sibling}/labels", {"labels": ["wontfix"]})
            _github_write(
                repo,
                f"issues/{sibling}",
                {"state": "closed", "state_reason": "not_planned"},
                method="PATCH",
            )
            _audit_once(
                task["id"],
                f"factory-refine-supersede-close:{task['id']}:{sibling}",
                "intake_closed",
                {
                    "issue_number": sibling,
                    "outcome": REJECT_OUTCOME,
                    "superseded_by": favoured,
                    "comment_url": comment_url,
                },
            )
        except Exception as exc:  # noqa: BLE001 - one sibling cannot fail the task
            _supersede_skip(task["id"], number, sibling, type(exc).__name__)


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
    if invalid is None:
        invalid = _verify_supersedes(repo, number, artifact, outcome)
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
        allowed, why = closing_allowed(policy, exclude_task_id=task["id"])
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
        if effective == REJECT_OUTCOME:
            _settle_superseded(task, policy, artifact, match["html_url"])
    # A needs-human outcome succeeds because the refine task produced a
    # verified brief and escalated the one decision that remains. A close
    # succeeds because the issue is verifiably closed with its reason on
    # record.
    state = {
        READY_LABEL: "refine_agent_ready",
        HUMAN_LABEL: "refine_needs_human",
        DEFER_OUTCOME: "refine_deferred",
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
            refine_prompt(
                task,
                receipt,
                closing=closing_allowed(policy, exclude_task_id=task["id"])[0],
            ),
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
    if node is None or any(
        run["status"] not in factory_conductor.graph.TERMINAL_RUN_STATUSES
        for run in attempts
    ):
        return
    ready = {
        candidate["node_key"]
        for candidate in factory_conductor._ready_nodes(nodes, runs)
    }
    if NODE_KEY in ready:
        return
    accounted = sum(run["accounted_cost_usd"] for run in attempts)
    # Attempts the node actually spent, which is what readiness above counted:
    # a create the control plane refused for capacity is excluded up to its
    # bound, so a saturated EmberVM does not read as a node out of attempts.
    spent = factory_conductor.graph.attempts_spent(attempts, NODE_KEY)
    if spent >= node["max_attempts"]:
        reason = (
            "Refine attempt limit exhausted after "
            f"{spent} of {node['max_attempts']} attempts without a "
            "verified brief."
        )
    elif accounted >= node["max_cost_usd"]:
        reason = (
            "Refine cost limit exhausted after "
            f"{spent} of {node['max_attempts']} attempts: "
            f"{accounted:.2f} USD accounted against the "
            f"{node['max_cost_usd']:.2f} USD node allowance without a verified brief."
        )
    else:
        # Refine has no dependencies, but keep this conservative if its graph
        # shape ever changes. Only the same attempt or cost bounds used by the
        # scheduler authorize terminal settlement here.
        return
    settled = finish_task(
        task["id"],
        "failed",
        ACTOR,
        evidence={"state": "refine_failed", "reason": reason},
    )
    if settled["ok"]:
        with _locked_session() as (db, _control):
            _audit(
                db,
                ACTOR,
                "refine_failed",
                task_id=task["id"],
                reason=reason,
            )
