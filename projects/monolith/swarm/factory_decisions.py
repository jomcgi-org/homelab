"""Apply an operator's answer to a refine escalation, as a repository write.

Phase 6 of #6002. A `needs-human` refine leaves an escalation document on its
receipt: the question, the recommendation, and two to four options a person
could pick. This module is what turns a pick into labels, comments, child
issues and closes on GitHub, and records who decided what.

Two properties do the work here.

**Every write is idempotent on the pair (receipt, option).** A comment carries
a hidden marker naming that pair and is skipped when the marker is already on
the issue, a label add is idempotent by nature, and each child issue a split
opens is fenced by its own audit row. So a retry after a network failure
finishes the decision rather than doubling it, which matters because the
operator who clicks twice is the operator whose first click looked like it did
nothing.

**A decision is claimed before it is applied.** The claim is what refuses a
second, different option against the same escalation: an escalation answered
`close` must not also be answered `deliver` because two tabs were open.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx
from sqlmodel import select

from swarm.factory_controls import (
    _audit,
    _locked_session,
    _now,
    _read_session,
    intake_policy,
)
from swarm.factory_intake import INTAKE_ACTOR
from swarm.factory_models import FactoryAudit, FactoryControl, FactoryReceipt
from swarm.factory_refine import (
    CLOSE_REASONS,
    DEFER_LABEL,
    HUMAN_LABEL,
    READY_LABEL,
    TASK_CLASS,
)

logger = logging.getLogger(__name__)

ACTOR = "factory:decision"
MAX_NOTE = 4000
# Comment pages read before applying, to find this decision's own marker. A
# decision is answered once and the marker sits on a comment this code wrote,
# so one page is the ordinary case and the cap only bounds a busy thread.
COMMENT_PAGE_SIZE = 100
MAX_COMMENT_PAGES = 3
CHAT_PREFIX = "Operator asks:"
# Receipt states that mean a node is live on this issue right now.
_RUNNING_STATES = ("admitted", "uncertain")
# Receipt states a settled refine can be decided from.
_SETTLED_STATES = ("succeeded", "failed")


class DecisionError(ValueError):
    """A decision that cannot be applied, with the reason an operator reads."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _marker(receipt_id: int, option_key: str) -> str:
    """The hidden tag that makes one decision's comment writable exactly once."""
    return f"<!-- factory-decision:{receipt_id}:{option_key} -->"


def _iso(value: datetime) -> str:
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(timezone.utc).isoformat()


def _github():
    """The read and write seams, imported lazily and in one place.

    Landing owns the bounded write client and the conductor owns the bounded
    read; importing them here rather than at module scope keeps a board render
    off the reconciler, exactly as landing does it.
    """
    from swarm.factory_conductor import github_get, github_list
    from swarm.factory_landing import github_write

    return github_get, github_list, github_write


def escalation_of(row: FactoryReceipt) -> dict | None:
    return json.loads(row.escalation_json) if row.escalation_json else None


