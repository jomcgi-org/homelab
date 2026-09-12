"""Bounded autonomous GitHub issue intake, inert unless policy enables it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import re

from sqlmodel import select

from swarm.factory_controls import (
    DEFAULT_TASK_CLASS,
    LANES,
    _audit,
    is_advisory,
    receipt_task_class,
    _locked_session,
    _now,
    _read_session,
    delivery_admissions,
    intake_policy,
    intake_state,
    lane_for,
)
from swarm.factory_intake import (
    INTAKE_ACTOR,
    ceiling_below_lanes,
    open_lanes,
    receive_issue,
)
from swarm.factory_models import FactoryAudit, FactoryReceipt

logger = logging.getLogger(__name__)

# One name for the identity admission also keys on, so the receipts this
# module writes and the receipts admit_next will accept can never diverge.
ACTOR = INTAKE_ACTOR
PAGE_SIZE = 100
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
    "refine_disabled",
    "lane_full",
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


def _pages(repo: str, endpoint: str) -> tuple[list, bool]:
    """Open rows oldest first, and whether the read hit the page cap.

    GitHub defaults to newest first, so a repository with more open rows than
    MAX_PAGES * PAGE_SIZE would truncate exactly the oldest ones, which is the
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
            f"&per_page={PAGE_SIZE}&page={page}",
        )
        result.extend(rows)
        if len(rows) < PAGE_SIZE:
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


def _throttled(action: str, detail: dict) -> None:
    """Write one audit of this action per hour, and drop the rest.

    A quiet lane reaches the same conclusion every fifteen seconds. Recording
    each one would bury the audit trail under rows that all say what the first
    already said.
    """
    cutoff = _now() - timedelta(seconds=IDLE_AUDIT_SECONDS)
    with _locked_session() as (db, _control):
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

        if not _listing_due(now):
            return []

        repo = policy["repo"]
        # The clock records the ATTEMPT, not the result. A sweep that fails is
        # the case most worth rate limiting: a 403 usually means the shared
        # budget is already spent, and retrying every fifteen seconds is how
        # it stays spent.
        with _locked_session() as (db, _control):
            _audit(db, ACTOR, "intake_swept")
        try:
            issues, issues_cut = _pages(repo, "issues")
            pulls, pulls_cut = _pages(repo, "pulls")
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
            return []
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

        excluded: dict[str, int] = {reason: 0 for reason in _EXCLUSION_REASONS}

        def exclude(reason: str) -> None:
            excluded[reason] += 1

        include_labels = {label.lower() for label in intake["labels"]}
        exclude_labels = {label.lower() for label in intake["exclude_labels"]}
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
            if any(
                row.state == "succeeded" and not is_advisory(receipt_task_class(row))
                for row in rows
            ):
                exclude("delivered")
                continue
            # An escalated receipt is a question in front of a person. The
            # issue carries needs-human, which the default exclusion list
            # already drops, but an operator who takes that label out must not
            # have the lane start a second attempt on the same work while the
            # first one's decision is still open.
            if any(row.state == "escalated" for row in rows):
                exclude("escalated")
                continue
            if (
                latest is not None
                and latest.state in ("failed", "cancelled")
                and _aware(latest.updated_at) >= cooldown_cutoff
            ):
                exclude("cooldown")
                continue
            delivery = bool(labels & include_labels)
            refine = not delivery
            task_class, class_reason = derive_task_class(labels, refine=refine)
            # Scoped to the class, not just the generation. A refine pass that
            # moved an issue to agent-ready has changed what the lane can do
            # with it, and waiting for an operator to bump the generation
            # would strand the readiness the lane just produced.
            if any(
                row.generation == generation and receipt_task_class(row) == task_class
                for row in rows
            ):
                exclude("already_received")
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
                    lane=candidate["lane"],
                    task_class=candidate["task_class"],
                    class_reason=candidate["class_reason"],
                    rank_reason=candidate["rank_reason"],
                    candidates=[
                        {
                            "number": other["number"],
                            "lane": other["lane"],
                            "task_class": other["task_class"],
                            "rank_reason": other["rank_reason"],
                        }
                        for other in candidates[:CANDIDATE_EVIDENCE_LIMIT]
                    ],
                    excluded=excluded,
                    admitted_today=admitted_today,
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
