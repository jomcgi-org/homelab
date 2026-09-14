"""Land an approved factory delivery: arm its merge, then close its issue.

Phase 4 of #6002, and the first factory step that writes to the repository
rather than reading it. Every write here is behind the policy ``auto_merge``
flag, which defaults off, so a lane that has not opted in behaves exactly as
it did before: it verifies the delivery, settles the task, and stops.

Landing is deliberately serial. This repository merges through the GitHub
merge queue, and a queue ejection cascades across every candidate behind the
one that failed, so the lane holds at most one armed pull request at a time
and defers the rest to a later tick.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re

import httpx
from sqlalchemy import or_
from sqlmodel import select

from core.github import GITHUB_API
from factory.orchestration.factory_controls import (
    ADVISORY_CLASSES,
    _audit,
    _locked_session,
    _now,
    _read_session,
    auto_merge_enabled,
    request_landing_recovery,
)
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt

logger = logging.getLogger(__name__)

ACTOR = "factory:landing"
# A sanity cap, not a scheduling policy. Selection already drops everything
# that reached a terminal landing audit, so this only bounds a pathological
# backlog; ordering is oldest first, because arming is serial and the delivery
# that has waited longest takes the free slot.
LANDING_BATCH = 200
# Only for a delivery landing has never touched. Turning the flag on for the
# first time must not stampede over every delivery in the lane's history, but
# anything the lane has already armed is followed to a terminal state whatever
# its age.
LANDING_WINDOW_HOURS = 168
LANDING_ERROR_SECONDS = 3600
# Ejections the lane will absorb before it hands the pull request to a human.
# A queue analysis failure is usually transient and worth one re-arm; an
# invalid merge commit needs a rebase no node here can do, and re-arming into
# that forever would spend the queue on a candidate that cannot pass.
MAX_EJECTIONS = 2
WRITE_TIMEOUT_SECONDS = 15
RESPONSE_LIMIT_BYTES = 1_000_000
# Bound each response below the shared read cap at typical pull body sizes.
# Exhausting the page budget cannot prove the arming slot is free.
OPEN_PULLS_PAGE = 50
OPEN_PULLS_MAX_PAGES = 5
FACTORY_BRANCH_PREFIX = "factory/"
_PR_URL = re.compile(r"/pull/([0-9]+)$")
# Multi-row actions: a delivery can be armed, ejected and armed again, so the
# counts are the state and a one-row-per-action fence would freeze it.
_REPEATABLE = ("merge_armed", "merge_ejected")
_ARM_AUTO_MERGE = """
mutation ArmAutoMerge($pullRequestId: ID!) {
  enablePullRequestAutoMerge(
    input: {pullRequestId: $pullRequestId, mergeMethod: REBASE}
  ) {
    pullRequest {
      number
    }
  }
}
"""
_QUEUE_ENTRIES = """
query FactoryQueueEntries($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on PullRequest {
      id
      mergeQueueEntry { id }
    }
  }
}
"""
_DISARM_AUTO_MERGE = """
mutation DisarmAutoMerge($pullRequestId: ID!) {
  disablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId}) {
    pullRequest {
      number
    }
  }
}
"""


class GraphQLRefused(ValueError):
    """A GraphQL mutation that GitHub answered with an errors array."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def github_get(repo: str, suffix: str) -> dict:
    """Bounded read, imported lazily so a board render never pulls the reconciler.

    The conductor owns the read path and its response bound. Keeping the name
    here leaves tests one seam, exactly as the intake loop does.
    """
    from factory.orchestration.factory_conductor import github_get as read

    return read(repo, suffix)


