"""Bounded autonomous GitHub issue intake, inert unless policy enables it."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from core.clock import aware
from factory.orchestration.factory_controls import (
    DEFAULT_TASK_CLASS,
    LANES,
    _audit,
    _locked_session,
    _now,
    _read_session,
    delivery_admissions,
    enabled_repos,
    execution_slot_holders,
    unresolved_start_task_ids,
    intake_policy,
    intake_state,
    is_advisory,
    lane_for,
    receipt_task_class,
    repos_map,
)
from factory.orchestration.factory_intake import (
    INTAKE_ACTOR,
    admission_eligible,
    ceiling_below_lanes,
    lane_of,
    open_lanes,
    receive_issue,
)
from factory.orchestration import factory_operational_handoff as handoff
from factory.orchestration import intake_evidence
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
# PR bodies and embedded repository metadata can exceed the 1 MB reader cap
# before a 50-row page fills. Preserve the existing 500 issue / 250 PR bounds
# with smaller responses, rather than treating a shorter scan as complete.
PAGE_SIZE = 25
PULL_PAGE_SIZE = 25
MAX_PAGES = 20
MAX_PULL_PAGES = 10
CANDIDATE_EVIDENCE_LIMIT = 20
# Keep repeated idle messages hourly, independently of successful discovery.
# With room in a lane, a completed sweep can refresh after four minutes.
# That bounds preventable feed delay without reading on every 15-second tick.
# At these page caps, listing costs at most 450 requests/hour/repo
# before admission-triggered sweeps. Failed/incomplete reads remain hourly;
# the shared GitHub cooldown still gates every actual request.
IDLE_AUDIT_SECONDS = 3600
DISCOVERY_REFRESH_SECONDS = 240
RANK_LABELS = ("critical", "bug")
# ADR agents/038 decision 5, read off the issue's own labels. A refine
# candidate is advisory whatever else it carries, because it delivers a
# comment rather than a change. Security findings retain their judgment class
# when agent-ready makes them delivery candidates.
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
    "human",
    "excluded_label",
    "linked_pr",
    "delivered",
    "repository_delivered",
    "delivery_check_deferred",
    "escalated",
    "cooldown",
    "cancelled_issue",
    "landing_held",
    "refinement_failed",
    "already_received",
    "active_issue",
    "blocked",
    "deferred",
    "refine_disabled",
    "outside_refinement_batch",
    "lane_full",
    "local_malformed",
    "local_unnumbered",
)


def github_list(repo: str, suffix: str) -> list:
    """Bounded list read, imported lazily so the board stays off the reconciler.

    factory_controls.status reads the intake block, and the board reads status,
    so a module-scope import here would pull the whole conductor into a page
    render. Keeping the name here also leaves tests one seam to fake.
    """
    from factory.orchestration.factory_conductor import github_list as read

    return read(repo, suffix)


def _pages(
    repo: str, endpoint: str, *, page_size: int = PAGE_SIZE, on_page=None
) -> tuple[list, bool]:
    """Open rows oldest first, and whether the read hit the page cap.

    GitHub defaults to newest first. Asking for ascending order preserves the
    oldest rows that ranking prefers. Hitting the endpoint's row ceiling marks
    the sweep partial; it never establishes absence outside the scanned pages.
    """
    result = []
    truncated = False
    max_pages = MAX_PULL_PAGES if endpoint == "pulls" else MAX_PAGES
    for page in range(1, max_pages + 1):
        rows = github_list(
            repo,
            f"{endpoint}?state=open&sort=created&direction=asc"
            f"&per_page={page_size}&page={page}",
        )
        if not isinstance(rows, list):
            raise ValueError("malformed discovery page")
        if on_page is not None:
            on_page(rows)
        result.extend(rows)
        if len(rows) < page_size:
            break
        truncated = page == max_pages
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
    if any(row.state in ("admitted", "uncertain", "landing") for row in rows):
        return "active_issue"
    if any(row.state == "escalated" for row in rows):
        return "escalated"
    from factory.orchestration.factory_controls import natural_history_exclusion

    protected = natural_history_exclusion(rows)
    if protected is not None:
        return protected
    latest = rows[0] if rows else None
    if (
        latest is not None
        and latest.state in ("failed", "cancelled")
        and aware(latest.updated_at) >= cooldown_cutoff
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
        if last is not None and aware(last.created_at) >= cutoff:
            return
        _audit(db, ACTOR, action, **detail)


def _idle(detail: dict) -> None:
    _throttled("intake_idle", detail)


def _slot_holders(rows, policy: dict, *, unresolved_task_ids=None) -> list:
    """Receipts that really hold a lane slot for this intake tick.

    A queued receipt holds a slot only if admit_next could admit it, which
    is admission_eligible in factory_intake: one intake created itself, or
    one whose issue is on the policy allowlist. An operator receipt outside
    the allowlist can never admit, so counting it let four such receipts
    fill a delivery lane and stall intake with no audit (#6483).
    """
    return execution_slot_holders(
        policy, rows, include_queued=True, unresolved_task_ids=unresolved_task_ids
    )


def _listing_due(now: datetime, *, session=None) -> bool:
    """Whether this tick may spend GitHub reads on a fresh sweep.

    A completed sweep refreshes after four minutes while a lane has room.
    Failed, partial or unfinished attempts retain the hourly retry bound.
    An evaluation must follow the latest attempt before it can shorten that
    bound: an earlier success cannot authorize faster retries of a later
    failure. Settlements and admissions still reopen discovery immediately.

    The clock is its own audit rather than the idle one. An idle row records a
    refusal, which is not the same event as reading GitHub: the daily cap
    writes one for a delivery candidate it turned away, and tying the sweep to
    that would leave the lane blind for an hour after the cap had cleared. A
    settlement recorded in the same second as the sweep re-opens it.

    An admission re-opens it too, and that is what lets a lane fill. A sweep
    takes at most one candidate per lane, so at four delivery and eight
    advisory slots the hourly clock filled the lanes at one slot an hour. When
    the newest admission is at or after the newest sweep, the previous sweep
    found work and there may be more, so the next tick sweeps again. It costs
    at most one extra sweep per admission: the sweep stamps its own clock
    before reading GitHub, so a sweep that admits nothing leaves the newest
    admission behind the newest sweep and the bounded refresh clock governs. The
    caller has already established that a lane has room; a tick with none
    returns before reaching this.
    """
    with _read_session(session) as db:
        last = _latest(db, "intake_swept")
        if last is None:
            return True
        marked = aware(last.created_at)
        interval = IDLE_AUDIT_SECONDS
        evaluated = _latest(db, "intake_evaluated")
        if evaluated is not None and evaluated.id > last.id:
            try:
                detail = json.loads(evaluated.detail_json)
            except (TypeError, ValueError):
                detail = None
            if (
                isinstance(detail, dict)
                and detail.get("github") == "completed"
                and detail.get("truncated") is False
            ):
                interval = DISCOVERY_REFRESH_SECONDS
        if marked <= now - timedelta(seconds=interval):
            return True
        if evaluated is None or evaluated.id < last.id:
            return False
        admitted = _latest(db, "intake_admitted")
        if admitted is not None and aware(admitted.created_at) >= marked:
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
    eligible: set | None = None,
    evidence=None,
) -> tuple[list[dict], dict[str, int], int]:
    """Build candidates from authoritative local rows without a GitHub read."""
    excluded = {reason: 0 for reason in _EXCLUSION_REASONS}
    exclude_labels = {label.lower() for label in intake["exclude_labels"]}
    candidates = []

    # GitHub-authority rows are snapshots, not admission candidates. Until
    # cutover adds equivalent source guards, the live listing owns assignee,
    # linked-PR, and open-state validation. Local authority remains eligible
    # without a GitHub read.
    with _read_session() as db:
        local_items = db.exec(
            select(WorkItem)
            .where(
                WorkItem.authority == "local",
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
        if evidence is not None:
            evidence.observe(
                repo,
                {
                    "number": number,
                    "title": work_item.title,
                    "labels": work_item.labels,
                    "created_at": aware(
                        work_item.github_created_at or work_item.created_at
                    ).isoformat(),
                },
                "local",
            )

        def exclude(reason):
            excluded[reason.split(":", 1)[0]] += 1
            if evidence is not None:
                evidence.reason(repo, number, reason)

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
            exclude("local_malformed")
            _throttled(
                "local_item_malformed",
                {"work_item_id": work_item.id},
            )
            continue
        labels = {label.lower() for label in work_item.labels}
        if "human" in labels:
            exclude("human")
            continue
        if labels & exclude_labels:
            exclude("excluded_label:" + ",".join(sorted(labels & exclude_labels)))
            continue
        if work_item.id in blocked_local_ids:
            exclude("blocked")
            continue
        delivery = work_item.state == "ready"
        if intake["refinement_batch"] and (
            delivery or number not in intake["refinement_batch"]
        ):
            exclude("outside_refinement_batch")
            continue
        refine = not delivery
        task_class, class_reason = derive_task_class(labels, refine=refine)
        rows = [row for row in local_receipts if row.work_item_id == work_item.id]
        if refine and "needs-thought" in labels:
            exclude("deferred")
            continue
        receipt_exclusion = _receipt_exclusion(
            rows,
            generation=generation,
            task_class=task_class,
            cooldown_cutoff=cooldown_cutoff,
        )
        if receipt_exclusion is not None:
            exclude(receipt_exclusion)
            if receipt_exclusion == "cooldown" and evidence is not None:
                evidence.reason(
                    repo,
                    number,
                    "cooldown_until:"
                    + (
                        aware(rows[0].updated_at)
                        + timedelta(hours=intake["cooldown_hours"])
                    ).isoformat(),
                )
            continue
        if refine and not intake["refine_enabled"]:
            exclude("refine_disabled")
            continue
        if type(number) is not int or not 1 <= number <= 2**31 - 1:
            exclude("local_malformed")
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
        if eligible is not None and delivery and "agent-ready" in labels:
            eligible.add((repo, number))
        if not room.get(lane):
            exclude("lane_full")
            continue
        created_at = work_item.github_created_at or work_item.created_at
        item = {
            "number": number,
            "title": title,
            "body": body,
            "html_url": work_item.source_ref
            or f"https://github.com/{repo}/issues/{number}",
            "labels": work_item.labels,
            "created_at": aware(created_at).isoformat(),
            "user": None,
        }
        candidates.append(
            {
                "issue": item,
                "number": number,
                "source": "local",
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


def intake_tick(
    policy: dict,
    *,
    generation: int,
    lanes=LANES,
    defer_evidence=False,
    evidence_sink=None,
    observe_only=False,
) -> list[dict]:
    """Queue at most one issue per open lane, or audit why it queued none.

    The lanes are ranked and filled independently: a delivery candidate and an
    advisory one can both enter on the same tick when both lanes have room,
    and never more than one of either. ``lanes`` lets a caller hold one shut
    without touching the policy.
    """
    evidence = None
    evaluation_complete = False
    local_errors = False
    try:
        policy = {**policy, "generation": generation}
        intake = intake_policy(policy)
        lanes = tuple(lane for lane in LANES if lane in lanes)
        deferred_lanes = tuple(lane for lane in LANES if lane not in lanes)
        if deferred_lanes:
            _throttled(
                "agent_board_lanes_deferred",
                {
                    "deferred_lanes": list(deferred_lanes),
                    "eligible_lanes": list(lanes),
                },
            )
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
            held = _slot_holders(
                db.exec(
                    select(FactoryReceipt).where(
                        FactoryReceipt.state.in_(
                            ("queued", "admitted", "uncertain", "landing")
                        ),
                    )
                ).all(),
                {**policy, "generation": generation},
                unresolved_task_ids=unresolved_start_task_ids(db),
            )
            room = open_lanes(policy, held, lanes)
            if observe_only:
                room = {lane: 0 for lane in LANES}
        # Every enabled repo sweeps under its own intake labels and daily
        # cap; a repo outside the enabled set is not even read. A policy
        # with only the legacy repo reads as that one entry, so today's
        # single-repo behaviour is unchanged.
        repos = enabled_repos(policy)
        if intake["refinement_batch"]:
            # The grant names primary-repository issues. Resuming another repo
            # must not repurpose the same numeric IDs there.
            repos = [repo for repo in repos if repo == policy["repo"]]
        if not repos:
            return []
        entries = repos_map(policy)
        intake_evidence.finalize()
        # Claim the aggregate discovery clock under the same control lock used
        # by admission. Replicas cannot both reserve a due sweep.
        with _locked_session() as (db, _control):
            listing_due = _listing_due(now, session=db)
            if listing_due:
                _audit(db, ACTOR, "intake_swept")
                marker = _latest(db, "intake_swept")
                evidence = intake_evidence.Sweep(marker.id, now)
                evidence.coverage = {repo: "unavailable" for repo in repos}
        with _read_session() as db:
            # Delivery only. The cap bounds delivery churn, and an advisory
            # refine costs cents and produces a comment, so the cap is not
            # consulted for one at all: it must never throttle a burn-down.
            # Counted per repo, which is what that repo's cap bounds.
            admitted_today = {
                slug: delivery_admissions(db, today, slug) for slug in repos
            }
        excluded: dict[str, int] = {reason: 0 for reason in _EXCLUSION_REASONS}

        def exclude(reason: str) -> None:
            excluded[reason] += 1
            if evidence is not None and isinstance(item, dict):
                evidence.reason(slug, item.get("number"), reason)
                if reason == "excluded_label":
                    row = evidence.issues.get((slug, item.get("number")))
                    if row is not None:
                        row["excluded_labels"] = sorted(
                            _label_names(item) & exclude_labels
                        )

        candidates = []
        eligible = set()
        local_errors = False
        cooldown_cutoff = now - timedelta(hours=intake["cooldown_hours"])
        local_listed = 0
        for slug in repos:
            repo_intake = {
                **intake,
                "labels": entries[slug]["labels"],
                "exclude_labels": entries[slug]["exclude_labels"],
            }
            try:
                local_candidates, local_excluded, listed = _local_candidates(
                    slug,
                    intake=repo_intake,
                    generation=generation,
                    room=room,
                    cooldown_cutoff=cooldown_cutoff,
                    eligible=eligible,
                    evidence=evidence,
                )
                for candidate in local_candidates:
                    candidate["repo"] = slug
                candidates.extend(local_candidates)
                for reason, count in local_excluded.items():
                    excluded[reason] += count
                local_listed += listed
            except Exception as exc:  # noqa: BLE001 - GitHub intake must still run
                local_errors = True
                _throttled(
                    "local_intake_error",
                    {"error": type(exc).__name__, "repo": slug},
                )
                logger.warning("factory local intake failed", exc_info=True)

        # The sweep clock records the ATTEMPT, not the result. A sweep that
        # fails is the case most worth rate limiting: a 403 usually means the
        # shared budget is already spent, and retrying every fifteen seconds
        # is how it stays spent. One stamp covers every repo; each repo then
        # reads and syncs on its own, so one repo's failure never blinds the
        # others.
        github_status = None if listing_due else "not_due"
        listed_total = 0
        truncated = False
        per_repo: list[tuple[str, list, list]] = []
        read_failed: list[str] = []
        for slug in repos:
            issues: list = []
            pulls: list = []
            issues_cut = False
            pulls_cut = False
            if listing_due:
                try:

                    def observed_page(rows):
                        for issue in rows:
                            evidence.observe(slug, issue)

                    issues, issues_cut = _pages(slug, "issues", on_page=observed_page)
                    # Reject malformed identities/text before the batch sync.
                    # One invalid row must not poison valid rows or masquerade
                    # as complete coverage (which could close unseen items).
                    from factory.orchestration.work_items import _github_values

                    valid_issues = []
                    for issue in issues:
                        try:
                            if not isinstance(issue, dict):
                                raise ValueError("malformed issue")
                            _github_values(slug, issue)
                        except (TypeError, ValueError, AttributeError):
                            issues_cut = True
                            excluded["malformed"] += 1
                            if isinstance(issue, dict):
                                evidence.reason(slug, issue.get("number"), "malformed")
                        else:
                            valid_issues.append(issue)
                    issues = valid_issues
                    if evidence is not None:
                        evidence.coverage[slug] = (
                            "partial" if issues_cut else "complete"
                        )
                        if any(
                            not isinstance(issue, dict)
                            or type(issue.get("number")) is not int
                            for issue in issues
                        ):
                            evidence.coverage[slug] = "partial"
                            issues_cut = True
                    pulls, pulls_cut = _pages(slug, "pulls", page_size=PULL_PAGE_SIZE)
                    if pulls_cut:
                        # An unseen PR may already own any candidate issue.
                        # Exclude GitHub candidates until coverage is complete.
                        raise ValueError("open PR discovery incomplete")
                except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
                    if evidence is not None:
                        evidence.coverage[slug] = "unavailable"
                        for observation in evidence.issues.values():
                            if (
                                observation["repo"] == slug
                                and observation["source"] == "github"
                            ):
                                observation["reason"] = "discovery_failed"
                    # A 403 from the shared rate limit, or any other read
                    # failure, would otherwise leave intake silently dead: the
                    # blanket handler below logs where nobody looks. Record
                    # the shape of the failure without its response body, URL
                    # or credential-bearing text.
                    _throttled(
                        "intake_error",
                        {
                            "stage": "listing",
                            "error": type(exc).__name__,
                            "status": getattr(
                                getattr(exc, "response", None), "status_code", None
                            ),
                            "repo": slug,
                        },
                    )
                    logger.warning("factory intake listing failed", exc_info=True)
                    issues = []
                    pulls = []
                    truncated = truncated or pulls_cut
                    issues_cut = False
                    pulls_cut = False
                    read_failed.append(slug)
                    per_repo.append((slug, issues, pulls))
                    continue
                try:
                    from factory.orchestration.work_item_links import (
                        reconcile_body_edges,
                    )
                    from factory.orchestration.work_items import sync_github_work_items

                    work_item_counts = sync_github_work_items(
                        slug,
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
                                WorkItem.github_repo == slug,
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
                                slug,
                                items_with_bodies,
                                actor=ACTOR,
                                truncated=issues_cut,
                            )
                            logger.info("work_item_edge_reconcile", extra=edge_counts)
                        db.commit()
                except Exception as exc:  # noqa: BLE001 - intake survives sync failure
                    if evidence is not None:
                        evidence.coverage[slug] = "unavailable"
                        for issue in issues:
                            if isinstance(issue, dict):
                                evidence.reason(
                                    slug, issue.get("number"), "discovery_failed"
                                )
                    logger.exception("work_item_sync_error")
                    _throttled(
                        "work_item_sync_error",
                        {
                            "error": type(exc).__name__,
                            "truncated": issues_cut,
                            "repo": slug,
                        },
                    )
                    read_failed.append(slug)
                    per_repo.append((slug, [], []))
                    continue
            truncated = truncated or issues_cut or pulls_cut
            listed_total += len(issues)
            per_repo.append((slug, issues, pulls))
        if listing_due and len(read_failed) == len(repos):
            github_status = "failed"

        # One budget for the whole sweep, not one per repo: the delivery
        # checks spend the same shared GitHub request budget as the listing.
        delivery_check_budget = {"reads": handoff.INTAKE_READS_PER_SWEEP}
        for slug, issues, pulls in per_repo:
            include_labels = {label.lower() for label in entries[slug]["labels"]}
            exclude_labels = {
                label.lower() for label in entries[slug]["exclude_labels"]
            }
            from factory.orchestration import dependency_prs

            if (
                room.get("delivery")
                and policy.get("base_branch")
                and not intake["refinement_batch"]
            ):
                for pull in pulls:
                    if not dependency_prs.eligible(pull, slug, policy):
                        continue
                    number = pull["number"]
                    with _read_session() as db:
                        existing = db.exec(
                            select(FactoryReceipt).where(
                                FactoryReceipt.repo == slug,
                                FactoryReceipt.issue_number == number,
                                FactoryReceipt.generation == generation,
                                FactoryReceipt.task_class == "judgment-analysis",
                            )
                        ).first()
                    if existing is not None:
                        continue
                    candidates.append(
                        {
                            "issue": pull,
                            "number": number,
                            "source": "dependency_pr",
                            "repo": slug,
                            "work_item_id": None,
                            "lane": "delivery",
                            "task_class": "judgment-analysis",
                            "class_reason": "adversarial dependency PR review",
                            "rank_reason": "dependency PR",
                            "sort": (0, len(RANK_LABELS), _created_rank(pull), number),
                        }
                    )

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
                if (
                    evidence is not None
                    and evidence.issues.get((slug, item.get("number")), {}).get(
                        "source"
                    )
                    == "local"
                ):
                    continue
                labels = _label_names(item)
                if "pull_request" in item:
                    exclude("pull_request")
                elif item.get("state") != "open":
                    exclude("not_open")
                elif "human" in labels:
                    exclude("human")
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
                            WorkItem.github_repo == slug,
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
                        .where(FactoryReceipt.repo == slug, by_number_or_item)
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
                if intake["refinement_batch"] and (
                    delivery or number not in intake["refinement_batch"]
                ):
                    exclude("outside_refinement_batch")
                    continue
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
                    if receipt_exclusion == "active_issue" and evidence is not None:
                        observation = evidence.issues.get((slug, number))
                        if observation is not None:
                            active = next(
                                row
                                for row in rows
                                if row.state in ("admitted", "uncertain", "landing")
                            )
                            observation["receipt_id"] = active.id
                    if receipt_exclusion == "cooldown" and evidence is not None:
                        evidence.reason(
                            slug,
                            number,
                            "cooldown_until:"
                            + (
                                aware(rows[0].updated_at)
                                + timedelta(hours=intake["cooldown_hours"])
                            ).isoformat(),
                        )
                    continue
                if not delivery and not intake["refine_enabled"]:
                    exclude("refine_disabled")
                    continue
                if type(number) is not int or not 1 <= number <= 2**31 - 1:
                    exclude("malformed")
                    continue
                label_rank = next(
                    (
                        index
                        for index, label in enumerate(RANK_LABELS)
                        if label in labels
                    ),
                    len(RANK_LABELS),
                )
                rank_reason = (
                    RANK_LABELS[label_rank]
                    if label_rank < len(RANK_LABELS)
                    else "oldest"
                )
                lane = lane_for(task_class)
                if handoff.enabled():
                    # Finished repository work with only live checks left is
                    # delivered, whatever generation re-read its labels (#6288).
                    handed_off = handoff.intake_exclusion(
                        slug, number, rows, delivery_check_budget
                    )
                    if handed_off is not None:
                        exclude(handed_off)
                        continue
                if delivery and "agent-ready" in labels:
                    eligible.add((slug, number))
                if not room.get(lane):
                    exclude("lane_full")
                    continue
                candidates.append(
                    {
                        "issue": item,
                        "number": number,
                        "source": "github",
                        "repo": slug,
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
        if listing_due:
            # The idle audit has an independent hourly throttle. Its next
            # write can be a not_due tick, and suppress the actual sweep's
            # exclusions forever. Preserve one bounded evaluation per real
            # sweep, separately from both its attempt clock and idle ticks.
            with _locked_session() as (db, _control):
                _audit(
                    db,
                    ACTOR,
                    "intake_evaluated",
                    generation=generation,
                    github=(
                        "failed"
                        if len(read_failed) == len(repos)
                        else "partial"
                        if read_failed
                        else "completed"
                    ),
                    listed=listed_total,
                    local_listed=local_listed,
                    excluded=excluded,
                    candidates={
                        lane: sum(candidate["lane"] == lane for candidate in candidates)
                        for lane in LANES
                    },
                    truncated=truncated,
                )
        if not candidates:
            _idle(
                {
                    "excluded": excluded,
                    "listed": listed_total,
                    "local_listed": local_listed,
                    **({"github": github_status} if github_status else {}),
                    **({"truncated": True} if truncated else {}),
                }
            )
            if listing_due and not observe_only:
                _refill_observation(
                    policy,
                    eligible,
                    not (
                        truncated
                        or read_failed
                        or local_errors
                        or excluded.get("delivery_check_deferred")
                    ),
                    lanes,
                    now,
                )
            evaluation_complete = True
            return []

        # One per lane, in lane-blind rank order, so the delivery lane keeps
        # its precedence over refine while an open advisory lane is not left
        # idle behind a delivery candidate it has nothing to do with.
        # The daily cap is per repo and checked here, while choosing: a
        # capped repo's delivery candidate falls through to the next one in
        # that lane, so a capped repo never takes the slot another repo with
        # room would have used. The advisory lane has nothing to do with what
        # the cap bounds, so it is never consulted for one.
        chosen: list[dict] = []
        taken: set[str] = set()
        capped: dict[str, dict] = {}
        for candidate in candidates:
            if candidate["lane"] in taken:
                continue
            repo = candidate["repo"]
            repo_max_per_day = entries[repo]["max_per_day"]
            if (
                candidate["lane"] == "delivery"
                and admitted_today[repo] >= repo_max_per_day
            ):
                if evidence is not None:
                    evidence.reason(repo, candidate["number"], "daily_cap")
                capped[repo] = {
                    "admitted_today": admitted_today[repo],
                    "max_per_day": repo_max_per_day,
                }
                continue
            taken.add(candidate["lane"])
            chosen.append(candidate)

        received_all = []
        for candidate in chosen:
            if evidence is not None:
                evidence.reason(
                    candidate["repo"], candidate["number"], "evaluation_failed"
                )
            # Re-read the lane under the lock. The room that picked this
            # candidate was measured before a GitHub sweep that takes seconds,
            # and another replica may have filled the lane in between. admit_next
            # is still the real gate; this only stops the lane queueing receipts
            # it already knows it cannot admit.
            with _locked_session() as (db, _control):
                held = _slot_holders(
                    db.exec(
                        select(FactoryReceipt).where(
                            FactoryReceipt.state.in_(
                                ("queued", "admitted", "uncertain", "landing")
                            ),
                        )
                    ).all(),
                    {**policy, "generation": generation},
                    unresolved_task_ids=unresolved_start_task_ids(db),
                )
                room = open_lanes(policy, held, lanes)
                held_counts: dict[str, int] = {}
                for row in held:
                    key = f"{lane_of(row)}:{row.state}"
                    held_counts[key] = held_counts.get(key, 0) + 1
            if not room.get(candidate["lane"]):
                if evidence is not None:
                    evidence.reason(candidate["repo"], candidate["number"], "capacity")
                # The lane filled between the pre-sweep room check and this
                # re-read: another replica, or an earlier candidate this tick
                # admitted above. admit_next is still the real gate; this idle
                # row is how the board shows the skip, throttled like every
                # other idle reason so a full lane writes once an hour.
                _idle(
                    {
                        "reason": "lane_full",
                        "lane": candidate["lane"],
                        "held": held_counts,
                    }
                )
                continue
            delivery = candidate["lane"] == "delivery"
            repo = candidate["repo"]
            issue = candidate["issue"]
            if candidate["source"] == "dependency_pr":
                try:
                    received = dependency_prs.receive(
                        repo, candidate["number"], policy, generation=generation
                    )
                except Exception as exc:  # noqa: BLE001 - fail closed, retry a later sweep
                    _throttled(
                        "dependency_pr_held",
                        {
                            "repo": repo,
                            "pr_number": candidate["number"],
                            "error": type(exc).__name__,
                            "reason": str(exc)[:1000]
                            if isinstance(exc, ValueError)
                            else "GitHub dependency evidence unavailable",
                        },
                    )
                    logger.warning("dependency PR evidence unavailable", exc_info=True)
                    continue
            else:
                if evidence is not None:
                    evidence.reason(repo, candidate["number"], "receipt_failed")
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
            if not received.get("ok") or not received.get("receipt", {}).get("id"):
                if evidence is not None:
                    evidence.reason(repo, candidate["number"], "receipt_refused")
                continue
            if evidence is not None:
                observation = evidence.issues.get((repo, candidate["number"]))
                if observation is not None:
                    observation.update(
                        reason="queued", receipt_id=received["receipt"]["id"]
                    )
            if delivery:
                admitted_today[repo] += 1
            received_all.append(received)
            with _locked_session() as (db, _control):
                _audit(
                    db,
                    ACTOR,
                    "intake_admitted",
                    receipt_id=received["receipt"]["id"],
                    issue_number=candidate["number"],
                    source=candidate["source"],
                    repo=repo,
                    lane=candidate["lane"],
                    task_class=candidate["task_class"],
                    class_reason=candidate["class_reason"],
                    rank_reason=candidate["rank_reason"],
                    candidates=[
                        {
                            "number": other["number"],
                            "source": other["source"],
                            "repo": other["repo"],
                            "lane": other["lane"],
                            "task_class": other["task_class"],
                            "rank_reason": other["rank_reason"],
                        }
                        for other in candidates[:CANDIDATE_EVIDENCE_LIMIT]
                    ],
                    excluded=excluded,
                    admitted_today=admitted_today[repo],
                    **(
                        {
                            "github": github_status,
                            "listed": listed_total,
                            "local_listed": local_listed,
                        }
                        if github_status
                        else {}
                    ),
                    **({"truncated": True} if truncated else {}),
                )
        # Once per tick, and only when the cap actually refused a delivery
        # candidate. A tick that admitted an advisory one still says so. One
        # repo reads exactly today's shape; more than one names each capped
        # repo beside it.
        if capped:
            first = next(iter(capped))
            _idle(
                {
                    "reason": "daily_cap",
                    "admitted_today": capped[first]["admitted_today"],
                    "max_per_day": capped[first]["max_per_day"],
                    "local_listed": local_listed,
                    **({"repos": capped} if len(repos) > 1 else {}),
                    **(
                        {"github": github_status, "listed": listed_total}
                        if github_status
                        else {}
                    ),
                }
            )
        if listing_due and not observe_only:
            _refill_observation(
                policy,
                eligible,
                not (
                    truncated
                    or read_failed
                    or local_errors
                    or excluded.get("delivery_check_deferred")
                ),
                lanes,
                now,
            )
        evaluation_complete = True
        return received_all
    except Exception:  # noqa: BLE001 - intake is optional and never stops the lane
        logger.exception("factory autonomous intake failed")
        return []
    finally:
        if evidence is not None:
            if local_errors or not evaluation_complete:
                evidence.coverage = {
                    repo: "partial" if value == "complete" else value
                    for repo, value in evidence.coverage.items()
                }
            if not evaluation_complete:
                for observation in evidence.issues.values():
                    if observation["reason"] == "not_selected":
                        observation["reason"] = "evaluation_failed"
            evidence.stage()
            if evidence_sink is not None:
                evidence_sink.append(evidence.id)
            if not defer_evidence:
                intake_evidence.finalize()


def _refill_observation(policy, eligible, complete, lanes, observed_at):
    from factory.orchestration.backlog_refill import observe

    observe(
        policy,
        eligible=sum(repo == policy["repo"] for repo, _ in eligible),
        complete=complete,
        lanes=lanes,
        observed_at=observed_at,
    )


# Re-exported so a caller reading the loop finds the board's view of it here.
__all__ = [
    "ACTOR",
    "CLASS_LABELS",
    "derive_task_class",
    "github_list",
    "intake_state",
    "intake_tick",
]
