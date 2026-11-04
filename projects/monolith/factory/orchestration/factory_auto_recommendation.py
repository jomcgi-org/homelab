"""Default an eligible routine escalation to its validated recommendation.

An escalation card asks a person to pick one of two to four options, the
recommendation first. For a routine card whose recommendation only carries the
work on, waiting for that pick makes the factory ask permission to solve the
problem rather than to land a working solution. This tick answers such a card
through the same durable, idempotent `request_decision` path an operator uses,
so every effect, fence and audit row is the existing one.

Nothing here executes option prose. Only the structured `agent-ready` effect of
the first option is applied, and only when every gate below passes. A card that
fails a gate stays for a person, exactly as before.

A blocker that keeps coming back is not answered again. Without new evidence (a
different question or a moved pull request head) the card is left in place and
one `recurring_blocker` audit is written, which the existing problem-issue
producer turns into a single deduplicated repair issue.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import timezone

from sqlmodel import select

from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt

logger = logging.getLogger(__name__)

ACTOR = "factory:auto-recommendation"
ENABLED_ENV = "FACTORY_AUTO_RECOMMENDATION_ENABLED"
APPLIED = "auto_recommendation_applied"
BLOCKER = "recurring_blocker"
# A person who is already looking at the card gets this long to answer first.
GRACE_SECONDS = 900
INTERVAL_SECONDS = 300
MAX_PER_TICK = 3
# Answers per receipt, however much evidence moves. Past it the repair path
# owns the problem.
MAX_PER_RECEIPT = 3
# A repair issue the factory generated never spawns a repair of its own.
REPAIR_MARKER = "factory-problem:"
HUMAN_OWNED_LABELS = frozenset({"human", "security-finding", "security"})
# The only card origins that may be defaulted. Everything else, including
# every unstamped or historical card, waits for a person.
ROUTINE_ORIGINS = frozenset({"planner_pause", "deadline_pause"})
# Direction keys owned by an explicit human or operator constraint.
RESTRICTION_KEYS = (
    "landing_hold",
    "landing_hold_provenance",
    "model_restriction",
    "nonrenewable_recovery",
)
MAX_REVIEW_PAGES = 5
# Text that routes a card to a person: access and secrets, destructive scope.
SENSITIVE = re.compile(
    r"secret|credential|token|password|api[ -]?key|private key|\bssh\b|\bcert"
    r"|kubeconfig|cluster-admin|security|vulnerab|exploit|rbac|\biam\b"
    r"|1password|oauth|\bauth|\baccess|privileg|bypass|destructive|delet|destroy"
    r"|\bremov|\bdrop|wip(e|ing)|purg|truncat|overwrit|irreversib|force[- ]push"
    r"|reset --hard|rm -rf|disabl\w* (the )?(review|ci|gate|check)"
    r"|human[- ]owned|\bdo not (merge|deploy)",
    re.IGNORECASE,
)

EVIDENCE = "\n\nevidence: "
NOTE = (
    "Applied the validated recommendation: no hold, access or destructive "
    "scope, and no open review objection."
)

_last_run: float | None = None


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "false").lower() == "true"


def _norm(text: object) -> str:
    return " ".join(str(text or "").lower().split())


def _sensitive(escalation: dict, option: dict) -> bool:
    """Defence in depth only. A miss is never authorization: the structured
    origin allowlist and the shared restriction owners decide first."""
    text = " ".join(
        str(part or "")
        for part in (
            escalation.get("question"),
            escalation.get("reason"),
            escalation.get("summary"),
            option.get("label"),
            json.dumps(option.get("detail") or {}),
        )
    )
    return SENSITIVE.search(text) is not None


def _origin(row: FactoryReceipt, escalation: dict) -> str | None:
    """The server-stamped class of a routine card, or None when unclassified."""
    from factory.orchestration.factory_refine import TASK_CLASS

    stamped = (escalation.get("origin") or {}).get("class")
    if stamped in ROUTINE_ORIGINS:
        return stamped
    # A refinement of an issue the factory generated for its own repair.
    if row.task_class == TASK_CLASS and REPAIR_MARKER in (row.body or ""):
        return "repair_refine"
    return None


def _runs(db, row: FactoryReceipt) -> list[dict]:
    if not row.task_id:
        return []
    from factory.orchestration import graph

    return graph.node_runs(row.task_id, session=db)


def failure_identity(db, row: FactoryReceipt, origin: str) -> tuple[str, int]:
    """Stable blocker key and the newest server-observed success watermark.

    The key names the problem (issue, card class and the nodes that failed),
    not the prose a planner wrote about it, so rewording cannot reset it.
    """
    runs = _runs(db, row)
    failed = sorted(
        {r["node_key"] for r in runs if r["status"] in ("failed", "escalated")}
    )
    raw = f"{row.repo}#{row.issue_number}:{origin}:{','.join(failed)}"
    watermark = max((r["id"] for r in runs if r["status"] == "succeeded"), default=0)
    return hashlib.sha256(raw.encode()).hexdigest(), watermark


def screen(db, row: FactoryReceipt, escalation: dict | None, now) -> str | None:
    """Reasons a card is not routine. None means it may proceed.

    Runs both before the live read and again under the claim lock.
    """
    from factory.orchestration import factory_pause

    if not isinstance(escalation, dict) or escalation.get("resolved") is not None:
        return "not_open"
    if row.state != controls.ESCALATED or row.task_paused or row.cancellation_requested:
        return "receipt_held"
    if factory_pause.is_card(escalation):
        return "not_routine_kind"
    origin = _origin(row, escalation)
    if origin is None:
        return "unclassified"
    if escalation.get("dispatch_refusal"):
        return "funding_gate"
    direction = json.loads(row.direction_json) if row.direction_json else {}
    if (
        any(key in direction for key in RESTRICTION_KEYS)
        or controls.nonrenewable_recovery(row)
        or (row.task_id and controls.review_recovery_held(row.task_id, session=db))
        or (row.task_id and controls.task_recovery_grant(row.task_id, session=db))
    ):
        return "explicit_hold"
    tasks = [row.task_id, *(direction.get("previous_task_ids") or [])]
    if db.exec(
        select(FactoryAudit.id).where(
            FactoryAudit.action == "human_handoff",
            FactoryAudit.task_id.in_([t for t in tasks if t]),
        )
    ).first():
        return "human_handoff"
    options = escalation.get("options")
    if controls.verify_option_list(options, subject="escalation") is not None:
        return "invalid_options"
    option = options[0]
    if option["effect"] != controls.CONTINUE_EFFECT:
        return "recommendation_not_continue"
    if _sensitive(escalation, option):
        return "sensitive_scope"
    if now is not None:
        updated = row.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if (now - updated).total_seconds() < GRACE_SECONDS:
            return "grace"
    return None


def _prior(db, receipt_id: int) -> list[dict]:
    rows = db.exec(
        select(FactoryAudit.detail_json)
        .where(FactoryAudit.action == APPLIED)
        .order_by(FactoryAudit.id)
    ).all()
    return [d for d in map(json.loads, rows) if d.get("receipt_id") == receipt_id]


def _live(row: FactoryReceipt, escalation: dict) -> tuple[str | None, str | None]:
    """Fresh GitHub evidence: a refusal reason, and the pull request head."""
    from factory.orchestration.factory_conductor import github_get, github_list

    try:
        issue = github_get(row.repo, f"issues/{row.issue_number}")
        if str(issue.get("state") or "").lower() != "open":
            return "issue_closed", None
        labels = {
            (item.get("name", "") if isinstance(item, dict) else str(item)).lower()
            for item in issue.get("labels", [])
        }
        if labels & HUMAN_OWNED_LABELS:
            return "human_owned", None
        if not controls.issue_body_unchanged(
            escalation.get("issue_body_sha256"), row.body, issue.get("body")
        ):
            return "stale_evidence", None
        number = escalation.get("pr_number")
        if type(number) is not int or number <= 0:
            return None, None
        pull = github_get(row.repo, f"pulls/{number}")
        if pull.get("state") not in (None, "open"):
            return "pull_request_closed", None
        head = pull.get("head", {}).get("sha")
        latest: dict[str, str] = {}
        for page in range(1, MAX_REVIEW_PAGES + 1):
            reviews = github_list(
                row.repo, f"pulls/{number}/reviews?per_page=100&page={page}"
            )
            for review in reviews:
                user = (review.get("user") or {}).get("login", "")
                if review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                    latest[user] = review["state"]
            if len(reviews) < 100:
                break
        else:
            return "evidence_unreadable", head
        if "CHANGES_REQUESTED" in latest.values():
            return "review_objection", head
        return None, head
    except Exception:  # noqa: BLE001 - unreadable evidence fails closed
        logger.info("auto-recommendation evidence unreadable for %s", row.id)
        return "evidence_unreadable", None


def _emit_blocker(db, row: FactoryReceipt, key: str, reason: str) -> None:
    """One repair signal per blocker; repair issues never recurse."""
    if REPAIR_MARKER in (row.body or ""):
        return
    for raw in db.exec(
        select(FactoryAudit.detail_json).where(FactoryAudit.action == BLOCKER)
    ).all():
        if json.loads(raw).get("blocker_key") == key:
            return
    controls._audit(
        db,
        ACTOR,
        BLOCKER,
        task_id=row.task_id,
        receipt_id=row.id,
        blocker_key=key,
        reason=reason,
        repo=row.repo,
        issue_number=row.issue_number,
    )


def _note(head: str | None) -> str:
    return NOTE + EVIDENCE + json.dumps({"head": head})


def _head_of(note: str | None) -> str | None:
    try:
        return json.loads((note or "").split(EVIDENCE, 1)[1]).get("head")
    except (IndexError, ValueError, AttributeError):
        return None


def revalidate_claim(db, control, row, escalation, option, note) -> None:
    """Run inside the decision claim, under the control lock.

    Re-screens against the shared restriction owners, requires an enabled
    lane, applies the progress and answer-cap rules, and writes the ledger row
    in the claim's own transaction. A refusal that signals a repeat also
    records the single repair signal before raising.
    """
    from factory.orchestration.factory_decisions import DecisionError

    def refuse(code: str):
        raise DecisionError(409, f"auto:{code}")

    from factory.orchestration.factory_models import FactoryControl

    control = control or db.get(FactoryControl, "factory")
    if control is None or control.state != "enabled":
        refuse("lane_not_enabled")
    if not enabled():
        refuse("disabled")
    reason = screen(db, row, escalation, None)
    if reason is not None:
        refuse(reason)
    if option != escalation["options"][0]:
        refuse("not_the_recommendation")
    origin = _origin(row, escalation)
    key, watermark = failure_identity(db, row, origin)
    head = _head_of(note)
    prior = _prior(db, row.id)
    gate = None
    if len(prior) >= MAX_PER_RECEIPT:
        gate = "auto_limit"
    elif prior and prior[-1]["blocker_key"] == key:
        last = prior[-1]
        moved = head is not None and head != last.get("head_sha")
        if not (moved and watermark > last.get("watermark", 0)):
            gate = "no_progress"
    if gate is not None:
        _emit_blocker(db, row, key, gate)
        refuse(gate)
    controls._audit(
        db,
        ACTOR,
        APPLIED,
        task_id=row.task_id,
        receipt_id=row.id,
        blocker_key=key,
        head_sha=head,
        watermark=watermark,
        origin=origin,
    )


def _consider(row_id: int, now) -> str:
    from factory.orchestration import factory_decisions as decisions

    with controls._read_session() as db:
        row = db.get(FactoryReceipt, row_id)
        escalation = decisions.escalation_of(row)
        reason = screen(db, row, escalation, now)
        if reason is not None:
            return reason
        db.expunge(row)
    reason, head = _live(row, escalation)
    if reason is not None:
        return reason
    fields = decisions._fields(row)
    option = escalation["options"][0]
    with controls._read_session() as db:
        key, watermark = failure_identity(db, row, _origin(row, escalation))
        answered = len(_prior(db, row.id))
    result = decisions.request_decision(
        row.id,
        fields["decision_id"],
        option["key"],
        ACTOR,
        # New server-observed evidence is a new request; identical evidence
        # replays the stored outcome without another GitHub read or effect.
        request_key=f"auto:{fields['decision_id']}:{head}:{watermark}:{answered}",
        note=_note(head),
    )
    if result.get("state") == "refused":
        reason = str(result.get("reason") or "")
        return reason.removeprefix("auto:") if reason.startswith("auto:") else (
            f"refused:{reason}"
        )
    if result.get("state") != "completed":
        # Accepted or outcome-unknown: the request fence owns the outcome.
        return "unconfirmed"
    return "applied"


def tick(state: str | None = None) -> dict[str, int]:
    """Answer at most a few routine cards per interval. Safe to call often."""
    global _last_run
    counts: dict[str, int] = {}
    if not enabled() or state != "enabled":
        return counts
    mark = time.monotonic()
    if _last_run is not None and mark - _last_run < INTERVAL_SECONDS:
        return counts
    _last_run = mark
    now = controls._now()
    with controls._read_session() as db:
        ids = [
            row.id
            for row in db.exec(
                select(FactoryReceipt)
                .where(
                    FactoryReceipt.state == controls.ESCALATED,
                    FactoryReceipt.escalation_json.is_not(None),
                )
                .order_by(FactoryReceipt.updated_at)
            ).all()
        ]
    applied = 0
    for row_id in ids:
        if applied >= MAX_PER_TICK:
            break
        try:
            outcome = _consider(row_id, now)
        except Exception:  # noqa: BLE001 - one card never stops the rest
            logger.exception("auto-recommendation failed for receipt %s", row_id)
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1
        applied += outcome == "applied"
    return counts
