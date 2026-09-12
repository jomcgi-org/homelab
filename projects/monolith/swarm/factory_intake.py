"""GitHub receipt deduplication and single-task admission, without network calls."""

from __future__ import annotations

import json

from sqlalchemy import or_
from sqlmodel import Session, select

from swarm.factory_controls import (
    _audit,
    _integer,
    _json,
    _locked_session,
    _now,
    _snapshot,
    _text,
    DEFAULT_TASK_CLASS,
    FEEDER_ACTOR,
    capability_tier_for,
    factory_max_concurrent_tasks,
    INTAKE_ACTOR,
    LANES,
    intake_policy,
    lane_for,
    lane_limits,
    lane_max_tasks,
    normalize_repo,
    receipt_task_class,
    TASK_CLASSES,
    validate_task_class,
    validate_policy,
)
from swarm.factory_models import FactoryReceipt
from swarm.models import SwarmTask, mint_task_id


def receive_issue(
    repo: str,
    issue_number: int,
    title: str,
    body: str,
    url: str,
    actor: str,
    *,
    generation: int = 0,
    task_class: str = DEFAULT_TASK_CLASS,
    source_key: str | None = None,
    requires_issue_close: bool = True,
    require_enabled: bool = False,
    session: Session | None = None,
) -> dict:
    """Store one bounded issue snapshot. A duplicate can never replace its text.

    Generation is an operator-authorized recurrence identity, not an issue field.
    Receipt creation grants no execution authority; admit_next checks the policy.
    """
    repo = normalize_repo(repo)
    issue_number = _integer(issue_number, "issue_number", 1, 2**31 - 1)
    generation = _integer(generation, "generation", 0, 2**31 - 1)
    actor = _text(actor, "actor")
    task_class = validate_task_class(task_class)
    source_key = _text(source_key or f"issue:{issue_number}", "source_key", 512)
    title = _text(title, "title", 512)
    if not isinstance(body, str) or len(body) > 65536:
        raise ValueError("invalid body")
    if (
        not isinstance(url, str)
        or url.lower() != f"https://github.com/{repo}/issues/{issue_number}"
    ):
        raise ValueError("url must identify the received GitHub issue")
    with _locked_session(session) as (db, control):
        if require_enabled and control.state != "enabled":
            return {"ok": False, "created": False, "reason": control.state}
        existing = db.exec(
            select(FactoryReceipt).where(
                FactoryReceipt.repo == repo,
                FactoryReceipt.generation == generation,
                FactoryReceipt.source_key == source_key,
                FactoryReceipt.task_class == task_class,
            )
        ).first()
        if existing is not None:
            return {"ok": True, "created": False, "receipt": _snapshot(db, existing)}
        row = FactoryReceipt(
            repo=repo,
            issue_number=issue_number,
            generation=generation,
            title=title,
            body=body,
            url=url,
            actor=actor,
            source_key=source_key,
            requires_issue_close=requires_issue_close,
            task_class=task_class,
        )
        db.add(row)
        db.flush()
        _audit(
            db,
            actor,
            "receive_issue",
            receipt_id=row.id,
            repo=repo,
            issue_number=issue_number,
            generation=generation,
            source_key=source_key,
            task_class=task_class,
        )
        return {"ok": True, "created": True, "receipt": _snapshot(db, row)}


def concurrency_limit(policy: dict) -> int:
    """Tasks the factory may have in flight across both lanes.

    The lanes are bounded separately, so this is only the cheap total gate a
    caller uses to decide whether to look at all. A lane with room is what
    actually admits, and ``admit_next`` is the authority on that.
    """
    return sum(lane_limits(policy).values())


def lane_of(row) -> str:
    """The lane a receipt belongs to, reading a pre-class receipt as delivery."""
    return lane_for(receipt_task_class(row))


def ceiling_below_lanes(policy: dict) -> dict | None:
    """The numbers, when the chart ceiling cannot hold both lanes at once.

    None when it can. The ceiling is chart configuration and the per-lane
    maxima are posted policy, so the two can be set against each other with
    nothing in either place saying so.
    """
    wanted = lane_max_tasks(policy)
    ceiling = factory_max_concurrent_tasks()
    total = sum(wanted.values())
    if ceiling >= total:
        return None
    return {
        "ceiling": ceiling,
        "delivery_max": wanted["delivery"],
        "advisory_max": wanted["advisory"],
        "lanes_sum": total,
    }


def open_lanes(policy: dict, rows, lanes=LANES) -> dict:
    """Room left per lane, given the receipts already holding slots in them.

    The chart owns one ceiling for the whole factory and the policy asks for a
    maximum per lane. When the ceiling covers their sum each lane simply has
    its own maximum minus what it holds, and nothing here contends.

    Below their sum the lanes do contend, and giving delivery its whole maximum
    first is what starved advisory work: at a ceiling of four with lanes four
    and eight, advisory got nothing and one sweep excluded 272 candidates as
    lane_full. Contended, the free part of the ceiling is instead dealt one
    slot at a time to the lane furthest from its own maximum, so a lane holding
    nothing is never denied while another holds everything. Delivery takes the
    first slot, because delivery is what the lane exists to do and a ceiling an
    operator set low must never leave it unable to start anything.
    """
    wanted = lane_max_tasks(policy)
    held = {lane: 0 for lane in LANES}
    for row in rows:
        held[lane_of(row)] += 1
    headroom = {
        lane: max(0, wanted[lane] - held[lane]) if lane in lanes else 0
        for lane in LANES
    }
    free = max(0, factory_max_concurrent_tasks() - sum(held.values()))
    if free >= sum(headroom.values()):
        return headroom
    room = {lane: 0 for lane in LANES}
    for slot in range(free):
        candidates = [lane for lane in LANES if room[lane] < headroom[lane]]
        if not candidates:
            break
        if slot == 0 and "delivery" in candidates:
            pick = "delivery"
        else:
            # Fullest-last, by fraction of the lane's own maximum, so the
            # split follows what each lane actually holds rather than what it
            # asked for. Delivery wins a tie, as it does for the first slot.
            pick = min(
                candidates,
                key=lambda lane: (
                    (held[lane] + room[lane]) / wanted[lane],
                    LANES.index(lane),
                ),
            )
        room[pick] += 1
    return room


