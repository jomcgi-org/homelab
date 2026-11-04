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
# Text that routes a card to a person: access and secrets, destructive scope.
SENSITIVE = re.compile(
    r"secret|credential|token|password|api[ -]?key|security|vulnerab|exploit"
    r"|rbac|\biam\b|1password|oauth|destructive|\bdelete|\bdrop\b|force[- ]push"
    r"|rm -rf|truncate|\bwipe|\bpurge|human[- ]owned|\bdo not (merge|deploy)",
    re.IGNORECASE,
)

NOTE = (
    "Applied the validated recommendation: no hold, access or destructive "
    "scope, and no open review objection."
)

_last_run: float | None = None


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "false").lower() == "true"


def _norm(text: object) -> str:
    return " ".join(str(text or "").lower().split())


def blocker_key(row: FactoryReceipt, escalation: dict) -> str:
    """Stable identity of the problem a card asks about, not of one task."""
    raw = f"{row.repo}#{row.issue_number}:{_norm(escalation.get('question'))}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _sensitive(escalation: dict, option: dict) -> bool:
    text = " ".join(
        str(part or "")
        for part in (
            escalation.get("question"),
            escalation.get("reason"),
            option.get("label"),
            json.dumps(option.get("detail") or {}),
        )
    )
    return SENSITIVE.search(text) is not None


def screen(row: FactoryReceipt, escalation: dict | None, now) -> str | None:
    """Local, read-free reasons a card is not routine. None means it may pass."""
    from factory.orchestration import factory_pause

    if not isinstance(escalation, dict) or escalation.get("resolved") is not None:
        return "not_open"
    if row.state != controls.ESCALATED or row.task_paused or row.cancellation_requested:
        return "receipt_held"
    if factory_pause.is_card(escalation) or escalation.get("kind") not in (
        "delivery",
        "refine",
        None,
    ):
        return "not_routine_kind"
    if escalation.get("dispatch_refusal"):
        return "funding_gate"
    direction = json.loads(row.direction_json) if row.direction_json else {}
    if direction.get("landing_hold") or direction.get("model_restriction"):
        return "explicit_hold"
    options = escalation.get("options")
    if controls.verify_option_list(options, subject="escalation") is not None:
        return "invalid_options"
    option = options[0]
    if option["effect"] != controls.CONTINUE_EFFECT:
        return "recommendation_not_continue"
    if _sensitive(escalation, option):
        return "sensitive_scope"
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
    details = [json.loads(raw) for raw in rows]
    return [d for d in details if d.get("receipt_id") == receipt_id]


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
        head = github_get(row.repo, f"pulls/{number}").get("head", {}).get("sha")
        latest: dict[str, str] = {}
        for review in github_list(row.repo, f"pulls/{number}/reviews?per_page=100"):
            user = (review.get("user") or {}).get("login", "")
            if review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[user] = review["state"]
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


def _consider(row_id: int, now) -> str:
    from factory.orchestration import factory_decisions as decisions

    with controls._read_session() as db:
        row = db.get(FactoryReceipt, row_id)
        escalation = decisions.escalation_of(row)
        reason = screen(row, escalation, now)
        if reason is not None:
            return reason
        db.expunge(row)
    key = blocker_key(row, escalation)
    reason, head = _live(row, escalation)
    if reason is not None:
        return reason
    with controls._locked_session() as (db, _control):
        prior = _prior(db, row.id)
        gate = None
        if len(prior) >= MAX_PER_RECEIPT:
            gate = "auto_limit"
        elif prior and prior[-1]["blocker_key"] == key and (
            head is None or prior[-1].get("head_sha") == head
        ):
            gate = "no_progress"
        if gate is not None:
            _emit_blocker(db, row, key, gate)
            return gate
    fields = decisions._fields(row)
    option = escalation["options"][0]
    result = decisions.request_decision(
        row.id,
        fields["decision_id"],
        option["key"],
        ACTOR,
        request_key=f"auto:{fields['decision_id']}",
        note=NOTE,
    )
    if result.get("state") == "refused":
        return f"refused:{result.get('reason')}"
    with controls._locked_session() as (db, _control):
        controls._audit(
            db,
            ACTOR,
            APPLIED,
            task_id=row.task_id,
            receipt_id=row.id,
            decision_id=fields["decision_id"],
            blocker_key=key,
            head_sha=head,
            state=result.get("state"),
        )
    return "applied"


def tick() -> dict[str, int]:
    """Answer at most a few routine cards per interval. Safe to call often."""
    global _last_run
    counts: dict[str, int] = {}
    if not enabled():
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
