"""No-progress observations feed the funding judge; historical holds stay owned.

This adapter counts settled turns and new delivery evidence. New crossings use
the durable funding review, its single decision function and five-minute retry
wait. It never runs another model or creates another watchdog card. Legacy
cards still require their recorded operator decision, even after a delivery.
"""

from __future__ import annotations

import json
import re

from sqlmodel import select

from factory.orchestration.turn_artifact import run_artifact
from factory.orchestration import graph
from factory.orchestration import factory_controls as controls
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt

ACTOR = "factory:watchdog"
KIND = "watchdog"
ASSESSED = "watchdog_assessed"
CLEARED = "funding_progress_cleared"
LEGACY_CLEARED = "watchdog_cleared"
ENACT_REFUSED = "funding_legacy_hold_refused"
PROGRESSING = "progressing"
LOOPING = "looping"
_SHA = re.compile(r"^[0-9a-f]{40}$")

RESUME_OPTION = "resume"
STOP_OPTION = "stop"
RESCOPE_OPTION = "rescope"


def _trail(db, task_id: str) -> list:
    return list(
        db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action.in_(
                    (
                        ASSESSED,
                        CLEARED,
                        LEGACY_CLEARED,
                        "funding_review_settled",
                        "funding_progress_rearmed",
                        "pause_task",
                        "resume_task",
                        "conductor_rejected",
                        "dispatch_refused",
                    )
                ),
            )
            .order_by(FactoryAudit.id)
        ).all()
    )


def state(trail: list, threshold: int) -> dict:
    """Where the watchdog stands, read from its own audit rows.

    ``next_threshold_turns`` is the turn count that triggers the next
    assessment, or None when the latest row predates turn counting and the
    watchdog must re-arm,
    ``run_watermark`` the last node run already accounted for, and
    ``audit_watermark`` the last audit row, so refusals are counted since the
    same point. ``open`` is the looping assessment still waiting on a person.
    """
    result = {
        "next_threshold_turns": threshold,
        "run_watermark": 0,
        "audit_watermark": 0,
        "open": None,
        "assessment_cost_usd": 0.0,
        "last": None,
    }
    for row in trail:
        if row.action not in (
            ASSESSED,
            CLEARED,
            LEGACY_CLEARED,
            "funding_review_settled",
            "funding_progress_rearmed",
        ):
            continue
        detail = controls.audit_detail(row)
        if row.action == "funding_progress_rearmed":
            if result["open"] is None:
                result.update(
                    next_threshold_turns=detail["next_threshold_turns"],
                    run_watermark=detail["run_watermark"],
                    audit_watermark=row.id,
                    last={"id": row.id, "action": row.action, **detail},
                )
            continue
        if row.action == "funding_review_settled":
            checkpoint = detail.get("progress")
            if checkpoint and not any(
                (
                    result["open"],
                    detail.get("refusal"),
                    detail.get("escalated"),
                    detail.get("budget_approval_required"),
                )
            ):
                result.update(checkpoint)
                result["last"] = {"id": row.id, "action": row.action, **detail}
            continue
        result["assessment_cost_usd"] += float(detail.get("cost_usd") or 0.0)
        result["last"] = {"id": row.id, "action": row.action, **detail}
        if row.action in (CLEARED, LEGACY_CLEARED):
            result.update(
                next_threshold_turns=detail.get("next_threshold_turns"),
                run_watermark=detail["run_watermark"],
                audit_watermark=row.id,
                open=None,
            )
        elif detail.get("verdict") == LOOPING:
            result["open"] = {"id": row.id, **detail}
        else:
            result["next_threshold_turns"] = detail.get("next_threshold_turns")
    return result


def turns(db, task_id: str) -> int:
    """Settled turns, work and planner alike.

    A start still reserved is an attempt in flight and has not finished its
    turn, so counting it would trip the watchdog on the dispatch itself.
    """
    settled = [s for s in controls._starts(db, task_id) if s.status != "reserved"]
    used = controls._accounting(settled)
    return used["turns_used"] + used["planner_turns_used"]


def _delivery_marks(run: dict) -> set[str]:
    """The commits and pull requests one attempt reports having produced."""
    artifact = run_artifact(run)
    marks = set()
    for sha in (artifact.get("head_sha"), run.get("head_sha")):
        if isinstance(sha, str) and _SHA.fullmatch(sha) and sha != run.get("base_sha"):
            marks.add(f"sha:{sha}")
    number = artifact.get("pr_number")
    if type(number) is int and number > 0:
        marks.add(f"pr:{number}")
    return marks