def admit_next(actor: str, *, lanes=LANES, session: Session | None = None) -> dict:
    """Atomically reserve one WIP slot and pin the operator policy to a new SwarmTask.

    Each lane is bounded on its own, so a full delivery lane never blocks an
    advisory admission and the reverse. ``lanes`` narrows that further for a
    caller holding a reason to keep one shut, and an unnamed lane admits
    nothing rather than falling back to the policy.

    max_tasks bounds tasks in flight, not tasks ever admitted: an autonomous
    intake must keep admitting as tasks settle. Total spend per generation is
    bounded by the receipts it can hold (one per repo/issue/generation/class)
    times task_budget_usd, not by a counter a human has to re-arm.
    admitted_count is kept for status only.

    Three paths return a settled receipt to queued, all of them in
    swarm/factory_decisions.py and all of them an explicit human act on one
    issue at a time: an operator answering a refine escalation with "needs
    more chat", an operator answering an escalated delivery with the option
    that says carry on, and the same chat action on an escalated delivery. So
    the bound above becomes those receipts plus the rounds somebody asked for
    by hand, rather than anything the lane can do to itself.

    A refine re-brief costs one advisory node. A delivery re-admission is a
    whole new task: a fresh graph, a fresh allowance and a fresh
    task_budget_usd, because the escalated attempt's spend is history and the
    new task has to be able to plan and deliver inside its own envelope. The
    receipt carries its previous task ids so the board can show what the issue
    has cost across all of them, and the escalations are the place to watch
    that, since nothing here caps how many times one issue may be re-admitted.
    """
    actor = _text(actor, "actor")
    lanes = tuple(lane for lane in LANES if lane in lanes)
    with _locked_session(session) as (db, control):
        if control.state != "enabled":
            return {"ok": False, "reason": control.state}
        policy = validate_policy(json.loads(control.policy_json))
        limits = lane_limits(policy)
        active = db.exec(
            select(FactoryReceipt)
            .where(FactoryReceipt.state.in_(("admitted", "uncertain")))
            .order_by(FactoryReceipt.id)
        ).all()
        room = open_lanes(policy, active, lanes)
        available = [lane for lane in lanes if room[lane] > 0]
        if not available:
            held = [row.task_id for row in active]
            return {
                "ok": False,
                "reason": "wip_limit",
                "task_id": held[0] if held else None,
                "active_task_ids": held,
                "active": len(active),
                "limit": sum(limits[lane] for lane in lanes),
                "lanes": {
                    lane: {
                        "limit": limits[lane] if lane in lanes else 0,
                        "active": sum(1 for row in active if lane_of(row) == lane),
                    }
                    for lane in LANES
                },
            }
        # A receipt written before classes existed has a NULL column and reads
        # as the default class, which is delivery. Matching it by class alone
        # would leave those receipts unadmittable.
        classes = [name for name in TASK_CLASSES if lane_for(name) in available]
        in_lane = FactoryReceipt.task_class.in_(classes)
        if "delivery" in available:
            in_lane = or_(in_lane, FactoryReceipt.task_class.is_(None))
        eligible = or_(
            FactoryReceipt.issue_number.in_(policy["issue_numbers"]),
            FactoryReceipt.actor == FEEDER_ACTOR,
        )
        if intake_policy(policy)["enabled"]:
            # Intake receipts are not in the operator allowlist by construction.
            # They are admissible only while intake is on, so turning intake off
            # leaves the allowlist exactly as it was.
            eligible = or_(eligible, FactoryReceipt.actor == INTAKE_ACTOR)
        row = db.exec(
            select(FactoryReceipt)
            .where(
                FactoryReceipt.state == "queued",
                FactoryReceipt.repo == policy["repo"],
                eligible,
                in_lane,
                FactoryReceipt.generation == policy["generation"],
            )
            .order_by(FactoryReceipt.created_at, FactoryReceipt.id)
        ).first()
        if row is None:
            return {"ok": False, "reason": "no_eligible_issue"}
        task_id = mint_task_id()
        task = SwarmTask(
            id=task_id,
            task_text=f"GitHub issue {row.url}\n\n{row.title}\n\n{row.body}",
            repo=policy["repo"],
            base_branch=policy["base_branch"],
            conductor_model=policy["conductor_model"],
            task_class=receipt_task_class(row),
            capability_tier=capability_tier_for(receipt_task_class(row)),
            budget_usd=policy["task_budget_usd"],
            workflow_id=f"factory:{task_id}",
            start_state="factory",
            start_triggered_by=actor,
        )
        db.add(task)
        db.flush()
        row.task_id, row.state, row.policy_json = task_id, "admitted", _json(policy)
        row.updated_at = _now()
        control.admitted_count += 1
        control.updated_at = _now()
        db.add(row)
        db.add(control)
        _audit(
            db,
            actor,
            "admit_next",
            task_id=task_id,
            receipt_id=row.id,
            lane=lane_of(row),
            policy_version=control.version,
        )
        return {
            "ok": True,
            "task_id": task_id,
            "receipt_id": row.id,
            "lane": lane_of(row),
            "policy": policy,
            "receipt": _snapshot(db, row, body=True),
        }
