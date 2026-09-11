"""Bounded autonomous GitHub issue intake, inert unless policy enables it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import re

from sqlmodel import select

from swarm.factory_controls import (
    DELIVER,
    REFINE,
    _audit,
    _locked_session,
    _now,
    _read_session,
    intake_policy,
    intake_state,
)
from swarm.factory_intake import receive_issue
from swarm.factory_models import FactoryAudit, FactoryReceipt

logger = logging.getLogger(__name__)

ACTOR = "factory:intake"
PAGE_SIZE = 100
MAX_PAGES = 5
CANDIDATE_EVIDENCE_LIMIT = 20
IDLE_AUDIT_SECONDS = 3600
RANK_LABELS = ("critical", "bug")
_EXCLUSION_REASONS = (
    "pull_request",
    "not_open",
    "assigned",
    "excluded_label",
    "linked_pr",
    "cooldown",
    "already_received",
    "refine_disabled",
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def github_list(repo: str, suffix: str) -> list:
    """Bounded list read, imported lazily so the board stays off the reconciler.

    factory_controls.status reads the intake block, and the board reads status,
    so a module-scope import here would pull the whole conductor into a page
    render. Keeping the name here also leaves tests one seam to fake.
    """
    from swarm.factory_conductor import github_list as read

    return read(repo, suffix)


def _pages(repo: str, endpoint: str) -> list:
    result = []
    for page in range(1, MAX_PAGES + 1):
        rows = github_list(
            repo, f"{endpoint}?state=open&per_page={PAGE_SIZE}&page={page}"
        )
        result.extend(rows)
        if len(rows) < PAGE_SIZE:
            break
    return result


def _label_names(issue: dict) -> set[str]:
    names = set()
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.add(name.lower())
    return names


def _created_rank(issue: dict) -> float:
    value = issue.get("created_at")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass
    return float(issue["number"])


def _idle(detail: dict) -> None:
    cutoff = _now() - timedelta(seconds=IDLE_AUDIT_SECONDS)
    with _locked_session() as (db, _control):
        last = db.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action == "intake_idle")
            .order_by(FactoryAudit.id.desc())
        ).first()
        if last is not None and _aware(last.created_at) >= cutoff:
            return
        _audit(db, ACTOR, "intake_idle", **detail)


def intake_tick(policy: dict, *, generation: int) -> dict | None:
    """Admit at most one issue the operator never named, or audit why not."""
    try:
        intake = intake_policy(policy)
        if not intake["enabled"]:
            return None
        now = _now()
        today = now - timedelta(hours=24)
        with _locked_session() as (db, _control):
            busy = db.exec(
                select(FactoryReceipt.id).where(
                    FactoryReceipt.generation == generation,
                    FactoryReceipt.state.in_(("queued", "admitted", "uncertain")),
                )
            ).first()
            if busy is not None:
                return None
            admitted_today = len(
                db.exec(
                    select(FactoryAudit.id).where(
                        FactoryAudit.action == "intake_admitted",
                        FactoryAudit.created_at >= today,
                    )
                ).all()
            )
        if admitted_today >= intake["max_per_day"]:
            _idle(
                {
                    "reason": "daily_cap",
                    "admitted_today": admitted_today,
                    "max_per_day": intake["max_per_day"],
                }
            )
            return None

        repo = policy["repo"]
        issues = _pages(repo, "issues")
        pulls = _pages(repo, "pulls")
        linked: set[int] = set()
        for pull in pulls:
            if not isinstance(pull, dict):
                continue
            for field in ("title", "body"):
                text = pull.get(field)
                if not isinstance(text, str):
                    continue
                for match in re.findall(r"#(\d+)", text):
                    number = int(match)
                    if 1 <= number <= 2**31 - 1:
                        linked.add(number)

        excluded: dict[str, int] = {reason: 0 for reason in _EXCLUSION_REASONS}

        def exclude(reason: str) -> None:
            excluded[reason] += 1

        include_labels = {label.lower() for label in intake["labels"]}
        exclude_labels = {label.lower() for label in intake["exclude_labels"]}
        survivors = []
        for item in issues:
            if not isinstance(item, dict):
                exclude("not_open")
                continue
            labels = _label_names(item)
            if "pull_request" in item:
                exclude("pull_request")
            elif item.get("state") != "open":
                exclude("not_open")
            elif item.get("assignees"):
                exclude("assigned")
            elif labels & exclude_labels:
                exclude("excluded_label")
            elif item.get("number") in linked:
                exclude("linked_pr")
            else:
                survivors.append((item, labels))

        receipt_rows = []
        numbers = [
            item.get("number")
            for item, _labels in survivors
            if type(item.get("number")) is int and 1 <= item["number"] <= 2**31 - 1
        ]
        if numbers:
            with _read_session() as db:
                receipt_rows = db.exec(
                    select(FactoryReceipt)
                    .where(
                        FactoryReceipt.repo == repo,
                        FactoryReceipt.issue_number.in_(numbers),
                    )
                    .order_by(FactoryReceipt.issue_number, FactoryReceipt.id.desc())
                ).all()
        by_number: dict[int, list[FactoryReceipt]] = {}
        for row in receipt_rows:
            by_number.setdefault(row.issue_number, []).append(row)

        candidates = []
        cooldown_cutoff = now - timedelta(hours=intake["cooldown_hours"])
        for item, labels in survivors:
            number = item.get("number")
            rows = by_number.get(number, []) if type(number) is int else []
            latest = rows[0] if rows else None
            if (
                latest is not None
                and latest.state in ("failed", "cancelled")
                and _aware(latest.updated_at) >= cooldown_cutoff
            ):
                exclude("cooldown")
                continue
            if any(row.generation == generation for row in rows):
                exclude("already_received")
                continue
            delivery = bool(labels & include_labels)
            if not delivery and not intake["refine_enabled"]:
                exclude("refine_disabled")
                continue
            if type(number) is not int or not 1 <= number <= 2**31 - 1:
                exclude("not_open")
                continue
            kind = DELIVER if delivery else REFINE
            label_rank = next(
                (index for index, label in enumerate(RANK_LABELS) if label in labels),
                len(RANK_LABELS),
            )
            rank_reason = (
                RANK_LABELS[label_rank] if label_rank < len(RANK_LABELS) else "oldest"
            )
            candidates.append(
                {
                    "issue": item,
                    "number": number,
                    "kind": kind,
                    "rank_reason": rank_reason,
                    "sort": (
                        0 if kind == DELIVER else 1,
                        label_rank,
                        _created_rank(item),
                        number,
                    ),
                }
            )
        candidates.sort(key=lambda candidate: candidate["sort"])
        excluded = {reason: count for reason, count in excluded.items() if count}
        if not candidates:
            _idle({"excluded": excluded, "listed": len(issues)})
            return None

        chosen = candidates[0]
        issue = chosen["issue"]
        received = receive_issue(
            repo,
            chosen["number"],
            issue.get("title"),
            issue.get("body") or "",
            issue.get("html_url"),
            ACTOR,
            generation=generation,
            kind=chosen["kind"],
        )
        with _locked_session() as (db, _control):
            _audit(
                db,
                ACTOR,
                "intake_admitted",
                receipt_id=received["receipt"]["id"],
                issue_number=chosen["number"],
                kind=chosen["kind"],
                rank_reason=chosen["rank_reason"],
                candidates=[
                    {
                        "number": candidate["number"],
                        "kind": candidate["kind"],
                        "rank_reason": candidate["rank_reason"],
                    }
                    for candidate in candidates[:CANDIDATE_EVIDENCE_LIMIT]
                ],
                excluded=excluded,
                admitted_today=admitted_today + 1,
            )
        return received
    except Exception:  # noqa: BLE001 - intake is optional and never stops the lane
        logger.exception("factory autonomous intake failed")
        return None


# Re-exported so a caller reading the loop finds the board's view of it here.
__all__ = ["intake_tick", "intake_state", "github_list", "ACTOR"]
