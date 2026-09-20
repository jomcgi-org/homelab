"""First-pass verdict feedback and retired class routing.

Delivery and historical advisory samples stay in separate, per-class windows.
Delivery classes always route to delivery, and admission heals stored advisory
routes. Explicit advisory classes retain their dedicated review path.

The demotion gate is deliberately retired, not merely relaxed. It measured
first-pass approval, which is the input to a correction loop rather than its
outcome, and its recovery epoch sampled a different model than the one it
gated, so a demoted class could never earn its way back. The windows and the
decisions below are kept for reporting, and the board and the admission audit
still surface them, but no decision moves a class off the delivery tier.

Because store_class_route can no longer write an advisory row, the
class_tier_demoted branch in factory_intake is dead. The recovery window and
delivery_rejections remain as historical reporting fields. Issue 6283 tracks
whether automatic routing returns and on what signal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlmodel import Session, select

from factory.orchestration.factory_controls import (
    ADVISORY_CLASSES,
    _audit,
    _locked_session,
    _read_session,
    receipt_task_class,
)
from factory.orchestration.factory_models import (
    FactoryClassTier,
    FactoryReceipt,
    FactoryReviewVerdict,
)

ACTOR = "factory:feedback"
DELIVERY_TIER = "delivery"
ADVISORY_TIER = "advisory"
ROUTING_TIERS = (DELIVERY_TIER, ADVISORY_TIER)
WINDOW_SIZE = 20
APPROVAL_FLOOR = 0.60
RECENT_REJECTION_LIMIT = 5
SUMMARY_CHARS = 2_000
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _artifact(run: dict) -> dict:
    try:
        outcome = json.loads(run.get("outcome_json") or "{}")
    except (TypeError, ValueError):
        return {}
    value = outcome.get("value") or outcome.get("artifact") or {}
    return value if isinstance(value, dict) else {}


def _first_review(runs: list[dict]) -> dict | None:
    """Earliest completed review invocation, excluding zero-work denials."""
    candidates = [
        run
        for run in runs
        if str(run.get("node_key") or "").startswith("review_")
        and run.get("status") in ("succeeded", "failed", "escalated", "cancelled")
        and isinstance(run.get("id"), int)
        and isinstance(run.get("session_id"), int)
        and not run.get("capacity_denied")
    ]
    return min(candidates, key=lambda run: run["id"]) if candidates else None


def _recipe_run(runs: list[dict], review: dict) -> dict | None:
    matches = [
        run
        for run in runs
        if str(run.get("node_key") or "").startswith("conductor_")
        and run.get("status") == "succeeded"
        and isinstance(run.get("id"), int)
        and run["id"] < review["id"]
    ]
    return max(matches, key=lambda run: run["id"]) if matches else None


def _classified_verdict(run: dict) -> tuple[str, str, str | None]:
    artifact = _artifact(run)
    if run.get("status") == "succeeded":
        verdict = artifact.get("verdict")
        if verdict not in ("approve", "changes_requested"):
            verdict = "unparseable"
    else:
        verdict = "blocked"
    summary = str(artifact.get("summary") or "")[:SUMMARY_CHARS]
    head = artifact.get("head_sha")
    return verdict, summary, head if isinstance(head, str) else None


def _verdict_dict(row: FactoryReviewVerdict) -> dict:
    reviewed_at = row.reviewed_at
    if reviewed_at.tzinfo is None:
        reviewed_at = reviewed_at.replace(tzinfo=timezone.utc)
    return {
        "id": row.id,
        "task_id": row.task_id,
        "review_run_id": row.review_run_id,
        "recipe_run_id": row.recipe_run_id,
        "task_class": row.task_class,
        "sample_kind": row.sample_kind,
        "verdict": row.verdict,
        "summary": row.summary,
        "head_sha": row.head_sha,
        "reviewed_at": reviewed_at.isoformat(),
    }


def record_first_pass(
    task_id: str,
    runs: list[dict],
) -> dict | None:
    """Persist one first-pass sample for a delivery task.

    Review retries cannot replace the earliest completed invocation. A failed
    invocation is blocked and a malformed successful artifact is unparseable,
    so neither can disappear from the rejection denominator or become approval.
    """
    if not any(
        str(run.get("node_key") or "").startswith("review_")
        and run.get("status") in ("succeeded", "failed", "escalated", "cancelled")
        and isinstance(run.get("session_id"), int)
        and not run.get("capacity_denied")
        for run in runs
    ):
        return None
    with _locked_session() as (db, _control):
        existing = db.exec(
            select(FactoryReviewVerdict).where(FactoryReviewVerdict.task_id == task_id)
        ).first()
        if existing is not None:
            return _verdict_dict(existing)
        receipt = db.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task_id)
        ).first()
        if receipt is None:
            return None
        task_class = receipt_task_class(receipt)
        if task_class in ADVISORY_CLASSES:
            return None
        sample_kind = DELIVERY_TIER
        review = _first_review(runs)
        if review is None:
            return None
        recipe = _recipe_run(runs, review)
        verdict, summary, head_sha = _classified_verdict(review)
        reviewed_at = review.get("finished_at") or review.get("created_at")
        if not isinstance(reviewed_at, datetime):
            reviewed_at = datetime.now(timezone.utc)
        row = FactoryReviewVerdict(
            task_id=task_id,
            review_run_id=review["id"],
            recipe_run_id=None if recipe is None else recipe["id"],
            task_class=task_class,
            sample_kind=sample_kind,
            verdict=verdict,
            summary=summary,
            head_sha=head_sha,
            reviewed_at=reviewed_at,
        )
        db.add(row)
        db.flush()
        _audit(
            db,
            ACTOR,
            "first_pass_verdict_recorded",
            task_id=task_id,
            verdict_id=row.id,
            review_run_id=row.review_run_id,
            recipe_run_id=row.recipe_run_id,
            task_class=task_class,
            sample_kind=sample_kind,
            verdict=verdict,
        )
        return _verdict_dict(row)


def _tier(db: Session, task_class: str) -> FactoryClassTier | None:
    return db.get(FactoryClassTier, task_class)


def _window(
    db: Session, task_class: str, sample_kind: str, since: datetime | None
) -> list[FactoryReviewVerdict]:
    query = select(FactoryReviewVerdict).where(
        FactoryReviewVerdict.task_class == task_class,
        FactoryReviewVerdict.sample_kind == sample_kind,
    )
    if since is not None:
        query = query.where(FactoryReviewVerdict.reviewed_at >= since)
    return list(
        db.exec(
            query.order_by(
                FactoryReviewVerdict.reviewed_at.desc(),
                FactoryReviewVerdict.id.desc(),
            ).limit(WINDOW_SIZE)
        ).all()
    )


def _rate_view(rows: list[FactoryReviewVerdict]) -> dict:
    approvals = sum(row.verdict == "approve" for row in rows)
    count = len(rows)
    rejections = count - approvals
    return {
        "sample_count": count,
        "approval_count": approvals,
        "rejection_count": rejections,
        "approval_rate": approvals / count if count else None,
        "rejection_rate": rejections / count if count else None,
    }


def empty_feedback(task_class: str) -> dict:
    tier = ADVISORY_TIER if task_class in ADVISORY_CLASSES else DELIVERY_TIER
    return {
        "task_class": task_class,
        "tier": tier,
        "previous_tier": tier,
        "decision": "class_floor"
        if task_class in ADVISORY_CLASSES
        else "insufficient_samples",
        "sample_kind": tier,
        "window_size": WINDOW_SIZE,
        "sample_count": 0,
        "approval_count": 0,
        "rejection_count": 0,
        "approval_rate": None,
        "rejection_rate": None,
        "approval_floor": APPROVAL_FLOOR,
        "recent_rejections": [],
        "delivery_window": _rate_view([]),
        "recovery_window": _rate_view([]),
        "delivery_rejections": [],
    }


def feedback_for_class(task_class: str, *, session: Session | None = None) -> dict:
    """Return the current isolated quality window and its implied route."""
    if task_class in ADVISORY_CLASSES:
        return empty_feedback(task_class)
    with _read_session(session) as db:
        state = _tier(db, task_class)
        previous = state.routing_tier if state is not None else DELIVERY_TIER
        since = state.transitioned_at if state is not None else None
        rows = _window(db, task_class, previous, since)
        active = _rate_view(rows)
        delivery_rows = _window(db, task_class, DELIVERY_TIER, None)
        recovery_rows = rows if previous == ADVISORY_TIER else []
        count = active["sample_count"]
        rate = active["approval_rate"]
        if previous == ADVISORY_TIER:
            tier, decision = DELIVERY_TIER, "advisory_retired"
        elif count < WINDOW_SIZE:
            tier, decision = DELIVERY_TIER, "insufficient_samples"
        elif rate is not None and rate < APPROVAL_FLOOR:
            tier, decision = DELIVERY_TIER, "below_floor"
        elif rate == APPROVAL_FLOOR:
            tier, decision = DELIVERY_TIER, "at_floor_hold"
        else:
            tier, decision = DELIVERY_TIER, "quality_holds"
        return {
            "task_class": task_class,
            "tier": tier,
            "previous_tier": previous,
            "decision": decision,
            "sample_kind": previous,
            "window_size": WINDOW_SIZE,
            "sample_count": count,
            "approval_count": active["approval_count"],
            "rejection_count": active["rejection_count"],
            "approval_rate": rate,
            "rejection_rate": active["rejection_rate"],
            "approval_floor": APPROVAL_FLOOR,
            "recent_rejections": [
                _verdict_dict(row) for row in rows if row.verdict != "approve"
            ][:RECENT_REJECTION_LIMIT],
            # The delivery ledger remains visible while a separate advisory
            # epoch earns recovery. Routing reads the top-level active window.
            "delivery_window": _rate_view(delivery_rows),
            "recovery_window": _rate_view(recovery_rows),
            "delivery_rejections": [
                _verdict_dict(row)
                for row in delivery_rows
                if previous == ADVISORY_TIER and row.verdict != "approve"
            ][:RECENT_REJECTION_LIMIT],
        }


def route_for_class(task_class: str, *, session: Session | None = None) -> dict:
    return feedback_for_class(task_class, session=session)


def store_class_route(task_class: str, feedback: dict, *, session: Session) -> None:
    """Persist the route applied at admission and start a fresh window on change."""
    if task_class in ADVISORY_CLASSES:
        return
    route = feedback.get("tier")
    if route not in ROUTING_TIERS:
        raise ValueError("invalid factory class routing tier")
    now = datetime.now(timezone.utc)
    row = _tier(session, task_class)
    if row is None:
        row = FactoryClassTier(
            task_class=task_class,
            routing_tier=route,
            transitioned_at=now if route == ADVISORY_TIER else EPOCH,
            updated_at=now,
        )
    elif row.routing_tier != route:
        row.routing_tier = route
        row.transitioned_at = now
        row.updated_at = now
    else:
        row.updated_at = now
    session.add(row)


__all__ = [
    "ADVISORY_TIER",
    "APPROVAL_FLOOR",
    "DELIVERY_TIER",
    "WINDOW_SIZE",
    "empty_feedback",
    "feedback_for_class",
    "record_first_pass",
    "route_for_class",
    "store_class_route",
]