def delivered_since(runs: list[dict], watermark: int) -> list[str]:
    """New commits or pull requests reported after ``watermark``.

    The same signal the escalation card and task detail read a pull request
    from (a succeeded attempt's artifact), widened to the pushed head: a mark
    is new when no attempt at or before the watermark reported it.
    """
    seen = set()
    for run in runs:
        if run["id"] <= watermark:
            seen |= _delivery_marks(run)
    fresh = []
    for run in sorted(runs, key=lambda item: item["id"]):
        if run["id"] <= watermark or run["status"] != "succeeded":
            continue
        for mark in sorted(_delivery_marks(run) - seen):
            fresh.append(mark)
            seen.add(mark)
    return fresh


def _record(action: str, task_id: str, **detail) -> None:
    with controls._locked_session() as (db, _control):
        controls._audit(db, ACTOR, action, task_id=task_id, **detail)


def _clear(task_id: str, reason: str, used: int, threshold: int, runs, **extra):
    _record(
        CLEARED,
        task_id,
        reason=reason,
        turns_used=used,
        next_threshold_turns=used + threshold,
        run_watermark=max((r["id"] for r in runs), default=0),
        threshold_turns=threshold,
        **extra,
    )


def _resolve_card(task_id: str, actor: str, note: str) -> None:
    """Mark the open watchdog card answered when the task was resumed directly."""
    with controls._locked_session() as (db, _control):
        row = controls._receipt(db, task_id)
        document = (
            json.loads(row.escalation_json) if row and row.escalation_json else None
        )
        if (
            not isinstance(document, dict)
            or document.get("kind") != KIND
            or document.get("task_id") != task_id
            or controls.terminal_resolution(document.get("resolved"))
        ):
            return
        option = document["options"][0]
        document["resolved"] = {
            "option_key": option["key"],
            "label": option["label"],
            "effect": option["effect"],
            "actor": actor,
            "note": note,
            "effects": {"resumed": True},
            "decided_at": controls._now().isoformat(),
            "decision_id": controls.decision_identity(
                {
                    "id": row.id,
                    "repo": row.repo,
                    "generation": row.generation,
                    "escalation": json.loads(row.escalation_json),
                }
            ),
        }
        row.escalation_json = json.dumps(document)
        row.updated_at = controls._now()
        db.add(row)


def _enact(task: dict, open_: dict, receipt, trail, used, threshold, runs) -> str:
    """Carry out what a person decided about an open watchdog pause."""
    task_id = task["id"]
    pause_id = max(
        (r.id for r in trail if r.action == "pause_task" and r.actor == ACTOR),
        default=open_["id"],
    )
    resumed = next(
        (
            r
            for r in trail
            if r.action == "resume_task"
            and r.id > pause_id
            and controls.audit_detail(r).get("ok")
        ),
        None,
    )
    document = json.loads(receipt.escalation_json) if receipt.escalation_json else {}
    resolved = (
        (document or {}).get("resolved") if document.get("kind") == KIND else None
    )
    choice = (resolved or {}).get("option_key")
    if resumed is None and choice == RESUME_OPTION:
        result = controls.set_control("resume_task", ACTOR, task_id=task_id)
        if not result["ok"]:
            _record(ENACT_REFUSED, task_id, option_key=choice, refusal=result["reason"])
            return "enact_refused"
        if result.get("pending"):
            return "enact_waiting"
        resumed = True
    if resumed is not None:
        with controls._read_session() as db:
            current = controls._receipt(db, task_id)
            if (
                json.loads(current.direction_json or "{}")
                .get("pending_capacity_release", {})
                .get("resume")
            ):
                return "enact_waiting"
            if current.task_paused:
                _record(
                    ENACT_REFUSED, task_id, option_key=choice, refusal="task_paused"
                )
                return "enact_refused"
        # A direct resume is the operator's action even if an automatic
        # revalidation previously dismissed the card without releasing it.
        actor = getattr(resumed, "actor", None) or (resolved or {}).get("actor", ACTOR)
        if actor == ACTOR and choice == RESUME_OPTION:
            # A restart can replay our resume audit before the clear landed.
            # That audit enacted the card's answer on the operator's behalf.
            actor = resolved.get("actor", ACTOR)
        if not controls.terminal_resolution(resolved):
            _resolve_card(task_id, actor, "Resumed with resume_task.")
        _clear(task_id, "resume", used, threshold, runs, resumed_by=actor)
        return "cleared"
    if choice in (STOP_OPTION, RESCOPE_OPTION):
        result = controls.finish_task(
            task_id,
            "cancelled",
            ACTOR,
            evidence={
                "state": f"watchdog_{choice}",
                "reason": f"No-progress watchdog: operator chose {choice}.",
            },
        )
        if not result["ok"]:
            # Most often an attempt still in flight. The next tick retries.
            return "enact_waiting"
        return "stopped"
    return "awaiting_person"