def github_list(repo: str, suffix: str) -> list:
    """Bounded list read, imported lazily for the same reason as the object read."""
    from factory.orchestration.factory_conductor import github_list as read

    return read(repo, suffix)


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "monolith-factory-landing",
    }
    token = os.environ.get("GITHUB_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _body(response: httpx.Response) -> object:
    if len(response.content) > RESPONSE_LIMIT_BYTES:
        raise ValueError("GitHub response exceeds factory limit")
    return json.loads(response.content or b"{}")


def github_write(
    repo: str, suffix: str, payload: dict, *, method: str = "POST"
) -> object:
    """One bounded write to the configured repository, and nowhere else."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid repository")
    with httpx.Client(timeout=WRITE_TIMEOUT_SECONDS) as client:
        response = client.request(
            method,
            f"{GITHUB_API}/repos/{repo}/{suffix}",
            headers=_headers(),
            json=payload,
        )
        response.raise_for_status()
        return _body(response)


def github_graphql(query: str, variables: dict) -> dict:
    """One bounded GraphQL mutation, with the errors array treated as failure.

    GitHub answers a refused mutation with HTTP 200 and an ``errors`` array, so
    a caller that only checked the status would read a refusal as an arming.
    """
    with httpx.Client(timeout=WRITE_TIMEOUT_SECONDS) as client:
        response = client.post(
            f"{GITHUB_API}/graphql",
            headers=_headers(),
            json={"query": query, "variables": variables},
        )
        response.raise_for_status()
        body = _body(response)
    if not isinstance(body, dict):
        raise ValueError("GitHub returned a non-object")
    errors = body.get("errors")
    if errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        # The type, never the message: a GraphQL message can quote repository
        # content back, and this lands in an audit an operator reads.
        code = first.get("type") or "graphql_error"
        raise GraphQLRefused(str(code)[:64], "GitHub refused the mutation")
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _record(task_id: str, action: str, **detail: object) -> bool:
    """Write one row of this action for this task, at most once. True when it wrote.

    The once-only steps happen once per task, and the reconciler reaches each
    of them on every tick until the next one takes, so the audit table is the
    fence as well as the record. Arming and ejection are not once-only and use
    ``_append``: their counts are what say whether the delivery holds the slot.
    """
    if action in _REPEATABLE:
        raise ValueError(f"{action} is a repeatable landing action")
    with _locked_session() as (db, _control):
        epoch = 0
        if action == "merge_arm_refused":
            epoch = (
                db.exec(
                    select(FactoryAudit.id)
                    .where(
                        FactoryAudit.task_id == task_id,
                        FactoryAudit.action == "landing_recovery_requested",
                    )
                    .order_by(FactoryAudit.id.desc())
                    .limit(1)
                ).first()
                or 0
            )
        existing = db.exec(
            select(FactoryAudit.id).where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action == action,
                FactoryAudit.id > epoch,
            )
        ).first()
        if existing is not None:
            return False
        _audit(db, ACTOR, action, task_id=task_id, **detail)
    return True


def _append(task_id: str, action: str, **detail: object) -> None:
    """Add one row of a repeatable landing action."""
    with _locked_session() as (db, _control):
        _audit(db, ACTOR, action, task_id=task_id, **detail)


def _error(task_id: str, stage: str, exc: Exception) -> None:
    """Record the shape of a failed GitHub call, throttled, and never its body."""
    cutoff = _now() - timedelta(seconds=LANDING_ERROR_SECONDS)
    with _locked_session() as (db, _control):
        last = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action == "landing_error",
            )
            .order_by(FactoryAudit.id.desc())
        ).first()
        if last is None or _aware(last.created_at) < cutoff:
            _audit(
                db,
                ACTOR,
                "landing_error",
                task_id=task_id,
                stage=stage,
                error=type(exc).__name__,
                status=getattr(getattr(exc, "response", None), "status_code", None),
            )
    logger.warning(
        "factory landing %s failed for task %s", stage, task_id, exc_info=True
    )


LANDING_ACTIONS = (
    "merge_armed",
    "merge_ejected",
    "landing_recovery_requested",
    "landing_recovery_round",
    "landing_recovery_skipped",
    "merge_arm_refused",
    "merged",
    "issue_closed",
)


def _landing_state(db, task_ids: list[str]) -> dict[str, dict]:
    """Every landing audit for these tasks, as counts, in one read."""
    state = {
        task_id: {action: [] for action in LANDING_ACTIONS} for task_id in task_ids
    }
    if not task_ids:
        return state
    rows = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id.in_(task_ids),
            FactoryAudit.action.in_(LANDING_ACTIONS),
        )
        .order_by(FactoryAudit.id)
    ).all()
    for row in rows:
        if row.action == "landing_recovery_requested":
            # A newly reviewed settlement starts a fresh arming epoch. Recovery
            # requests retain their own durable cap and all historical audits.
            state[row.task_id] = {action: [] for action in LANDING_ACTIONS}
        state[row.task_id][row.action].append(json.loads(row.detail_json))
    return state


def _delivery_prs(db, task_ids: list[str]) -> dict[str, tuple[int, str | None]]:
    """The pull request each succeeded task delivered, from its settlement evidence.

    Advisory and refine tasks also settle succeeded and carry no pull request,
    so an absent ``pr_url`` is what separates a delivery from a comment.
    """
    result: dict[str, tuple[int, str | None]] = {}
    if not task_ids:
        return result
    rows = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id.in_(task_ids),
            FactoryAudit.action == "finish_task",
        )
        .order_by(FactoryAudit.id.desc())
    ).all()
    for row in rows:
        if row.task_id in result:
            continue
        evidence = json.loads(row.detail_json).get("evidence") or {}
        url = evidence.get("pr_url")
        match = _PR_URL.search(url) if isinstance(url, str) else None
        if match is not None:
            head = evidence.get("head_sha")
            result[row.task_id] = (
                int(match.group(1)),
                head if isinstance(head, str) else None,
            )
    return result


def _deliveries(policy: dict, *, include_refused: bool = False) -> list[dict]:
    """Every delivery whose landing is unfinished, oldest first.

    Selected on landing state, never on recency. Selecting the newest receipts
    of any class let a burst of advisory settlements push an armed but unmerged
    delivery out of the batch, which left it never observed, never merged, its
    issue never closed, and the one-at-a-time holder reading as absent so a
    second pull request was armed behind it.

    Terminal is a refusal, which hands the pull request to a human, or a closed
    issue, which is the last step of a successful landing. A merged delivery
    whose issue is not closed yet is still live work.
    """
    repo = policy["repo"]
    cutoff = _now() - timedelta(hours=LANDING_WINDOW_HOURS)
    with _read_session() as db:
        terminal = select(FactoryAudit.task_id).where(
            FactoryAudit.action == "issue_closed",
            FactoryAudit.task_id.is_not(None),
        )
        rows = db.exec(
            select(FactoryReceipt)
            .where(
                FactoryReceipt.repo == repo,
                FactoryReceipt.state == "succeeded",
                FactoryReceipt.task_id.is_not(None),
                FactoryReceipt.task_id.not_in(terminal),
                # A receipt written before classes existed reads as delivery.
                or_(
                    FactoryReceipt.task_class.is_(None),
                    FactoryReceipt.task_class.not_in(ADVISORY_CLASSES),
                ),
            )
            .order_by(FactoryReceipt.id)
            .limit(LANDING_BATCH)
        ).all()
        task_ids = [row.task_id for row in rows]
        prs = _delivery_prs(db, task_ids)
        state = _landing_state(db, task_ids)
    result = []
    for row in rows:
        delivery = prs.get(row.task_id)
        if delivery is None:
            continue
        audits = state[row.task_id]
        if audits["merge_arm_refused"] and not include_refused:
            continue
        armed, ejected = audits["merge_armed"], audits["merge_ejected"]
        touched = bool(
            armed or ejected or audits["merged"] or audits["merge_arm_refused"]
        )
        if not touched and _aware(row.updated_at) < cutoff:
            continue
        number, head = delivery
        result.append(
            {
                "task_id": row.task_id,
                "issue_number": row.issue_number,
                "pr_number": number,
                # The head the newest arming was measured against, so a branch
                # that moves under an armed pull request can be caught.
                "head_sha": armed[-1].get("head_sha") if armed else head,
                "approved_head_sha": head,
                "recovery_pending": bool(
                    audits["landing_recovery_requested"]
                    and not audits["landing_recovery_round"]
                ),
                "recovery_skipped": bool(audits["landing_recovery_skipped"]),
                "refusal": audits["merge_arm_refused"][-1]
                if audits["merge_arm_refused"]
                else None,
                "armed": len(armed),
                "ejected": len(ejected),
                "merged": bool(audits["merged"]),
                "closed": bool(audits["issue_closed"]),
            }
        )
    return result


def holding(item: dict) -> bool:
    """Whether this delivery is the one armed pull request the lane allows."""
    return item["armed"] > item["ejected"] and not item["merged"]


def arm_eligible(item: dict) -> bool:
    """Whether this delivery may take the arming slot on this tick."""
    return (
        not item["merged"]
        and item["armed"] == item["ejected"]
        and item["ejected"] < MAX_EJECTIONS
    )


def _queued_ids(node_ids: list[str]) -> set[str]:
    """Read queue membership separately from auto-merge, failing closed."""
    if not node_ids:
        return set()
    data = github_graphql(_QUEUE_ENTRIES, {"ids": node_ids})
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != len(node_ids):
        raise ValueError("GitHub returned incomplete queue membership")
    seen = set()
    queued = set()
    for node in nodes:
        if (
            not isinstance(node, dict)
            or node.get("id") not in node_ids
            or "mergeQueueEntry" not in node
        ):
            raise ValueError("GitHub returned invalid queue membership")
        seen.add(node["id"])
        entry = node["mergeQueueEntry"]
        if entry is not None:
            if not isinstance(entry, dict) or not entry.get("id"):
                raise ValueError("GitHub returned invalid queue entry")
            queued.add(node["id"])
    if seen != set(node_ids):
        raise ValueError("GitHub omitted queue membership")
    return queued


def _armed_on_github(repo: str) -> dict | None:
    """A factory pull request somebody else armed, so it still counts as the holder.

    The audit trail only knows what this lane did. An operator arming a factory
    pull request by hand puts it in the same queue, and a second armed
    candidate is exactly what the one-at-a-time rule exists to prevent.
    """
    for page in range(1, OPEN_PULLS_MAX_PAGES + 1):
        pulls = github_list(
            repo,
            f"pulls?state=open&sort=created&direction=asc"
            f"&per_page={OPEN_PULLS_PAGE}&page={page}",
        )
        candidates = []
        for pull in pulls:
            if not isinstance(pull, dict):
                continue
            ref = (pull.get("head") or {}).get("ref")
            if isinstance(ref, str) and ref.startswith(FACTORY_BRANCH_PREFIX):
                if pull.get("auto_merge") is not None:
                    return {"pr_number": pull.get("number"), "task_id": None}
                if not isinstance(pull.get("node_id"), str) or not pull["node_id"]:
                    raise ValueError("GitHub holder has no node id")
                candidates.append(pull)
        queued = _queued_ids([pull["node_id"] for pull in candidates])
        for pull in candidates:
            if pull["node_id"] in queued:
                return {"pr_number": pull.get("number"), "task_id": None}
        if len(pulls) < OPEN_PULLS_PAGE:
            return None
    raise ValueError("GitHub holder listing exceeds factory page limit")


def _notify_stuck(task_id: str, number: int) -> None:
    """One best-effort Discord warning, on the path the conductor already uses."""
    try:
        from agent.api import notify

        asyncio.run(
            notify(
                f"Factory pull request #{number} on task {task_id} exhausted "
                "its bounded landing recovery. Auto-merge is off "
                "and the pull request is left for a human.",
                level="warn",
            )
        )
    except Exception:  # noqa: BLE001 - notification is best effort
        logger.warning(
            "factory landing notification failed for task %s", task_id, exc_info=True
        )


def _refuse(item: dict, reason: str, **detail: object) -> None:
    _record(
        item["task_id"],
        "merge_arm_refused",
        pr_number=item["pr_number"],
        reason=reason,
        **detail,
    )
    item["refused"] = True


def _arm(repo: str, item: dict) -> None:
    number = item["pr_number"]
    try:
        pr = github_get(repo, f"pulls/{number}")
        if pr.get("merged"):
            # Someone merged it by hand between approval and this tick. The
            # rest of the landing still owes the issue its close.
            _record(item["task_id"], "merged", pr_number=number, armed_by_factory=False)
            item["merged"] = True
            return
        if pr.get("state") != "open" or pr.get("draft"):
            _refuse(item, "pull request is not open and ready")
            return
        head = (pr.get("head") or {}).get("sha")
        approved = item.get("approved_head_sha", item["head_sha"])
        if not isinstance(approved, str) or not re.fullmatch(r"[0-9a-f]{40}", approved):
            _refuse(item, "approved_head_missing")
            return
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
            _refuse(item, "pull_request_head_missing", approved_head_sha=approved)
            return
        if head != approved:
            _refuse(item, "head_moved", approved_head_sha=approved, head_sha=head)
            return
        if item.get("recovery_pending") or pr.get("mergeable") is False:
            _recover_delivery(item, head, "delivered_pr")
            return
        node_id = pr.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            _refuse(item, "pull request has no node id")
            return
        github_graphql(_ARM_AUTO_MERGE, {"pullRequestId": node_id})
    except GraphQLRefused as exc:
        # A refused arming is final for this task. Retrying a mutation GitHub
        # has already declined would spend the request budget to be declined
        # again, and the refusal is on the board for an operator to read.
        _refuse(item, exc.code)
        return
    except (httpx.HTTPError, ValueError) as exc:
        _error(item["task_id"], "arm", exc)
        return
    # The head this arming is measured against is the head GitHub has now, not
    # the head the review approved: a branch that moved between approval and
    # arming is caught by the next observation rather than merged quietly.
    _append(
        item["task_id"],
        "merge_armed",
        pr_number=number,
        head_sha=head if isinstance(head, str) else item["head_sha"],
        attempt=item["armed"] + 1,
        merge_method="rebase",
    )
    item["armed"] += 1
    if isinstance(head, str):
        item["head_sha"] = head


def _disarm(repo: str, pr: dict) -> bool:
    """Return whether auto-merge was successfully disabled."""
    node_id = pr.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        return False
    try:
        github_graphql(_DISARM_AUTO_MERGE, {"pullRequestId": node_id})
    except (GraphQLRefused, httpx.HTTPError, ValueError):
        logger.warning("factory landing could not disarm pull request", exc_info=True)
        return False
    return True


def _recover_delivery(
    item: dict, head: object, source: str, *, reason: str = "merge_conflict"
) -> dict:
    item["refused"] = True
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        _refuse(item, "pull_request_head_missing")
        return {"ok": False, "reason": "pull_request_head_missing"}
    result = request_landing_recovery(
        item["task_id"],
        item["pr_number"],
        head,
        source,
        ACTOR,
        reason=reason,
    )
    if result.get("reason") == "recovery_limit":
        _refuse(item, "landing_recovery_exhausted")
        _notify_stuck(item["task_id"], item["pr_number"])
    return result


def _recover_refused(policy: dict) -> None:
    """Reassess one historical refusal per tick without touching other closures."""
    for item in _deliveries(policy, include_refused=True):
        if item.get("recovery_skipped"):
            continue
        refusal = item.get("refusal") or {}
        if not item.get("recovery_pending") and refusal.get("reason") not in (
            "merge_conflict",
            "ejected_from_merge_queue",
        ):
            continue
        try:
            pr = github_get(policy["repo"], f"pulls/{item['pr_number']}")
            if pr.get("merged"):
                _observe(policy["repo"], item)
                _close_issue(policy["repo"], item)
                return
            if pr.get("state") != "open" or pr.get("draft"):
                _record(
                    item["task_id"],
                    "landing_recovery_skipped",
                    pr_number=item["pr_number"],
                    reason="pull_request_not_ready",
                )
                return
            head = (pr.get("head") or {}).get("sha")
            if head != item["approved_head_sha"]:
                _record(
                    item["task_id"],
                    "landing_recovery_skipped",
                    pr_number=item["pr_number"],
                    reason="head_moved",
                )
                return
            if pr.get("auto_merge") is not None or pr.get("node_id") in _queued_ids(
                [pr["node_id"]]
            ):
                return
            _recover_delivery(
                item,
                head,
                "merge_queue",
                reason=(
                    "merge_conflict"
                    if pr.get("mergeable") is False
                    else "queue_ejection"
                ),
            )
            return
        except (httpx.HTTPError, ValueError) as exc:
            _error(item["task_id"], "recover", exc)
            return


def _observe(repo: str, item: dict) -> None:
    number = item["pr_number"]
    try:
        pr = github_get(repo, f"pulls/{number}")
    except (httpx.HTTPError, ValueError) as exc:
        _error(item["task_id"], "observe", exc)
        return
    if pr.get("merged"):
        _record(
            item["task_id"],
            "merged",
            pr_number=number,
            merge_commit_sha=pr.get("merge_commit_sha"),
            armed_by_factory=True,
            # The hook phase 4 still owes. Confirming that the chart version
            # write-back landed and that the new image is live is a verify
            # node, not a field, and this is where its verdict will be read
            # from once that node exists (#6002).
            rollout_verified=None,
        )
        item["merged"] = True
        return
    if pr.get("state") == "closed":
        _refuse(item, "pull request closed without merging")
        return
    head = (pr.get("head") or {}).get("sha")
    if isinstance(head, str) and item["head_sha"] and head != item["head_sha"]:
        # Somebody pushed under an armed pull request. Whatever the review
        # approved is not what would merge, so the arming comes off and the
        # delivery goes back to a human rather than to the queue.
        _disarm(repo, pr)
        _refuse(item, "head_moved", armed_head_sha=item["head_sha"], head_sha=head)
        return
    if pr.get("mergeable") is False:
        try:
            node_id = pr.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                raise ValueError("GitHub holder has no node id")
            if node_id in _queued_ids([node_id]):
                return
        except (httpx.HTTPError, ValueError) as exc:
            _error(item["task_id"], "observe_queue", exc)
            return
        # GitHub accepts auto-merge on conflicting PRs without queueing them.
        # Release the slot only after any outstanding auto-merge is disabled.
        if pr.get("auto_merge") is not None and not _disarm(repo, pr):
            return
        _recover_delivery(item, head, "merge_queue")
        return
    if pr.get("auto_merge") is not None:
        return
    try:
        node_id = pr.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("GitHub holder has no node id")
        if node_id in _queued_ids([node_id]):
            return
    except (httpx.HTTPError, ValueError) as exc:
        _error(item["task_id"], "observe_queue", exc)
        return
    # The worker assesses the actual queue/check evidence before deciding
    # whether to retry, rebase, correct code, or escalate.
    _recover_delivery(item, head, "merge_queue", reason="queue_ejection")


def _close_issue(repo: str, item: dict) -> None:
    issue, number = item["issue_number"], item["pr_number"]
    try:
        current = github_get(repo, f"issues/{issue}")
        closed_here = current.get("state") == "open"
        if closed_here:
            github_write(
                repo,
                f"issues/{issue}/comments",
                {
                    "body": (
                        f"Closed by the factory against the observed merge of "
                        f"pull request #{number}."
                    )
                },
            )
            github_write(
                repo,
                f"issues/{issue}",
                {"state": "closed", "state_reason": "completed"},
                method="PATCH",
            )
    except (httpx.HTTPError, ValueError) as exc:
        _error(item["task_id"], "close", exc)
        return
    _record(
        item["task_id"],
        "issue_closed",
        issue_number=issue,
        pr_number=number,
        closed_by_factory=closed_here,
    )
    item["closed"] = True


def _defer(item: dict, holder: dict) -> None:
    with _locked_session() as (db, _control):
        previous = db.exec(
            select(FactoryAudit.detail_json).where(
                FactoryAudit.task_id == item["task_id"],
                FactoryAudit.action == "merge_deferred",
            )
        ).all()
        if any(
            json.loads(raw).get("blocked_by_pr") == holder["pr_number"]
            for raw in previous
        ):
            return
        _audit(
            db,
            ACTOR,
            "merge_deferred",
            task_id=item["task_id"],
            pr_number=item["pr_number"],
            blocked_by_pr=holder["pr_number"],
            blocked_by_task_id=holder["task_id"],
        )


def landing_tick(policy: dict) -> None:
    """Advance every settled delivery one landing step, arming at most one.

    Observation runs before arming on purpose: an armed pull request that
    merged during this tick frees the single arming slot in the same tick that
    records the merge, rather than holding the next delivery for another
    fifteen seconds.
    """
    try:
        if not auto_merge_enabled(policy):
            return
        repo = policy["repo"]
        _recover_refused(policy)
        deliveries = _deliveries(policy)
        for item in deliveries:
            item["refused"] = False
            if holding(item):
                _observe(repo, item)
            if item["merged"] and not item["closed"]:
                _close_issue(repo, item)
        live = [item for item in deliveries if not item["refused"]]
        holder = next((item for item in live if holding(item)), None)
        waiting = [item for item in live if arm_eligible(item)]
        if not waiting:
            return
        if holder is None:
            # Nothing this lane armed is outstanding, but an operator may have
            # armed a factory pull request by hand and it sits in the same
            # queue. Bounded list reads, only when there is something to arm.
            try:
                holder = _armed_on_github(repo)
            except (httpx.HTTPError, ValueError) as exc:
                # Not knowing whether a factory pull request is already armed
                # is not a licence to arm a second one.
                _error(waiting[0]["task_id"], "holder", exc)
                return
        if holder is not None:
            for item in waiting:
                _defer(item, holder)
            return
        # A capacity-blocked recovery must not block unrelated ready PRs.
        # Bound reads while allowing a refused candidate to release this tick.
        for index, first in enumerate(waiting[:5]):
            _arm(repo, first)
            if first["merged"] and not first["closed"]:
                _close_issue(repo, first)
            if holding(first) and not first.get("refused"):
                for item in waiting[index + 1 :]:
                    _defer(item, first)
                break
            if not first.get("refused") and not first["merged"]:
                break
    except Exception:  # noqa: BLE001 - landing is optional and never stops the lane
        logger.exception("factory landing failed")


# Keep stored exceptions readable by replicas on either side of a deploy.
GraphQLRefused.__module__ = "swarm.factory_landing"
