"""Which factory notifications page the operator, and the daily digest.

Every human-needed notice goes through ``factory_conductor._notify_person_once``
with a kind. With ``FACTORY_NOTIFY_DIGEST_ENABLED`` only the kinds that need a
person's authority page Discord at once; the rest are recorded as
``task_needs_person_digested`` and sent once a day in one message, together
with what the factory decided on its own (merit funding grants, watchdog
resumes, live checks handed off). Decision cards are untouched: every one of
them stays on the escalations page whether it paged or not.

What pages:

- ``escalation``: a delivery decision card (a planner pause, a funding
  question the judge would not take or a cap blocked, a branch another owner
  holds). The planner charter reserves pause for human authority.
- ``watchdog:*``: a looping verdict, or two unreadable answers.
- ``landing``: a pull request left for a person to merge.
- ``rollout``: a merged delivery still blocked on rollout after 60 minutes.
- ``intervention`` and ``deadline``, but only while nothing will release the
  slot on its own. With active cessation or the deadline backstop on, the
  factory settles these without a person (every one of the 24 intervention
  pages in the week to 2026-10-01 was settled automatically), so they digest.

What digests: ``refine`` needs-human briefs (a one-minute scoping question on
advisory work, not authority) and the self-releasing kinds above.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlmodel import select

logger = logging.getLogger(__name__)

ENABLED_ENV = "FACTORY_NOTIFY_DIGEST_ENABLED"
DIGESTED = "task_needs_person_digested"
SENT = "factory_digest_sent"
FAILED = "factory_digest_failed"
ACTOR = "factory:digest"
DIGEST_INTERVAL = timedelta(hours=24)
RETRY_AFTER = timedelta(hours=1)
SECTION_LIMIT = 8
MESSAGE_LIMIT = 1900
DIGEST_KINDS = frozenset({"refine"})
SELF_RELEASING_KINDS = frozenset({"intervention", "deadline"})


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "false").lower() == "true"


def automatic_release() -> bool:
    """Whether a stranded attempt is settled without a person."""
    from factory.orchestration import factory_cessation
    from factory.orchestration.factory_conductor import _deadline_backstop_enabled

    return factory_cessation.enabled() or _deadline_backstop_enabled()


def pages(kind: str) -> bool:
    """Whether a notice of this kind should reach Discord immediately."""
    if not enabled():
        return True
    if kind in DIGEST_KINDS:
        return False
    if kind in SELF_RELEASING_KINDS:
        return not automatic_release()
    return True


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _latest(db, action):
    from factory.orchestration.factory_models import FactoryAudit

    return db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action == action)
        .order_by(FactoryAudit.id.desc())
    ).first()


def _items(db, watermark: int) -> list:
    from factory.orchestration.factory_models import FactoryAudit

    return db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.id > watermark,
            FactoryAudit.action.in_(
                (
                    DIGESTED,
                    "funding_granted",
                    "watchdog_assessed",
                    "repository_scope_delivered",
                    "rollout_wait_reported",
                )
            ),
        )
        .order_by(FactoryAudit.id)
    ).all()


def _detail(row) -> dict:
    try:
        value = json.loads(row.detail_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def sections(rows) -> dict[str, list[str]]:
    """The digest lines, by section, from audit rows past the watermark."""
    out: dict[str, list[str]] = {
        "funded": [],
        "resumed": [],
        "handed_off": [],
        "rollout_waits": [],
        "notices": [],
    }
    for row in rows:
        detail = _detail(row)
        task = row.task_id or "?"
        if row.action == "funding_granted":
            # Only the judge's own merit grants are auto-approvals; a dispatch
            # refusal grant is an operator's answer, already on a card.
            if not detail.get("merit_ceiling"):
                continue
            requested = detail.get("requested_task_budget_usd")
            granted = detail.get("granted_task_budget_usd")
            reason = " ".join(str(detail.get("reason") or "").split())[:120]
            out["funded"].append(
                f"{task}: task budget ${granted:g} (asked ${requested:g}). {reason}"
                if isinstance(granted, (int, float))
                and isinstance(requested, (int, float))
                else f"{task}: {reason}"
            )
        elif row.action == "watchdog_assessed":
            if detail.get("verdict") != "progressing" or detail.get("short_circuit"):
                continue
            spend = detail.get("spend_usd")
            reason = " ".join(str(detail.get("reason") or "").split())[:120]
            out["resumed"].append(
                f"{task} at ${spend:.2f}: {reason}"
                if isinstance(spend, (int, float))
                else f"{task}: {reason}"
            )
        elif row.action == "repository_scope_delivered":
            issue = detail.get("issue_number")
            child = detail.get("child_number") or detail.get("tracked_in")
            prs = ", ".join(f"#{pr}" for pr in detail.get("delivered_prs") or [])
            target = f", live checks in #{child}" if child else ""
            out["handed_off"].append(f"#{issue} delivered in {prs}{target}")
        elif row.action == "rollout_wait_reported":
            values = {
                key: detail.get(key) if detail.get(key) is not None else "unknown"
                for key in (
                    "pr_number",
                    "issue_number",
                    "waited_minutes",
                    "application",
                    "resource",
                    "reason",
                )
            }
            out["rollout_waits"].append(
                f"#{values['pr_number']} (issue #{values['issue_number']}, {task}): "
                f"{values['waited_minutes']}m on {values['application']} "
                f"{values['resource']} ({values['reason']})"
            )
        elif row.action == DIGESTED:
            message = " ".join(str(detail.get("message") or "").split())[:160]
            out["notices"].append(f"[{detail.get('kind')}] {message}")
    return out


TITLES = {
    "funded": "Funding the judge approved",
    "resumed": "Watchdog let continue",
    "handed_off": "Repository work delivered, live checks handed off",
    "rollout_waits": "Landings waiting on rollout",
    "notices": "Notices that did not need you at once",
}


def compose(groups: dict[str, list[str]]) -> str | None:
    """One Discord message, bounded, or None when there is nothing to say."""
    from factory.orchestration.factory_refine import ESCALATIONS_URL

    total = sum(len(lines) for lines in groups.values())
    if not total:
        return None
    parts = [f"Factory daily digest: {total} item(s) the factory handled or deferred."]
    for key, title in TITLES.items():
        lines = groups[key]
        if not lines:
            continue
        parts.append(f"\n{title} ({len(lines)}):")
        parts.extend(f"- {line}" for line in lines[:SECTION_LIMIT])
        if len(lines) > SECTION_LIMIT:
            parts.append(f"- and {len(lines) - SECTION_LIMIT} more")
    tail = f"\nOpen cards: {ESCALATIONS_URL}"
    body = "\n".join(parts)
    if len(body) + len(tail) > MESSAGE_LIMIT:
        body = body[: MESSAGE_LIMIT - len(tail) - 2].rstrip() + "\n…"
    return body + tail


def digest_tick() -> str:
    """Send at most one digest a day. Returns what it did."""
    if not enabled():
        return "disabled"
    from factory.orchestration.factory_controls import (
        _audit,
        _locked_session,
        _read_session,
    )
    from factory.orchestration.factory_models import FactoryAudit

    now = _now()
    with _read_session() as db:
        last = _latest(db, SENT)
        failed = _latest(db, FAILED)
        if last is None:
            # First run: start the window a day back rather than replaying
            # the whole audit history.
            floor = db.exec(
                select(FactoryAudit.id)
                .where(FactoryAudit.created_at < now - DIGEST_INTERVAL)
                .order_by(FactoryAudit.id.desc())
            ).first()
            watermark = floor or 0
        else:
            if _aware(last.created_at) > now - DIGEST_INTERVAL:
                return "not_due"
            watermark = _detail(last).get("watermark", 0)
        if (
            failed is not None
            and (last is None or failed.id > last.id)
            and _aware(failed.created_at) > now - RETRY_AFTER
        ):
            return "retry_wait"
        rows = _items(db, watermark)
        newest = max((row.id for row in rows), default=watermark)
        groups = sections(rows)
    message = compose(groups)
    counts = {key: len(lines) for key, lines in groups.items()}
    if message is not None:
        try:
            import asyncio

            from agent.api import notify

            asyncio.run(notify(message, level="info"))
        except Exception as exc:
            logger.warning("factory digest failed", exc_info=True)
            with _locked_session() as (db, _control):
                _audit(db, ACTOR, FAILED, error=type(exc).__name__, counts=counts)
            return "failed"
    with _locked_session() as (db, _control):
        _audit(
            db,
            ACTOR,
            SENT,
            watermark=newest,
            counts=counts,
            sent=message is not None,
        )
    return "sent" if message is not None else "empty"