def check(task_id: str, policy: dict) -> str:
    """One tick of the watchdog for one active task. Returns what it did."""
    watch_policy = controls.progress_watchdog_policy(policy)
    if not watch_policy["enabled"]:
        return "disabled"
    threshold = watch_policy["threshold_turns"]
    with controls._read_session() as db:
        receipt = controls._receipt(db, task_id)
        if (
            receipt is None
            or receipt.state not in ("admitted", "uncertain")
            or receipt.cancellation_requested
            or controls.is_advisory(controls.receipt_task_class(receipt))
        ):
            return "not_watched"
        trail = _trail(db, task_id)
        watch = state(trail, threshold)
        used = turns(db, task_id)
        runs = graph.node_runs(task_id, session=db)
        task = {
            "id": task_id,
            "repo": receipt.repo,
            "issue_number": receipt.issue_number,
            "title": receipt.title,
        }
        task_paused = receipt.task_paused
        crossing = watch["next_threshold_turns"]
        db.expunge(receipt)
    if watch["open"] is not None:
        return _enact(
            task,
            watch["open"],
            receipt,
            trail,
            used,
            threshold,
            runs,
        )
    if crossing is None:
        # The latest row counted dollars. Re-arm from now, keeping its run
        # watermark so a delivery since then still clears the next crossing.
        _record(
            CLEARED,
            task_id,
            reason="rearm_turns",
            turns_used=used,
            next_threshold_turns=used + threshold,
            run_watermark=watch["run_watermark"],
            threshold_turns=threshold,
        )
        return "rearmed"
    if used < crossing:
        return "under_threshold"
    fresh = delivered_since(runs, watch["run_watermark"])
    if fresh:
        _clear(task_id, "delivery", used, threshold, runs, delivered=fresh[:10])
        return "cleared"
    if task_paused:
        # Somebody else's fence. Assessing a task that cannot start anything
        # would only pile a second question on the first.
        return "paused_elsewhere"
    if any(r["status"] not in graph.TERMINAL_RUN_STATUSES for r in runs):
        # The funding judge weighs settled evidence. Uncertain starts still
        # carry their reservations and are reconciled by their existing owner.
        return "waiting_for_attempt"
    from factory.orchestration import factory_conductor as conductor, factory_funding

    requested = factory_funding.request(
        conductor._task(task_id),
        f"No new commit or pull request after {used} settled turns (progress step {threshold}).",
        progress=True,
    )
    return "funding_review" if requested else "funding_unavailable"


def decidable(row, escalation: dict | None) -> bool:
    """A watchdog card is answered while its task is paused, not after it settles."""
    return (
        isinstance(escalation, dict)
        and escalation.get("kind") == KIND
        and escalation.get("task_id") == row.task_id
        and row.state in ("admitted", "uncertain")
        and bool(row.task_paused)
    )


def detail(db, task_id: str | None, policy: dict | None) -> dict | None:
    """The watchdog block for the task detail view."""
    if not task_id:
        return None
    watch_policy = controls.progress_watchdog_policy(policy or {})
    trail = _trail(db, task_id)
    watch = state(trail, watch_policy["threshold_turns"])
    shadow = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id == task_id,
            FactoryAudit.action == "progress_shadow_assessed",
        )
        .order_by(FactoryAudit.id.desc())
    ).first()
    return {
        "enabled": watch_policy["enabled"],
        "threshold_turns": watch_policy["threshold_turns"],
        "turns_used": turns(db, task_id),
        "next_threshold_turns": watch["next_threshold_turns"],
        "assessment_cost_usd": round(watch["assessment_cost_usd"], 6),
        "paused_by_watchdog": watch["open"] is not None,
        "last": watch["last"],
        "shadow": controls.audit_detail(shadow) if shadow else None,
    }


def paused_receipts(db) -> list[tuple[int, int, int]]:
    """(receipt id, issue, turns) for each active task the watchdog is holding.

    Held means the task's standing pause is the watchdog's own: its latest
    successful pause_task was written by this actor. A person who paused the
    task again by hand after resuming it owns that pause instead.
    """
    held = []
    rows = db.exec(
        select(FactoryReceipt).where(
            FactoryReceipt.state.in_(("admitted", "uncertain")),
            FactoryReceipt.task_paused.is_(True),
            FactoryReceipt.task_id.is_not(None),
        )
    ).all()
    for row in rows:
        pauses = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == row.task_id,
                FactoryAudit.action == "pause_task",
            )
            .order_by(FactoryAudit.id.desc())
        ).all()
        latest = next((p for p in pauses if controls.audit_detail(p).get("ok")), None)
        if latest is not None and latest.actor == ACTOR:
            held.append((row.id, row.issue_number, turns(db, row.task_id)))
    return held