def _receipt(db, receipt_id: int) -> FactoryReceipt:
    row = db.exec(
        select(FactoryReceipt)
        .where(FactoryReceipt.id == receipt_id)
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        raise DecisionError(404, "no such receipt")
    return row


def _option(escalation: dict, option_key: str) -> dict:
    for option in escalation.get("options") or []:
        if option.get("key") == option_key:
            return option
    raise DecisionError(422, "the escalation offers no such option")


def _claim(receipt_id: int, option_key: str, actor: str) -> tuple[dict, dict, dict]:
    """Reserve this escalation for this option, or say why it cannot be.

    Returns the receipt fields the effects need, the escalation document, and
    the resolution already recorded when there is one. A repeat of the same
    option is not an error: it returns the stored resolution, so an operator
    who clicks twice sees the first click's outcome rather than a conflict.
    """
    with _locked_session() as (db, _control):
        row = _receipt(db, receipt_id)
        escalation = escalation_of(row)
        if escalation is None:
            raise DecisionError(409, "this receipt raised no escalation")
        option = _option(escalation, option_key)
        resolved = escalation.get("resolved")
        if resolved is None and row.state in _RUNNING_STATES:
            # A re-brief the operator asked for is running right now. Applying
            # an option under it would close or relabel an issue a live node is
            # still writing to, and the node would then settle against a
            # verdict nobody asked it for.
            raise DecisionError(
                409, "a brief is running on this issue; decide when it settles"
            )
        if resolved is not None:
            if resolved.get("option_key") == option_key:
                return _fields(row), escalation, resolved
            raise DecisionError(
                409,
                f"this escalation was already decided as {resolved.get('option_key')}",
            )
        holder = _live_claim(db, receipt_id, option_key)
        if holder is not None:
            raise DecisionError(409, f"a decision for {holder} is already in flight")
        _audit(
            db,
            actor,
            "decision_claimed",
            task_id=row.task_id,
            receipt_id=receipt_id,
            issue_number=row.issue_number,
            option_key=option_key,
            effect=option.get("effect"),
        )
        return _fields(row), escalation, None


def _decision_audits(db, receipt_id: int, action: str) -> list[dict]:
    """This receipt's rows for one decision action, in order.

    Matched on the receipt id inside the detail rather than on task_id. A
    receipt the operator sent back for another brief has task_id NULL until
    the lane admits it again, and `FactoryAudit.task_id == None` renders as
    `IS NULL`, which matches every other receipt in that state. Reading a
    claim or a child record for the wrong issue is not a thing to leave to
    ordering.
    """
    rows = db.exec(
        select(FactoryAudit.detail_json)
        .where(FactoryAudit.action == action)
        .order_by(FactoryAudit.id)
    ).all()
    details = [json.loads(raw) for raw in rows]
    return [detail for detail in details if detail.get("receipt_id") == receipt_id]


def _live_claim(db, receipt_id: int, option_key: str) -> str | None:
    """The other option holding this escalation, when one still holds it.

    A claim is superseded by a later `decision_failed` for the same option.
    Without that, one failed GitHub call would lock the escalation to the
    option that failed: the operator could neither retry it into a different
    answer nor pick another, and the only way out would be the database.
    """
    failed = [
        detail.get("option_key")
        for detail in _decision_audits(db, receipt_id, "decision_failed")
    ]
    for detail in _decision_audits(db, receipt_id, "decision_claimed"):
        held = detail.get("option_key")
        if held == option_key:
            continue
        if held in failed:
            continue
        return held
    return None


def _fields(row: FactoryReceipt) -> dict:
    return {
        "id": row.id,
        "repo": row.repo,
        "issue_number": row.issue_number,
        "generation": row.generation,
        "title": row.title,
        "url": row.url,
        "task_id": row.task_id,
        "task_class": row.task_class,
    }


def _has_marker(repo: str, number: int, marker: str) -> bool:
    """Whether this decision already wrote its comment on this issue."""
    _get, github_list, _write = _github()
    for page in range(1, MAX_COMMENT_PAGES + 1):
        rows = github_list(
            repo, f"issues/{number}/comments?per_page={COMMENT_PAGE_SIZE}&page={page}"
        )
        for comment in rows:
            if isinstance(comment, dict) and marker in str(comment.get("body") or ""):
                return True
        if len(rows) < COMMENT_PAGE_SIZE:
            break
    return False


def _comment(repo: str, number: int, marker: str, body: str) -> bool:
    """Post one comment at most once. True when this call wrote it."""
    if _has_marker(repo, number, marker):
        return False
    _get, _list, github_write = _github()
    github_write(repo, f"issues/{number}/comments", {"body": f"{marker}\n{body}"})
    return True


def _label(repo: str, number: int, add: list[str], remove: list[str]) -> None:
    """Move labels. Adding is idempotent; removing a label that is gone is a 404."""
    _get, _list, github_write = _github()
    if add:
        github_write(repo, f"issues/{number}/labels", {"labels": add})
    for name in remove:
        try:
            github_write(repo, f"issues/{number}/labels/{name}", {}, method="DELETE")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise


def _close(repo: str, number: int, reason: str) -> None:
    _get, _list, github_write = _github()
    if reason not in CLOSE_REASONS:
        raise DecisionError(422, "unsupported close reason")
    github_write(
        repo,
        f"issues/{number}",
        {"state": "closed", "state_reason": reason},
        method="PATCH",
    )


def _children_done(receipt_id: int) -> dict[int, int]:
    """Child issues this split already opened, by index, from the audit trail.

    Creating an issue is the one write here with no natural idempotency: there
    is no cheap read that says "the issue I would create already exists". So
    each one is fenced on its own row, and a retry resumes at the first index
    the trail does not name rather than opening the whole set again.
    """
    with _read_session() as db:
        details = _decision_audits(db, receipt_id, "decision_child_created")
    done: dict[int, int] = {}
    for detail in details:
        index, number = detail.get("child_index"), detail.get("child_number")
        if isinstance(index, int) and isinstance(number, int):
            done[index] = number
    return done


def _record_child(
    receipt_id: int, task_id: str | None, index: int, number: int, title: str
) -> None:
    with _locked_session() as (db, _control):
        _audit(
            db,
            ACTOR,
            "decision_child_created",
            task_id=task_id,
            receipt_id=receipt_id,
            child_index=index,
            child_number=number,
            title=title[:256],
        )


def _apply_split(fields: dict, option: dict, marker: str) -> dict:
    repo, number = fields["repo"], fields["issue_number"]
    _get, _list, github_write = _github()
    children = (option.get("detail") or {}).get("children") or []
    done = _children_done(fields["id"])
    opened: list[int] = []
    for index, child in enumerate(children):
        if index in done:
            opened.append(done[index])
            continue
        body = str(child.get("body") or "")
        created = github_write(
            repo,
            "issues",
            {
                "title": str(child.get("title"))[:256],
                "body": (
                    f"{body}\n\nSplit out of #{number} by an operator decision "
                    f"on the factory escalation."
                ).strip(),
            },
        )
        child_number = created.get("number") if isinstance(created, dict) else None
        if not isinstance(child_number, int):
            raise DecisionError(502, "GitHub did not return a child issue number")
        _record_child(
            fields["id"],
            fields["task_id"],
            index,
            child_number,
            str(child.get("title")),
        )
        opened.append(child_number)
    listed = "\n".join(f"- #{child}" for child in opened)
    _comment(
        repo,
        number,
        marker,
        f"Split by an operator decision into:\n{listed}\n\n"
        "Closing this parent; the children carry the work.",
    )
    _close(repo, number, "not_planned")
    return {"children": opened, "closed": True}


def _apply(fields: dict, option: dict, note: str | None) -> dict:
    """Perform one option's effect on GitHub and describe what it did."""
    repo, number = fields["repo"], fields["issue_number"]
    effect = option.get("effect")
    detail = option.get("detail") or {}
    marker = _marker(fields["id"], option["key"])
    suffix = f"\n\nOperator note: {note.strip()}" if note and note.strip() else ""
    if effect == "hold":
        return {"held": True}
    if effect == "agent-ready":
        # The escalation label comes off with the same call that puts the
        # delivery label on. Leaving `needs-human` behind would keep the issue
        # in the intake exclusion list, so the decision would read as applied
        # and the lane would still never pick the work up.
        _label(repo, number, [READY_LABEL], [HUMAN_LABEL])
        scope = str(detail.get("scope") or "").strip()
        body = f"Ready to deliver by an operator decision: {option['label']}."
        if scope:
            body = f"{body}\n\nScope:\n{scope}"
        _comment(repo, number, marker, body + suffix)
        return {"labels_added": [READY_LABEL], "labels_removed": [HUMAN_LABEL]}
    if effect == "close":
        reason = str(detail.get("reason") or "not_planned")
        comment = str(detail.get("comment") or "").strip()
        body = f"Closed by an operator decision: {option['label']}."
        if comment:
            body = f"{body}\n\n{comment}"
        _comment(repo, number, marker, body + suffix)
        _close(repo, number, reason)
        return {"closed": True, "reason": reason}
    if effect == "defer":
        _label(repo, number, [DEFER_LABEL], [HUMAN_LABEL])
        comment = str(detail.get("comment") or "").strip()
        body = f"Deferred by an operator decision: {option['label']}."
        if comment:
            body = f"{body}\n\nWaiting on:\n{comment}"
        _comment(repo, number, marker, body + suffix)
        return {"labels_added": [DEFER_LABEL], "labels_removed": [HUMAN_LABEL]}
    if effect == "split":
        return _apply_split(fields, option, marker)
    raise DecisionError(422, "unsupported option effect")


def _resolve(
    receipt_id: int, option: dict, actor: str, note: str | None, effects: dict
) -> dict:
    """Write the resolution onto the escalation and audit what it did."""
    resolution = {
        "option_key": option["key"],
        "label": option["label"],
        "effect": option["effect"],
        "actor": actor,
        "note": (note or "").strip()[:MAX_NOTE] or None,
        "effects": effects,
        "decided_at": _iso(_now()),
    }
    with _locked_session() as (db, _control):
        row = _receipt(db, receipt_id)
        escalation = escalation_of(row) or {}
        if escalation.get("resolved") is None:
            escalation["resolved"] = resolution
            row.escalation_json = json.dumps(escalation)
            # A decision cancels a re-brief the operator asked for and then
            # answered without waiting. Leaving the receipt queued would have
            # the lane spend an advisory slot briefing an issue that is
            # already closed, split or labelled for delivery.
            if row.state == "queued":
                row.state = "succeeded"
            row.updated_at = _now()
            db.add(row)
        else:
            resolution = escalation["resolved"]
        _audit(
            db,
            actor,
            "decision_applied",
            task_id=row.task_id,
            receipt_id=receipt_id,
            issue_number=row.issue_number,
            option_key=resolution["option_key"],
            effect=resolution["effect"],
            effects=resolution["effects"],
        )
    return resolution


def apply_decision(
    receipt_id: int, option_key: str, actor: str, note: str | None = None
) -> dict:
    """Answer one escalation with one of its options."""
    fields, escalation, resolved = _claim(receipt_id, option_key, actor)
    if resolved is not None:
        return {"ok": True, "applied": False, "resolution": resolved}
    option = _option(escalation, option_key)
    try:
        effects = _apply(fields, option, note)
    except (DecisionError, httpx.HTTPError, ValueError) as exc:
        # Every failure is recorded, a DecisionError included. A split whose
        # child issue came back without a number raises one from inside the
        # effect, and leaving that unaudited meant the claim it holds could
        # never be superseded: the escalation would be locked to an option
        # that had already failed, with nothing on the trail to say so.
        _record_failure(receipt_id, fields["task_id"], option_key, actor, exc)
        logger.warning(
            "factory decision %s on receipt %s failed",
            option_key,
            receipt_id,
            exc_info=True,
        )
        if isinstance(exc, DecisionError):
            raise
        raise DecisionError(502, "the decision could not be applied on GitHub") from exc
    resolution = _resolve(receipt_id, option, actor, note, effects)
    return {"ok": True, "applied": True, "resolution": resolution}


def _record_failure(
    receipt_id: int, task_id: str | None, option_key: str, actor: str, exc: Exception
) -> None:
    with _locked_session() as (db, _control):
        _audit(
            db,
            actor,
            "decision_failed",
            task_id=task_id,
            receipt_id=receipt_id,
            option_key=option_key,
            error=type(exc).__name__,
            status=getattr(exc, "status", None)
            or getattr(getattr(exc, "response", None), "status_code", None),
        )


def _requeue_blocker(db, row: FactoryReceipt) -> str | None:
    """Why this receipt cannot go back to the lane, in words an operator reads.

    Every one of these ends with the question posted on the issue and nothing
    scheduled to answer it, which is the outcome the page has to say out loud:
    a comment that reads like a request and a lane that never heard it is
    worse than a refusal.
    """
    if row.task_class != TASK_CLASS:
        return "this receipt is not a refine, so there is no brief to re-run"
    if row.state in _RUNNING_STATES:
        return "a brief is already running on this issue"
    if row.state == "queued":
        return "a brief is already queued for this issue"
    if row.state not in _SETTLED_STATES:
        return f"the receipt is {row.state}, so the lane will not admit it again"
    control = db.exec(
        select(FactoryControl)
        .where(FactoryControl.id == "factory")
        .execution_options(populate_existing=True)
    ).first()
    policy = json.loads(control.policy_json) if control else {}
    if row.generation != policy.get("generation"):
        return (
            f"the receipt is generation {row.generation} and the policy is on "
            f"{policy.get('generation')}, so admission will not select it"
        )
    # The same two clauses admit_next ORs together. An issue in the operator
    # allowlist is admissible whatever intake is doing, so reading the intake
    # flag alone would refuse a re-brief the lane would happily have run.
    allowlisted = row.issue_number in (policy.get("issue_numbers") or [])
    discovered = row.actor == INTAKE_ACTOR and intake_policy(policy)["enabled"]
    if not (allowlisted or discovered):
        return (
            "this issue is neither in the policy allowlist nor discoverable "
            "with intake on, so admission will not select it"
        )
    return None


def _requeue_refine(db, row: FactoryReceipt, note: str, actor: str) -> str | None:
    """Return this refine receipt to the queue so the lane briefs it again.

    The same receipt rather than a new one, because admission selects on the
    policy's generation and a receipt at any other generation would never be
    admitted at all. The issue text is never rewritten: the operator's note
    lives on the escalation document, and the prompt reads it from there.

    Returns the reason it could not be re-queued, or None when it was. The
    question is recorded and posted either way, so the caller is the one that
    has to tell the operator which of the two happened.
    """
    escalation = escalation_of(row) or {}
    chat = list(escalation.get("chat") or [])
    blocker = _requeue_blocker(db, row)
    # The question is recorded whether or not the receipt can be re-queued, so
    # a second question asked while the first re-brief is still queued is not
    # silently dropped. The prompt reads the newest entry.
    chat.append(
        {
            "note": note,
            "actor": actor,
            "asked_at": _iso(_now()),
            "requeued": blocker is None,
            "blocked_by": blocker,
        }
    )
    escalation["chat"] = chat
    row.escalation_json = json.dumps(escalation)
    row.updated_at = _now()
    if blocker is None:
        row.state = "queued"
        row.task_id = None
        row.policy_json = None
        row.allowance_json = None
        row.task_paused = False
        row.cancellation_requested = False
    db.add(row)
    return blocker


def request_chat(receipt_id: int, note: str, actor: str) -> dict:
    """Ask the lane for more before deciding, and say so on the issue."""
    note = (note or "").strip()
    if not note:
        return {"ok": False, "reason": "a chat request needs a note"}
    if len(note) > MAX_NOTE:
        raise DecisionError(422, "the note is too long")
    with _read_session() as db:
        row = _receipt(db, receipt_id)
        escalation = escalation_of(row)
        fields = _fields(row)
    if escalation is None:
        raise DecisionError(409, "this receipt raised no escalation")
    if escalation.get("resolved") is not None:
        raise DecisionError(409, "this escalation was already decided")
    # Keyed on how many times chat has been asked, so a second, different
    # question posts a second comment while a retry of the first does not.
    sequence = len(escalation.get("chat") or [])
    marker = _marker(receipt_id, f"chat-{sequence}")
    try:
        _comment(
            fields["repo"],
            fields["issue_number"],
            marker,
            f"{CHAT_PREFIX} {note}\n\nRe-queued for another brief that answers this.",
        )
    except (httpx.HTTPError, ValueError) as exc:
        raise DecisionError(502, "the question could not be posted on GitHub") from exc
    with _locked_session() as (db, _control):
        row = _receipt(db, receipt_id)
        task_id = row.task_id
        blocker = _requeue_refine(db, row, note, actor)
        _audit(
            db,
            actor,
            "decision_chat_requested",
            task_id=task_id,
            receipt_id=receipt_id,
            issue_number=fields["issue_number"],
            note=note[:MAX_NOTE],
            requeued=blocker is None,
            blocked_by=blocker,
            sequence=sequence,
        )
    return {
        "ok": True,
        "requeued": blocker is None,
        "blocked_by": blocker,
        "sequence": sequence,
    }


__all__ = ["DecisionError", "apply_decision", "request_chat"]
