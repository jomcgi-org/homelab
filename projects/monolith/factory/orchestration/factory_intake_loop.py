"""Bounded autonomous GitHub issue intake, inert unless policy enables it."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from factory.orchestration.factory_controls import (
    DEFAULT_TASK_CLASS,
    LANES,
    _audit,
    _locked_session,
    _now,
    _read_session,
    delivery_admissions,
    intake_policy,
    intake_state,
    is_advisory,
    lane_for,
    receipt_task_class,
)
from factory.orchestration.factory_intake import (
    INTAKE_ACTOR,
    ceiling_below_lanes,
    open_lanes,
    receive_issue,
)
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryReceipt,
    WorkItem,
    WorkItemEdge,
)

logger = logging.getLogger(__name__)

# One name for the identity admission also keys on, so the receipts this
# module writes and the receipts admit_next will accept can never diverge.
ACTOR = INTAKE_ACTOR
PAGE_SIZE = 100
PULL_PAGE_SIZE = 50
MAX_PAGES = 5
CANDIDATE_EVIDENCE_LIMIT = 20
# Both the idle audit and the listing that precedes it run on this clock. A
# quiet lane ticks every 15 seconds and the repository does not change that
# fast, so listing every tick would spend roughly 2400 of the shared 5000
# requests an hour to learn nothing.
IDLE_AUDIT_SECONDS = 3600
RANK_LABELS = ("critical", "bug")
# ADR agents/038 decision 5, read off the issue's own labels. A refine
# candidate is advisory whatever else it carries, because it delivers a
# comment rather than a change. security-finding is excluded by default, but
# this mapping applies when an operator removes it from the exclusions.
CLASS_LABELS = (
    ("security-finding", "judgment-analysis"),
    ("needs-thought", "judgment-analysis"),
    ("bug", "bug-fix"),
    ("documentation", "docs"),
    ("todo", "mechanical-refactor"),
)
_EXCLUSION_REASONS = (
    "malformed",
    "pull_request",
    "not_open",
    "assigned",
    "excluded_label",
    "linked_pr",
    "delivered",
    "escalated",
    "cooldown",
    "already_received",
    "active_issue",
    "blocked",
    "deferred",
    "refine_disabled",
    "lane_full",
    "local_malformed",
    "local_unnumbered",
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def github_list(repo: str, suffix: str) -> list:
    """Bounded list read, imported lazily so the board stays off the reconciler.

    factory_controls.status reads the intake block, and the board reads status,
    so a module-scope import here would pull the whole conductor into a page
    render. Keeping the name here also leaves tests one seam to fake.
    """
    from factory.orchestration.factory_conductor import github_list as read

    return read(repo, suffix)


def _pages(
    repo: str, endpoint: str, *, page_size: int = PAGE_SIZE
) -> tuple[list, bool]:
    """Open rows oldest first, and whether the read hit the page cap.

    GitHub defaults to newest first, so a repository with more open rows than
    MAX_PAGES * page_size would truncate exactly the oldest ones, which is the
    half ranking prefers. Asking for ascending order makes the truncation fall
    on the newest instead, and the flag says it happened so an operator reads
    a partial sweep as partial.
    """
    result = []
    truncated = False
    for page in range(1, MAX_PAGES + 1):
        rows = github_list(
            repo,
            f"{endpoint}?state=open&sort=created&direction=asc"
            f"&per_page={page_size}&page={page}",
        )
        result.extend(rows)
        if len(rows) < page_size:
            break
        truncated = page == MAX_PAGES
    return result, truncated


def _label_names(issue: dict) -> set[str]:
    names = set()
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.add(name.lower())
    return names


def derive_task_class(labels: set[str], *, refine: bool) -> tuple[str, str]:
    """The class and the stated reason it was chosen."""
    if refine:
        return "refine", "refine candidate"
    for name, task_class in CLASS_LABELS:
        if name in labels:
            return task_class, f"label {name}"
    return DEFAULT_TASK_CLASS, "default"


def _receipt_exclusion(
    rows: list[FactoryReceipt],
    *,
    generation: int,
    task_class: str,
    cooldown_cutoff: datetime,
) -> str | None:
    """Return the first receipt-derived reason that excludes a candidate."""
    if any(
        row.state == "succeeded" and not is_advisory(receipt_task_class(row))
        for row in rows
    ):
        return "delivered"
    if any(row.state in ("admitted", "uncertain") for row in rows):
        return "active_issue"
    if any(row.state == "escalated" for row in rows):
        return "escalated"
    latest = rows[0] if rows else None
    if (
        latest is not None
        and latest.state in ("failed", "cancelled")
        and _aware(latest.updated_at) >= cooldown_cutoff
    ):
        return "cooldown"
    if any(
        row.generation == generation and receipt_task_class(row) == task_class
        for row in rows
    ):
        return "already_received"
    return None


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
    # Unknown age sorts last. Falling back to the issue number would read a
    # low number as an old date and hand the top rank to the one candidate
    # whose age could not be established.
    return float("inf")


def _latest(db, action: str) -> FactoryAudit | None:
    return db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action == action)
        .order_by(FactoryAudit.id.desc())
    ).first()


def _throttled(action: str, detail: dict, *, session=None) -> None:
    """Write one audit of this action per hour, and drop the rest.

    A quiet lane reaches the same conclusion every fifteen seconds. Recording
    each one would bury the audit trail under rows that all say what the first
    already said.
    """
    cutoff = _now() - timedelta(seconds=IDLE_AUDIT_SECONDS)
    with _locked_session(session) as (db, _control):
        last = _latest(db, action)
        if last is not None and _aware(last.created_at) >= cutoff:
            return
        _audit(db, ACTOR, action, **detail)


def _idle(detail: dict) -> None:
    _throttled("intake_idle", detail)


def _listing_due(now: datetime) -> bool:
    """Whether this tick may spend GitHub reads on a fresh sweep.

    The sweep runs at most once an hour while the lane finds nothing, because
    a repository does not gain eligible work faster than that and the request
    budget is shared with everything else that reads GitHub. A settlement is
    the exception: once a receipt reaches a terminal state the picture has
    genuinely changed, so the next tick sweeps immediately rather than waiting
    out the rest of the hour.

    The clock is its own audit rather than the idle one. An idle row records a
    refusal, which is not the same event as reading GitHub: the daily cap
    writes one for a delivery candidate it turned away, and tying the sweep to
    that would leave the lane blind for an hour after the cap had cleared. A
    settlement recorded in the same second as the sweep re-opens it: one extra
    sweep costs two requests, a missed one costs an hour.

    An admission re-opens it too, and that is what lets a lane fill. A sweep
    takes at most one candidate per lane, so at four delivery and eight
    advisory slots the hourly clock filled the lanes at one slot an hour. When
    the newest admission is at or after the newest sweep, the previous sweep
    found work and there may be more, so the next tick sweeps again. It costs
    at most one extra sweep per admission: the sweep stamps its own clock
    before reading GitHub, so a sweep that admits nothing leaves the newest
    admission behind the newest sweep and the hourly clock governs again. The
    caller has already established that a lane has room; a tick with none
    returns before reaching this.
    """
    with _read_session() as db:
        last = _latest(db, "intake_swept")
        if last is None:
            return True
        marked = _aware(last.created_at)
        if marked < now - timedelta(seconds=IDLE_AUDIT_SECONDS):
            return True
        admitted = _latest(db, "intake_admitted")
        if admitted is not None and _aware(admitted.created_at) >= marked:
            return True
        settled = db.exec(
            select(FactoryReceipt.id).where(
                FactoryReceipt.state.in_(
                    ("succeeded", "failed", "cancelled", "escalated")
                ),
                FactoryReceipt.updated_at >= marked,
            )
        ).first()
        return settled is not None


def _local_candidates(
    repo: str,
    *,
    intake: dict,
    generation: int,
    room: dict[str, bool],
    cooldown_cutoff: datetime,
) -> tuple[list[dict], dict[str, int], int]:
    """Build candidates from authoritative local rows without a GitHub read."""
    excluded = {reason: 0 for reason in _EXCLUSION_REASONS}
    exclude_labels = {label.lower() for label in intake["exclude_labels"]}
    candidates = []

    # Webhook ingress adds trusted GitHub-authority rows to the same candidate
    # policy as local rows. It does not retire the sweep: migration and cutover
    # are separate operational work outside this repository slice.
    from factory.orchestration.factory_webhook import webhook_enabled

    item_scope = WorkItem.authority == "local"
    if webhook_enabled():
        item_scope = item_scope | (
            (WorkItem.authority == "github") & (WorkItem.trust == "trusted")
        )

    with _read_session() as db:
        local_items = db.exec(
            select(WorkItem)
            .where(
                item_scope,
                WorkItem.state.in_(("ready", "open")),
                WorkItem.github_repo == repo,
            )
            .order_by(WorkItem.github_created_at, WorkItem.created_at)
        ).all()
        unnumbered_items = db.exec(
            select(WorkItem).where(
                WorkItem.authority == "local",
                WorkItem.state.in_(("ready", "open")),
                WorkItem.github_issue_number.is_(None),
            )
        ).all()
        local_ids = [item.id for item in local_items if item.id is not None]
        local_receipts = (
            db.exec(
                select(FactoryReceipt)
                .where(
                    FactoryReceipt.repo == repo,
                    FactoryReceipt.work_item_id.in_(local_ids),
                )
                .order_by(FactoryReceipt.id.desc())
            ).all()
            if local_ids
            else []
        )
        blocked_local_ids = (
            set(
                db.exec(
                    select(WorkItemEdge.to_id)
                    .join(WorkItem, WorkItemEdge.from_id == WorkItem.id)
                    .where(
                        WorkItemEdge.kind == "blocks",
                        WorkItemEdge.to_id.in_(local_ids),
                        WorkItem.state != "closed",
                    )
                    .distinct()
                ).all()
            )
            if local_ids
            else set()
        )

    excluded["local_unnumbered"] = len(unnumbered_items)
    for work_item in local_items:
        number = work_item.github_issue_number
        if number is None:
            excluded["local_unnumbered"] += 1
            continue
        title = work_item.title
        body = work_item.body
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 512
            or not isinstance(body, str)
            or len(body) > 65536
        ):
            excluded["local_malformed"] += 1
            _throttled(
                "local_item_malformed",
                {"work_item_id": work_item.id},
            )
            continue
        labels = {label.lower() for label in work_item.labels}
        if labels & exclude_labels:
            excluded["excluded_label"] += 1
            continue
        if work_item.id in blocked_local_ids:
            excluded["blocked"] += 1
            continue
        delivery = work_item.state == "ready"
        refine = not delivery
        task_class, class_reason = derive_task_class(labels, refine=refine)
        rows = [row for row in local_receipts if row.work_item_id == work_item.id]
        if refine and "needs-thought" in labels:
            excluded["deferred"] += 1
            continue
        receipt_exclusion = _receipt_exclusion(
            rows,
            generation=generation,
            task_class=task_class,
            cooldown_cutoff=cooldown_cutoff,
        )
        if receipt_exclusion is not None:
            excluded[receipt_exclusion] += 1
            continue
        if refine and not intake["refine_enabled"]:
            excluded["refine_disabled"] += 1
            continue
        if type(number) is not int or not 1 <= number <= 2**31 - 1:
            excluded["local_malformed"] += 1
            _throttled(
                "local_item_malformed",
                {"work_item_id": work_item.id},
            )
            continue
        label_rank = next(
            (index for index, label in enumerate(RANK_LABELS) if label in labels),
            len(RANK_LABELS),
        )
        rank_reason = (
            RANK_LABELS[label_rank] if label_rank < len(RANK_LABELS) else "oldest"
        )
        lane = lane_for(task_class)
        if not room.get(lane):
            excluded["lane_full"] += 1
            continue
        created_at = work_item.github_created_at or work_item.created_at
        item = {
            "number": number,
            "title": title,
            "body": body,
            "html_url": work_item.source_ref
            or f"https://github.com/{repo}/issues/{number}",
            "labels": work_item.labels,
            "created_at": _aware(created_at).isoformat(),
            "user": None,
        }
        candidates.append(
            {
                "issue": item,
                "number": number,
                "source": ("local" if work_item.authority == "local" else "webhook"),
                "work_item_id": work_item.id,
                "lane": lane,
                "task_class": task_class,
                "class_reason": class_reason,
                "rank_reason": rank_reason,
                "sort": (
                    1 if refine else 0,
                    label_rank,
                    _created_rank(item),
                    number,
                ),
            }
        )

    return (
        candidates,
        {reason: count for reason, count in excluded.items() if count},
        len(local_items) + len(unnumbered_items),
    )


def intake_tick(policy: dict, *, generation: int, lanes=LANES) -> list[dict]:
    """Queue at most one issue per open lane, or audit why it queued none.

    The lanes are ranked and filled independently: a delivery candidate and an
    advisory one can both enter on the same tick when both lanes have room,
    and never more than one of either. ``lanes`` lets a caller hold one shut
    without touching the policy.
    """
    try:
        intake = intake_policy(policy)
        if not intake["enabled"]:
            return []
        # The ceiling is chart configuration and the lane maxima are posted
        # policy, so neither says the other is the binding constraint. Name it
        # here, throttled, rather than leaving an operator to infer it from a
        # lane that never opens.
        shortfall = ceiling_below_lanes(policy)
        if shortfall is not None:
            _throttled("lane_ceiling_below_lanes", shortfall)
        now = _now()
        today = now - timedelta(hours=24)
        with _locked_session() as (db, _control):
            held = db.exec(
                select(FactoryReceipt).where(
                    FactoryReceipt.generation == generation,
                    FactoryReceipt.state.in_(("queued", "admitted", "uncertain")),
                )
            ).all()
            room = open_lanes(policy, held, lanes)
            if not any(room.values()):
                return []
            # Delivery only. The cap bounds delivery churn, and an advisory
            # refine costs cents and produces a comment, so the cap is not
            # consulted for one at all: it must never throttle a burn-down.
            admitted_today = delivery_admissions(db, today)

        repo = policy["repo"]
        excluded: dict[str, int] = {reason: 0 for reason in _EXCLUSION_REASONS}

        def exclude(reason: str) -> None:
            excluded[reason] += 1

        include_labels = {label.lower() for label in intake["labels"]}
        exclude_labels = {label.lower() for label in intake["exclude_labels"]}
        candidates = []
        cooldown_cutoff = now - timedelta(hours=intake["cooldown_hours"])
        local_listed = 0
        try:
            local_candidates, local_excluded, local_listed = _local_candidates(
                repo,
                intake=intake,
                generation=generation,
                room=room,
                cooldown_cutoff=cooldown_cutoff,
            )
            candidates.extend(local_candidates)
            for reason, count in local_excluded.items():
                excluded[reason] += count
        except Exception as exc:  # noqa: BLE001 - GitHub intake must still run
            _throttled(
                "local_intake_error",
                {"error": type(exc).__name__},
            )
            logger.warning("factory local intake failed", exc_info=True)

        issues = []
        pulls = []
        issues_cut = False
        pulls_cut = False
        github_status = None
        if _listing_due(now):
            # The clock records the ATTEMPT, not the result. A sweep that fails
            # is the case most worth rate limiting: a 403 usually means the shared
            # budget is already spent, and retrying every fifteen seconds is how
            # it stays spent.
            with _locked_session() as (db, _control):
                _audit(db, ACTOR, "intake_swept")
            try:
                issues, issues_cut = _pages(repo, "issues")
                pulls, pulls_cut = _pages(repo, "pulls", page_size=PULL_PAGE_SIZE)
            except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
                # A 403 from the shared rate limit, or any other read failure,
                # would otherwise leave intake silently dead: the blanket handler
                # below logs where nobody looks. Record the shape of the failure
                # without its response body, URL or credential-bearing text.
                _throttled(
                    "intake_error",
                    {
                        "stage": "listing",
                        "error": type(exc).__name__,
                        "status": getattr(
                            getattr(exc, "response", None), "status_code", None
                        ),
                    },
                )
                logger.warning("factory intake listing failed", exc_info=True)
                issues = []
                pulls = []
                issues_cut = False
                pulls_cut = False
                github_status = "failed"
        else:
            github_status = "not_due"

        if github_status is None:
            try:
                from factory.orchestration.work_item_links import reconcile_body_edges
                from factory.orchestration.work_items import sync_github_work_items

                work_item_counts = sync_github_work_items(
                    repo,
                    issues,
                    truncated=issues_cut,
                    actor=ACTOR,
                    source_ordered=os.getenv(
                        "FACTORY_GITHUB_WEBHOOK_ENABLED", "false"
                    ).lower()
                    == "true",
                )
                logger.info("work_item_sync", extra=work_item_counts)

                # Reconcile body edges in a new transaction
                with _locked_session() as (db, _control):
                    synced_items = db.exec(
                        select(WorkItem).where(
                            WorkItem.github_repo == repo,
                        )
                    ).all()
                    # Reconcile only from persisted bodies. A stale sweep
                    # payload rejected by source ordering must not reappear
                    # here and undo newer webhook dependency state.
                    items_with_bodies = [
                        (item, item.body)
                        for item in synced_items
                        if item.github_issue_number is not None
                    ]

                    if items_with_bodies:
                        edge_counts = reconcile_body_edges(
                            db,
                            repo,
                            items_with_bodies,
                            actor=ACTOR,
                            truncated=issues_cut,
                        )
                        logger.info("work_item_edge_reconcile", extra=edge_counts)
                    db.commit()
            except Exception as exc:  # noqa: BLE001 - intake survives sync failure
                logger.exception("work_item_sync_error")
                _throttled(
                    "work_item_sync_error",
                    {
                        "error": type(exc).__name__,
                        "truncated": issues_cut,
                    },
                )
        truncated = issues_cut or pulls_cut
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

        survivors = []
        for item in issues:
            if not isinstance(item, dict):
                exclude("malformed")
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
        work_item_map = {}
        work_item_by_number = {}
        local_authority_numbers = set()
        blocked_numbers: set[int] = set()
        if numbers:
            with _read_session() as db:
                # Resolve the candidates' work items first, so a receipt that
                # was linked to one of them under another issue number is in
                # the set: fetching receipts by number alone can never see it.
                work_items = db.exec(
                    select(WorkItem).where(
                        WorkItem.github_repo == repo,
                        WorkItem.github_issue_number.in_(numbers),
                    )
                ).all()
                work_item_map = {
                    wi.id: wi.github_issue_number
                    for wi in work_items
                    if wi.github_issue_number is not None
                }
                work_item_by_number = {
                    number: item_id for item_id, number in work_item_map.items()
                }
                local_authority_numbers = {
                    item.github_issue_number
                    for item in work_items
                    if item.authority == "local"
                    and item.github_issue_number is not None
                }
                if work_item_map:
                    blocked_item_ids = set(
                        db.exec(
                            select(WorkItemEdge.to_id)
                            .join(WorkItem, WorkItemEdge.from_id == WorkItem.id)
                            .where(
                                WorkItemEdge.kind == "blocks",
                                WorkItemEdge.to_id.in_(list(work_item_map)),
                                WorkItem.state != "closed",
                            )
                            .distinct()
                        ).all()
                    )
                    blocked_numbers = {
                        work_item_map[item_id] for item_id in blocked_item_ids
                    }
                by_number_or_item = FactoryReceipt.issue_number.in_(numbers)
                if work_item_map:
                    by_number_or_item = by_number_or_item | (
                        FactoryReceipt.work_item_id.in_(list(work_item_map))
                    )
                receipt_rows = db.exec(
                    select(FactoryReceipt)
                    .where(FactoryReceipt.repo == repo, by_number_or_item)
                    .order_by(FactoryReceipt.issue_number, FactoryReceipt.id.desc())
                ).all()
        for item, labels in survivors:
            number = item.get("number")
            if number in local_authority_numbers:
                continue
            rows = []
            if type(number) is int:
                for row in receipt_rows:
                    if row.issue_number == number:
                        rows.append(row)
                    elif (
                        row.work_item_id is not None
                        and work_item_map.get(row.work_item_id) == number
                    ):
                        rows.append(row)
            if number in blocked_numbers:
                exclude("blocked")
                continue
            # A delivered issue is done, whatever generation delivered it and
            # whatever the issue's own labels still say. The defect this
            # closes is exactly that: #3877 shipped as PR #6007, the body
            # carried no closing keyword so the issue stayed open with
            # agent-ready, and the next generation admitted it again.
            #
            # Advisory classes are not deliveries. A refine pass settles
            # succeeded when it has written a brief and moved the labels, and
            # reading that as delivered would block the very delivery the
            # refine just made possible.
            #
            # The exclusion is unconditional, including for an issue a human
            # reopened. GitHub's issue listing carries created_at, updated_at
            # and closed_at but no reopen timestamp, and updated_at moves on
            # every comment, label and edit, so it cannot tell a reopen from a
            # comment. Establishing the difference would cost one events read
            # per candidate on a request budget the whole lane shares. An
            # operator who wants a delivered issue worked again names it in
            # the policy issue_numbers allowlist under a new generation, which
            # writes its receipt directly and never consults this sweep.
            delivery = bool(labels & include_labels)
            refine = not delivery
            task_class, class_reason = derive_task_class(labels, refine=refine)
            if refine and "needs-thought" in labels:
                exclude("deferred")
                continue
            # Scoped to the class, not just the generation. A refine pass that
            # moved an issue to agent-ready has changed what the lane can do
            # with it, and waiting for an operator to bump the generation
            # would strand the readiness the lane just produced.
            receipt_exclusion = _receipt_exclusion(
                rows,
                generation=generation,
                task_class=task_class,
                cooldown_cutoff=cooldown_cutoff,
            )
            if receipt_exclusion is not None:
                exclude(receipt_exclusion)
                continue
            if not delivery and not intake["refine_enabled"]:
                exclude("refine_disabled")
                continue
            if type(number) is not int or not 1 <= number <= 2**31 - 1:
                exclude("malformed")
                continue
            label_rank = next(
                (index for index, label in enumerate(RANK_LABELS) if label in labels),
                len(RANK_LABELS),
            )
            rank_reason = (
                RANK_LABELS[label_rank] if label_rank < len(RANK_LABELS) else "oldest"
            )
            lane = lane_for(task_class)
            if not room.get(lane):
                exclude("lane_full")
                continue
            candidates.append(
                {
                    "issue": item,
                    "number": number,
                    "source": "github",
                    "work_item_id": work_item_by_number.get(number),
                    "lane": lane,
                    "task_class": task_class,
                    "class_reason": class_reason,
                    "rank_reason": rank_reason,
                    "sort": (
                        1 if refine else 0,
                        label_rank,
                        _created_rank(item),
                        number,
                    ),
                }
            )
        candidates.sort(key=lambda candidate: candidate["sort"])
        excluded = {reason: count for reason, count in excluded.items() if count}
        if not candidates:
            _idle(
                {
                    "excluded": excluded,
                    "listed": len(issues),
                    "local_listed": local_listed,
                    **({"github": github_status} if github_status else {}),
                    **({"truncated": True} if truncated else {}),
                }
            )
            return []

        # One per lane, in lane-blind rank order, so the delivery lane keeps
        # its precedence over refine while an open advisory lane is not left
        # idle behind a delivery candidate it has nothing to do with.
        chosen: list[dict] = []
        taken: set[str] = set()
        for candidate in candidates:
            if candidate["lane"] in taken:
                continue
            taken.add(candidate["lane"])
            chosen.append(candidate)

        received_all = []
        capped = False
        for candidate in chosen:
            # Re-read the lane under the lock. The room that picked this
            # candidate was measured before a GitHub sweep that takes seconds,
            # and another replica may have filled the lane in between. admit_next
            # is still the real gate; this only stops the lane queueing receipts
            # it already knows it cannot admit.
            with _locked_session() as (db, _control):
                held = db.exec(
                    select(FactoryReceipt).where(
                        FactoryReceipt.generation == generation,
                        FactoryReceipt.state.in_(("queued", "admitted", "uncertain")),
                    )
                ).all()
                room = open_lanes(policy, held, lanes)
            if not room.get(candidate["lane"]):
                continue
            delivery = candidate["lane"] == "delivery"
            # Refuse this candidate rather than the rest of the tick. The
            # lanes are independent, and the advisory lane behind a capped
            # delivery candidate has nothing to do with what the cap bounds.
            if delivery and admitted_today >= intake["max_per_day"]:
                capped = True
                continue
            issue = candidate["issue"]
            received = receive_issue(
                repo,
                candidate["number"],
                issue.get("title"),
                issue.get("body") or "",
                issue.get("html_url"),
                ACTOR,
                generation=generation,
                task_class=candidate["task_class"],
                issue=issue,
                work_item_id=candidate["work_item_id"],
            )
            if delivery:
                admitted_today += 1
            received_all.append(received)
            with _locked_session() as (db, _control):
                _audit(
                    db,
                    ACTOR,
                    "intake_admitted",
                    receipt_id=received["receipt"]["id"],
                    issue_number=candidate["number"],
                    source=candidate["source"],
                    lane=candidate["lane"],
                    task_class=candidate["task_class"],
                    class_reason=candidate["class_reason"],
                    rank_reason=candidate["rank_reason"],
                    candidates=[
                        {
                            "number": other["number"],
                            "source": other["source"],
                            "lane": other["lane"],
                            "task_class": other["task_class"],
                            "rank_reason": other["rank_reason"],
                        }
                        for other in candidates[:CANDIDATE_EVIDENCE_LIMIT]
                    ],
                    excluded=excluded,
                    admitted_today=admitted_today,
                    **(
                        {
                            "github": github_status,
                            "listed": len(issues),
                            "local_listed": local_listed,
                        }
                        if github_status
                        else {}
                    ),
                    **({"truncated": True} if truncated else {}),
                )
        # Once per tick, and only when the cap actually refused a delivery
        # candidate. A tick that admitted an advisory one still says so.
        if capped:
            _idle(
                {
                    "reason": "daily_cap",
                    "admitted_today": admitted_today,
                    "max_per_day": intake["max_per_day"],
                    "local_listed": local_listed,
                    **(
                        {"github": github_status, "listed": len(issues)}
                        if github_status
                        else {}
                    ),
                }
            )
        return received_all
    except Exception:  # noqa: BLE001 - intake is optional and never stops the lane
        logger.exception("factory autonomous intake failed")
        return []


# Re-exported so a caller reading the loop finds the board's view of it here.
__all__ = [
    "ACTOR",
    "CLASS_LABELS",
    "derive_task_class",
    "github_list",
    "intake_state",
    "intake_tick",
]
