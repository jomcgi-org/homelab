"""GitHub receipt deduplication and single-task admission, without network calls."""

from __future__ import annotations

import logging

import json

from sqlalchemy import or_
from sqlmodel import Session, select

from factory.orchestration.factory_controls import (
    _audit,
    _integer,
    _json,
    _locked_session,
    _read_session,
    _now,
    _snapshot,
    _text,
    DEFAULT_TASK_CLASS,
    delivery_branch_owner,
    factory_max_concurrent_tasks,
    granted_delivery_surface,
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
from factory.orchestration.factory_models import (  # noqa: F401 - same_work re-exported
    FactoryReceipt,
    WorkItem,
    same_work,
)
from factory.orchestration.models import SwarmTask, mint_task_id


logger = logging.getLogger(__name__)


def receipts_for_work(
    db: Session, repo: str, issue_number: int, *, states: list[str] | None = None
) -> list[FactoryReceipt]:
    """Select receipts for a work item, matching by work item ID if present.

    If a work item exists for (repo, issue_number), receipts are selected where
    work_item_id matches that work item ID. Otherwise, receipts are selected by
    (repo, issue_number). Results are optionally filtered by state.
    """
    work_item_id = None
    work_item_row = db.exec(
        select(WorkItem).where(
            WorkItem.github_repo == repo,
            WorkItem.github_issue_number == issue_number,
        )
    ).one_or_none()
    if work_item_row is not None:
        work_item_id = work_item_row.id

    query = select(FactoryReceipt).where(
        (FactoryReceipt.work_item_id == work_item_id)
        if work_item_id is not None
        else (
            (FactoryReceipt.repo == repo)
            & (FactoryReceipt.issue_number == issue_number)
        )
    )
    if states is not None:
        query = query.where(FactoryReceipt.state.in_(states))
    return db.exec(query).all()


def get_issue_receipt(repo: str, issue_number: int, generation: int) -> dict | None:
    """Read an existing delivery receipt without depending on GitHub availability."""
    repo = normalize_repo(repo)
    issue_number = _integer(issue_number, "issue_number", 1, 2**31 - 1)
    generation = _integer(generation, "generation", 0, 2**31 - 1)
    with _read_session() as db:
        row = db.exec(
            select(FactoryReceipt).where(
                FactoryReceipt.repo == repo,
                FactoryReceipt.issue_number == issue_number,
                FactoryReceipt.generation == generation,
                FactoryReceipt.task_class == DEFAULT_TASK_CLASS,
            )
        ).one_or_none()
        return (
            {"ok": True, "created": False, "receipt": _snapshot(db, row)}
            if row
            else None
        )


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
    issue: dict | None = None,
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
    title = _text(title, "title", 512)
    if not isinstance(body, str) or len(body) > 65536:
        raise ValueError("invalid body")
    if (
        not isinstance(url, str)
        or url.lower() != f"https://github.com/{repo}/issues/{issue_number}"
    ):
        raise ValueError("url must identify the received GitHub issue")
    with _locked_session(session) as (db, _control):
        existing = db.exec(
            select(FactoryReceipt).where(
                FactoryReceipt.repo == repo,
                FactoryReceipt.issue_number == issue_number,
                FactoryReceipt.generation == generation,
                FactoryReceipt.task_class == task_class,
            )
        ).first()
        if existing is not None:
            return {"ok": True, "created": False, "receipt": _snapshot(db, existing)}
        # The sweep mints the work item before admission runs on the same
        # tick, so a receipt links to it at creation; an operator-posted
        # receipt for an issue the sweep has not seen yet links on the next
        # mint through the backfill in work_items.
        work_item = db.exec(
            select(WorkItem).where(
                WorkItem.github_repo == repo,
                WorkItem.github_issue_number == issue_number,
            )
        ).one_or_none()
        if work_item is None and issue is not None:
            from factory.orchestration import work_items

            # Minting is a side effect of receiving, never a gate on it: a
            # malformed issue or a missing table must not cost the receipt.
            # The savepoint keeps a failed mint from poisoning the session
            # the receipt is about to commit on; the next sweep's backfill
            # links the receipt once the item exists.
            try:
                with db.begin_nested():
                    work_item, _outcome = work_items.mint_or_sync_from_github(
                        db, repo, issue, actor=actor
                    )
            except Exception:  # noqa: BLE001 - logged, receipt still written
                logger.warning(
                    "work item mint failed while receiving %s#%s",
                    repo,
                    issue_number,
                    exc_info=True,
                )
                work_item = None
        row = FactoryReceipt(
            repo=repo,
            work_item_id=work_item.id if work_item else None,
            issue_number=issue_number,
            generation=generation,
            title=title,
            body=body,
            url=url,
            actor=actor,
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
    """The pinned route, or the class lane for a pre-feedback receipt."""
    return getattr(row, "routing_tier", None) or lane_for(receipt_task_class(row))


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
        # Evaluate quality at the admission boundary, then use the resulting
        # class map in the ordered receipt query. This keeps the query bounded
        # by the fixed class set and avoids loading the whole intake queue.
        from factory.orchestration.factory_feedback import (
            route_for_class,
            store_class_route,
        )

        routes = {name: route_for_class(name, session=db) for name in TASK_CLASSES}
        classes = [
            name for name, feedback in routes.items() if feedback["tier"] in available
        ]
        in_lane = FactoryReceipt.task_class.in_(classes)
        if routes[DEFAULT_TASK_CLASS]["tier"] in available:
            in_lane = or_(in_lane, FactoryReceipt.task_class.is_(None))
        eligible = FactoryReceipt.issue_number.in_(policy["issue_numbers"])
        if intake_policy(policy)["enabled"]:
            # Intake receipts are not in the operator allowlist by construction.
            # They are admissible only while intake is on, so turning intake off
            # leaves the allowlist exactly as it was.
            eligible = or_(eligible, FactoryReceipt.actor == INTAKE_ACTOR)
        # A new generation may coexist with an older task. A direct receipt
        # cannot start a second task on that same issue, even in another lane.
        busy_issues = [r.issue_number for r in active if r.repo == policy["repo"]]
        busy_work_items = [
            r.work_item_id
            for r in active
            if r.work_item_id is not None and r.repo == policy["repo"]
        ]
        base_query = select(FactoryReceipt).where(
            FactoryReceipt.state == "queued",
            FactoryReceipt.repo == policy["repo"],
            eligible,
            in_lane,
            FactoryReceipt.generation == policy["generation"],
        )
        if busy_issues:
            base_query = base_query.where(
                FactoryReceipt.issue_number.not_in(busy_issues)
            )
        if busy_work_items:
            base_query = base_query.where(
                (FactoryReceipt.work_item_id.is_(None))
                | (FactoryReceipt.work_item_id.not_in(busy_work_items))
            )
        row = db.exec(
            base_query.order_by(FactoryReceipt.created_at, FactoryReceipt.id)
        ).first()
        if row is None:
            return {"ok": False, "reason": "no_eligible_issue"}
        route = routes[receipt_task_class(row)]
        direction = json.loads(row.direction_json) if row.direction_json else {}
        if route["tier"] == "delivery" and not direction.get("conductor_gates"):
            refined = db.exec(
                select(FactoryReceipt)
                .where(
                    FactoryReceipt.repo == row.repo,
                    FactoryReceipt.issue_number == row.issue_number,
                    FactoryReceipt.task_class == "refine",
                    FactoryReceipt.state == "succeeded",
                )
                .order_by(FactoryReceipt.id.desc())
            ).first()
            if refined and refined.direction_json:
                gates = json.loads(refined.direction_json).get("conductor_gates", [])
                if gates:
                    direction["conductor_gates"] = gates
                    row.direction_json = _json(direction)
        try:
            granted_branch, _granted_pr = granted_delivery_surface(direction)
        except ValueError as exc:
            return {
                "ok": False,
                "reason": "delivery_surface_invalid",
                "detail": str(exc),
            }
        if granted_branch is not None:
            owner = delivery_branch_owner(
                db,
                row.repo,
                granted_branch,
                exclude_receipt_id=row.id,
            )
            if owner is not None:
                return {
                    "ok": False,
                    "reason": "delivery_branch_owned",
                    "task_id": owner,
                    "branch": granted_branch,
                }
        task_id = mint_task_id()
        task = SwarmTask(
            id=task_id,
            task_text=f"GitHub issue {row.url}\n\n{row.title}\n\n{row.body}",
            repo=policy["repo"],
            base_branch=policy["base_branch"],
            conductor_model=policy["conductor_model"],
            budget_usd=policy["task_budget_usd"],
            workflow_id=f"factory:{task_id}",
            start_state="factory",
            start_triggered_by=actor,
        )
        db.add(task)
        db.flush()
        from knowledge.api import prepare_recall

        prepare_recall(db, task.task_text)
        row.task_id, row.state, row.policy_json = task_id, "admitted", _json(policy)
        row.routing_tier = route["tier"]
        store_class_route(receipt_task_class(row), route, session=db)
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
            task_class=receipt_task_class(row),
            feedback_decision=route["decision"],
            feedback_samples=route["sample_count"],
            first_pass_approval_rate=route["approval_rate"],
            policy_version=control.version,
        )
        if route["tier"] != route["previous_tier"]:
            _audit(
                db,
                "factory:feedback",
                (
                    "class_tier_demoted"
                    if route["tier"] == "advisory"
                    else "class_tier_restored"
                ),
                task_id=task_id,
                task_class=receipt_task_class(row),
                previous_tier=route["previous_tier"],
                routing_tier=route["tier"],
                sample_kind=route["sample_kind"],
                sample_count=route["sample_count"],
                approval_rate=route["approval_rate"],
                approval_floor=route["approval_floor"],
            )
        return {
            "ok": True,
            "task_id": task_id,
            "receipt_id": row.id,
            "lane": lane_of(row),
            "policy": policy,
            "receipt": _snapshot(db, row, body=True),
        }
