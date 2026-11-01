"""Land an approved factory delivery: arm its merge, then close its issue.

Phase 4 of #6002, and the first factory step that writes to the repository
rather than reading it. Merge mutations are behind the policy ``auto_merge``
flag, while review check publication has its own environment gate. Both default
off. Merge observation and rollout reconciliation still follow deliveries
merged by another actor; observing a merge never enables arming.
Exact-head conflicts outside the queue can enter bounded repository repair
while merging is disabled, subject to the existing execution and hold fences.

Landing is deliberately serial. This repository merges through the GitHub
merge queue, and a queue ejection cascades across every candidate behind the
one that failed, so the lane holds at most one armed pull request at a time
and defers the rest to a later tick.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
import os
import re

import httpx
from sqlalchemy import or_
from sqlmodel import select

from core.clock import aware
from core.github import GITHUB_API
from factory.orchestration.factory_controls import (
    ADVISORY_CLASSES,
    _audit,
    _locked_session,
    _now,
    _read_session,
    auto_merge_enabled,
    request_landing_recovery,
    finish_task,
)
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt
from factory.orchestration.factory_controls import (
    operator_landing_target as operator_target,
)
from factory.orchestration import factory_controls as controls

logger = logging.getLogger(__name__)

ACTOR = "factory:landing"
# A sanity cap, not a scheduling policy. Selection already drops everything
# that reached a terminal landing audit, so this only bounds a pathological
# backlog; ordering is oldest first, because arming is serial and the delivery
# that has waited longest takes the free slot.
LANDING_BATCH = 200
REVIEW_PUBLISH_BATCH = 5
OBSERVATION_BATCH = 10
_delivery_cursors: dict[str, int] = {}
# Only for a delivery landing has never touched. Turning the flag on for the
# first time must not stampede over every delivery in the lane's history, but
# anything the lane has already armed is followed to a terminal state whatever
# its age.
LANDING_WINDOW_HOURS = 168
LANDING_ERROR_SECONDS = 3600
ROLLOUT_WAIT_REPORT_MINUTES = 30
ROLLOUT_ESCALATE_MINUTES = 60
# Ejections the lane will absorb before it hands the pull request to a human.
# A queue analysis failure is usually transient and worth one re-arm; an
# invalid merge commit needs a rebase no node here can do, and re-arming into
# that forever would spend the queue on a candidate that cannot pass.
MAX_EJECTIONS = 2
WRITE_TIMEOUT_SECONDS = 15
RESPONSE_LIMIT_BYTES = 1_000_000
# Bound each response below the shared read cap at typical pull body sizes.
# Exhausting the page budget cannot prove the arming slot is free.
OPEN_PULLS_PAGE = 25
OPEN_PULLS_MAX_PAGES = 10
FACTORY_BRANCH_PREFIX = "factory/"
_PR_URL = re.compile(r"/pull/([0-9]+)$")
# Multi-row actions: a delivery can be armed, ejected and armed again, so the
# counts are the state and a one-row-per-action fence would freeze it.
_REPEATABLE = ("merge_armed", "merge_ejected", "landing_recovery_declined")
_ARM_AUTO_MERGE = """
mutation ArmAutoMerge($pullRequestId: ID!, $expectedHeadOid: GitObjectID!) {
  enablePullRequestAutoMerge(
    input: {pullRequestId: $pullRequestId, mergeMethod: REBASE, expectedHeadOid: $expectedHeadOid}
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
_DEQUEUE_PULL_REQUEST = """
mutation HumanHandoffDequeue($pullRequestId: ID!) {
  dequeuePullRequest(input: {id: $pullRequestId}) {
    mergeQueueEntry { id }
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
        if last is None or aware(last.created_at) < cutoff:
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


# Check states that read as a failed run, from either the commit status API or
# the check-runs API. Anything not failed and not passed is still pending.
_CI_FAILED = frozenset(
    {"failure", "error", "cancelled", "timed_out", "action_required", "stale"}
)
_CI_PASSED = frozenset({"success", "neutral", "skipped"})


def _ci_conclusion(states: list[str]) -> str:
    """Fold check states into one: any failure fails, all passed passes."""
    if not states:
        return "none"
    if any(state in _CI_FAILED for state in states):
        return "failure"
    if all(state in _CI_PASSED for state in states):
        return "success"
    return "pending"


def _record_ci(repo: str, item: dict, pr: dict) -> None:
    if operator_target(item["task_id"]):
        return
    """Best effort: record what CI said about the head that merged.

    Written as its own once-only ``merge_ci`` row after ``merged``, never
    inside it, so a slow or failing read cannot delay or lose the merge
    record. A read that fails still writes the row, with the conclusion
    ``unknown`` and the exception type, because a merged delivery leaves the
    landing batch once its issue closes and would never be asked again.
    """
    head = (pr.get("head") or {}).get("sha")
    detail: dict = {
        "pr_number": item["pr_number"],
        "head_sha": head if isinstance(head, str) else None,
        "ejections": item.get("ejected", 0),
    }
    try:
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise ValueError("merged pull request has no head sha")
        status = github_get(repo, f"commits/{head}/status")
        runs = github_get(repo, f"commits/{head}/check-runs?per_page=100")
        checks = {
            str(entry.get("context")): str(entry.get("state"))
            for entry in status.get("statuses") or []
            if isinstance(entry, dict)
        }
        for run in runs.get("check_runs") or []:
            if isinstance(run, dict):
                checks[str(run.get("name"))] = str(
                    run.get("conclusion") or run.get("status")
                )
        detail["checks"] = checks
        detail["conclusion"] = _ci_conclusion(list(checks.values()))
    except Exception as exc:  # noqa: BLE001 - CI evidence never blocks landing
        logger.warning(
            "factory landing could not read CI for task %s",
            item["task_id"],
            exc_info=True,
        )
        detail["conclusion"] = "unknown"
        detail["error"] = type(exc).__name__
    try:
        _record(item["task_id"], "merge_ci", **detail)
    except Exception:  # noqa: BLE001 - nor does failing to write it
        logger.warning(
            "factory landing could not record CI for task %s",
            item["task_id"],
            exc_info=True,
        )


LANDING_ACTIONS = (
    "owned_pr_merged",
    "merge_armed",
    "merge_ejected",
    "landing_recovery_requested",
    "landing_recovery_round",
    "landing_recovery_skipped",
    "merge_arm_refused",
    "merged",
    "rollout_verified",
    "rollout_observed",
    "rollout_escalated",
    "issue_closed",
    "repository_delivery_complete",
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
            FactoryAudit.action.in_(("finish_task", "delivery_ready")),
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


def _deliveries(
    policy: dict, *, include_refused: bool = False, rotate: bool = False, session=None
) -> list[dict]:
    """Every delivery whose landing is unfinished, oldest first.

    Selected on landing state, never on recency. Selecting the newest receipts
    of any class let a burst of advisory settlements push an armed but unmerged
    delivery out of the batch, which left it never observed, never merged, its
    issue never closed, and the one-at-a-time holder reading as absent so a
    second pull request was armed behind it.

    Arming refuses previously rejected work. Observation revisits refusals
    because another actor may subsequently merge the PR. Closed issues and
    completed repository deliveries leave the scan.
    """
    repo = policy["repo"]
    cutoff = _now() - timedelta(hours=LANDING_WINDOW_HOURS)
    with _read_session(session) as db:
        terminal = select(FactoryAudit.task_id).where(
            FactoryAudit.action.in_(("issue_closed", "repository_delivery_complete")),
            FactoryAudit.task_id.is_not(None),
        )
        query = (
            select(FactoryReceipt)
            .where(
                FactoryReceipt.repo == repo,
                or_(
                    FactoryReceipt.state.in_(("succeeded", "landing")),
                    FactoryReceipt.task_id == controls.OPERATOR_DOCS_HANDOFF_TARGET[1],
                ),
                FactoryReceipt.task_id.is_not(None),
                or_(
                    FactoryReceipt.task_id.not_in(terminal),
                    FactoryReceipt.task_id == controls.OPERATOR_DOCS_HANDOFF_TARGET[1],
                ),
                # A receipt written before classes existed reads as delivery.
                or_(
                    FactoryReceipt.task_class.is_(None),
                    FactoryReceipt.task_class.not_in(ADVISORY_CLASSES),
                ),
            )
            .order_by(FactoryReceipt.id)
        )
        if rotate:
            cursor = _delivery_cursors.get(repo, 0)
            rows = db.exec(
                query.where(FactoryReceipt.id > cursor).limit(OBSERVATION_BATCH)
            ).all()
            if not rows:
                rows = db.exec(query.limit(OBSERVATION_BATCH)).all()
            _delivery_cursors[repo] = rows[-1].id if rows else 0
        else:
            rows = db.exec(query.limit(LANDING_BATCH)).all()
        task_ids = [row.task_id for row in rows]
        prs = _delivery_prs(db, task_ids)
        state = _landing_state(db, task_ids)
        merged_at = {}
        if task_ids:
            merges = db.exec(
                select(FactoryAudit)
                .where(
                    FactoryAudit.task_id.in_(task_ids),
                    FactoryAudit.action == "merged",
                )
                .order_by(FactoryAudit.id)
            ).all()
            merged_at = {row.task_id: aware(row.created_at) for row in merges}
    result = []
    for row in rows:
        delivery = prs.get(row.task_id)
        if operator_target(row.task_id):
            delivery = (
                controls.OPERATOR_DOCS_HANDOFF_TARGET[3],
                controls.OPERATOR_DOCS_HANDOFF_ACCEPTED_HEAD,
            )
        if delivery is None:
            continue
        audits = state[row.task_id]
        from factory.orchestration.factory_controls import landing_held

        if (
            audits["merge_arm_refused"]
            and not include_refused
            and not landing_held(row)
        ):
            continue
        armed, ejected = audits["merge_armed"], audits["merge_ejected"]
        touched = bool(
            armed or ejected or audits["merged"] or audits["merge_arm_refused"]
        )
        if (
            row.state != "landing"
            and not touched
            and not landing_held(row)
            and not operator_target(row.task_id)
            and aware(row.updated_at) < cutoff
        ):
            continue
        number, head = delivery
        result.append(
            {
                "task_id": row.task_id,
                "paused": row.task_paused,
                "owned_merge_observed": bool(audits["owned_pr_merged"]),
                "issue_number": row.issue_number,
                "conductor_gates": (
                    json.loads(row.direction_json) if row.direction_json else {}
                ).get("conductor_gates", []),
                "dependency_review": (
                    json.loads(row.direction_json) if row.direction_json else {}
                ).get("dependency_review"),
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
                "merged_at": merged_at.get(row.task_id),
                "merge_commit_sha": audits["merged"][-1].get("merge_commit_sha")
                if audits["merged"]
                else None,
                "rollout_verified": bool(audits["rollout_verified"]),
                "rollout_escalated": bool(audits["rollout_escalated"]),
                "rollout_observation": audits["rollout_observed"][-1]
                if audits["rollout_observed"]
                else None,
                "closed": bool(
                    audits["issue_closed"] or audits["repository_delivery_complete"]
                ),
            }
        )
    return result


def _rollout_wait(item: dict, now: datetime) -> dict | None:
    merged_at = item.get("merged_at")
    if not item.get("merged") or item.get("rollout_verified") or item.get("closed"):
        return None
    if merged_at is None:
        return None
    waited = int((now - aware(merged_at)).total_seconds() // 60)
    if waited < ROLLOUT_WAIT_REPORT_MINUTES:
        return None
    observation = item.get("rollout_observation") or {}
    return {
        "task_id": item["task_id"],
        "issue_number": item["issue_number"],
        "pr_number": item["pr_number"],
        "merged_at": aware(merged_at).isoformat(),
        "waited_minutes": waited,
        **{key: observation.get(key) for key in ("reason", "application", "resource")},
    }


def rollout_waits(policy: dict, *, session=None) -> list[dict]:
    """Bounded durable landing evidence only, safe for status readers."""
    if not policy.get("repo"):
        return []
    now = _now()
    return [
        wait
        for item in _deliveries(policy, include_refused=True, session=session)
        if (wait := _rollout_wait(item, now)) is not None
    ]


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
    from factory.orchestration.factory_conductor import _notify_person_once

    _notify_person_once(
        task_id,
        f"Factory pull request #{number} on task {task_id} exhausted "
        "its bounded landing recovery. Auto-merge is off "
        "and the pull request is left for a human.",
        kind="landing",
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
    if operator_target(item["task_id"]):
        return
    from factory.orchestration.factory_controls import review_recovery_held as held

    if _enforce_landing_hold(repo, item):
        return
    if held(item["task_id"]):
        _refuse(item, "review_only_recovery")
        return
    number = item["pr_number"]
    try:
        pr = github_get(repo, f"pulls/{number}")
        if pr.get("merged"):
            _record_merged(repo, item, pr)
            return
        if pr.get("state") != "open" or pr.get("draft"):
            _refuse(item, "pull request is not open and ready")
            return
        if item.get("dependency_review"):
            # A pre-queue assessment cannot authorize an eventual queue base.
            # The default-off queue publisher does not authorize activation.
            # Keep this lane inspection-only pending #6799 live acceptance,
            # including the same-repository CI credential boundary.
            _refuse(item, "dependency_auto_merge_disabled")
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
        from factory import review_publisher
        from factory.orchestration import factory_landing_release as releases
        from factory.orchestration.factory_controls import (
            _receipt,
            repo_auto_merge_enabled,
        )

        with _read_session() as db:
            refusal = releases.scoped_refusal(
                db, _receipt(db, item["task_id"]), number, head
            )
        if refusal:
            _scope_wait(item, refusal)
            return
        # Publication can be disabled; independent current-head review cannot.
        evidence = review_publisher.collect(item["task_id"])
        if not isinstance(evidence, review_publisher.ReviewEvidence) or (
            evidence.repo,
            evidence.pr_number,
            evidence.head_sha,
        ) != (repo, number, head):
            _scope_wait(item, "current_independent_review_missing")
            return
        from factory.orchestration.factory_controls import _locked_session

        with _locked_session() as (db, _control):
            refusal = releases.scoped_refusal(
                db, _receipt(db, item["task_id"]), number, head
            )
            if _control.state != "enabled" or not repo_auto_merge_enabled(
                json.loads(_control.policy_json or "{}"), repo
            ):
                refusal = "automatic_landing_disabled"
            if not refusal:
                current = github_get(repo, f"pulls/{number}")
                from factory.orchestration import factory_conductor as conductor
                from factory.orchestration.models import SwarmTask

                row = _receipt(db, item["task_id"])
                task = db.get(SwarmTask, item["task_id"]).model_dump()
                direction = json.loads(row.direction_json or "{}")
                task.update(
                    {
                        key: direction[key]
                        for key in ("delivery_branch", "delivery_adoption")
                        if key in direction
                    }
                )
                if (
                    (current.get("head") or {}).get("sha") != head
                    or current.get("state") != "open"
                    or current.get("draft")
                    or current.get("merged")
                    or current.get("node_id") != node_id
                    or current.get("number") != number
                    or not conductor.delivery_identity_matches(task, current)
                ):
                    refusal = "scoped_landing_authorization_identity_changed"
            if not refusal:
                github_graphql(
                    _ARM_AUTO_MERGE, {"pullRequestId": node_id, "expectedHeadOid": head}
                )
        if refusal:
            _scope_wait(item, refusal)
            return
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


def _scope_wait(item: dict, reason: str) -> None:
    # Missing authority is a wait for an explicit grant, not permission to
    # erase an unrelated permanent refusal. Existing refusal handling is intact.
    item["refused"] = True
    _record(
        item["task_id"],
        "landing_authorization_wait",
        reason=reason,
        pr_number=item["pr_number"],
    )


def _fence_operator_queue_entry(repo, item):
    from factory.orchestration import factory_landing_release as releases
    from factory.orchestration.factory_controls import _receipt

    try:
        with _locked_session() as (db, control):
            # Re-observe under the same lock as apply. A stale tick must never
            # dequeue a newly authorized head using an older PR snapshot.
            pr = github_get(repo, f"pulls/{controls.OPERATOR_DOCS_HANDOFF_TARGET[3]}")
            if pr.get("merged") or pr.get("state") != "open":
                return False
            node_id = pr.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                raise ValueError("operator delivery has no node id")
            queued = node_id in _queued_ids([node_id])
            if pr.get("auto_merge") is None and not queued:
                return False
            head = pr.get("head", {}).get("sha")
            row = _receipt(db, item["task_id"])
            refusal = releases.scoped_refusal(
                db, row, controls.OPERATOR_DOCS_HANDOFF_TARGET[3], head
            )
            if not refusal:
                refusal = releases.operator_queue_refusal(db, control, row, pr)
            if not refusal:
                return False
            if pr.get("auto_merge") is not None and not _disarm(repo, pr):
                return True
            if queued:
                github_graphql(_DEQUEUE_PULL_REQUEST, {"pullRequestId": node_id})
                if node_id in _queued_ids([node_id]):
                    raise ValueError("operator delivery remains queued")
        _scope_wait(item, refusal)
    except (httpx.HTTPError, ValueError) as exc:
        _error(item["task_id"], "operator_landing", exc)
        item["refused"] = True
    return True


def _fence_ungranted_queue_entry(repo: str, item: dict, pr: dict | None = None) -> bool:
    """Observe bounded known deliveries before policy/paused/refusal filters."""
    from factory import review_publisher
    from factory.orchestration import factory_landing_release as releases
    from factory.orchestration.factory_controls import _receipt

    if operator_target(item["task_id"]):
        return _fence_operator_queue_entry(repo, item)
    try:
        pr = pr if pr is not None else github_get(repo, f"pulls/{item['pr_number']}")
        if pr.get("merged") or pr.get("state") != "open":
            return False
        node_id = pr.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("ungranted holder has no node id")
        queued = node_id in _queued_ids([node_id])
        if pr.get("auto_merge") is None and not queued:
            return False
        head = (pr.get("head") or {}).get("sha")
        with _read_session() as db:
            refusal = releases.scoped_refusal(
                db, _receipt(db, item["task_id"]), item["pr_number"], head
            )
        if not refusal:
            evidence = review_publisher.collect(item["task_id"])
            if not isinstance(evidence, review_publisher.ReviewEvidence) or (
                evidence.repo,
                evidence.pr_number,
                evidence.head_sha,
            ) != (repo, item["pr_number"], head):
                refusal = "current_independent_review_missing"
        if not refusal:
            return False
        if pr.get("auto_merge") is not None and not _disarm(repo, pr):
            return True
        if queued:
            github_graphql(_DEQUEUE_PULL_REQUEST, {"pullRequestId": node_id})
            if node_id in _queued_ids([node_id]):
                raise ValueError("ungranted delivery remains queued")
        if holding(item) and head != item["head_sha"]:
            _refuse(item, "head_moved", armed_head_sha=item["head_sha"], head_sha=head)
        else:
            _scope_wait(item, refusal)
    except (httpx.HTTPError, ValueError) as exc:
        _error(item["task_id"], "scoped_landing", exc)
        item["refused"] = True
    return True


def _recover_delivery(
    item: dict, head: object, source: str, *, reason: str = "merge_conflict"
) -> dict:
    if _enforce_landing_hold(item.get("repo", ""), item):
        return {"ok": False, "reason": "receipt_landing_held"}
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
    elif not result.get("ok") and not result.get("pending"):
        # An approved head with a conflict that nothing repairs is the stuck
        # state a person has to read off the board, so name the refusal there
        # without refusing the delivery: the next tick retries it.
        _record_declined(item, str(result.get("reason")))
    return result


def _record_declined(item: dict, reason: str) -> None:
    """One audit per distinct refusal in a recovery round, not one per tick.

    A new recovery request or settlement starts a new round, so the same
    refusal after it is recorded again rather than hidden behind the old row.
    """
    from factory.orchestration.factory_controls import _read_session
    from factory.orchestration.factory_models import FactoryAudit

    with _read_session() as db:
        last = db.exec(
            select(FactoryAudit.id, FactoryAudit.detail_json)
            .where(
                FactoryAudit.task_id == item["task_id"],
                FactoryAudit.action == "landing_recovery_declined",
            )
            .order_by(FactoryAudit.id.desc())
            .limit(1)
        ).first()
        round_start = db.exec(
            select(FactoryAudit.id)
            .where(
                FactoryAudit.task_id == item["task_id"],
                FactoryAudit.action.in_(
                    ("landing_recovery_requested", "finish_task", "delivery_ready")
                ),
            )
            .order_by(FactoryAudit.id.desc())
            .limit(1)
        ).first()
    if (
        last is not None
        and json.loads(last[1]).get("reason") == reason
        and (round_start is None or round_start < last[0])
    ):
        return
    _append(
        item["task_id"],
        "landing_recovery_declined",
        pr_number=item["pr_number"],
        reason=reason,
    )


def _recover_refused(policy: dict) -> None:
    """Reassess one historical refusal per tick without touching other closures."""
    for item in _deliveries(policy, include_refused=True):
        if operator_target(item["task_id"]):
            continue
        if _enforce_landing_hold(policy["repo"], item) or item.get("paused"):
            continue
        if item.get("recovery_skipped"):
            continue
        refusal = item.get("refusal") or {}
        if not item.get("recovery_pending") and refusal.get("reason") not in (
            "merge_conflict",
            "ejected_from_merge_queue",
        ):
            continue
        if _handoff_human_delivery(policy["repo"], item):
            continue
        try:
            pr = github_get(policy["repo"], f"pulls/{item['pr_number']}")
            if pr.get("merged"):
                if _record_merged(policy["repo"], item, pr):
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


def _recover_conflict_without_arming(repo: str, item: dict) -> None:
    if operator_target(item["task_id"]):
        return
    """Repair an observed conflict without enabling or disturbing merge entry.

    A disabled merge policy still permits bounded repository repair. Only the
    exact reviewed head, outside auto-merge and the queue, can enter the existing
    recovery path. Other refusals and unknown GitHub evidence remain closed.
    """
    refusal = item.get("refusal") or {}
    if (
        item["merged"]
        or item.get("dependency_review")
        or item.get("recovery_skipped")
        or refusal.get("reason")
        not in (
            None,
            "merge_conflict",
            "ejected_from_merge_queue",
        )
    ):
        return
    try:
        pr = github_get(repo, f"pulls/{item['pr_number']}")
        head = (pr.get("head") or {}).get("sha")
        node_id = pr.get("node_id")
        if (
            pr.get("merged")
            or pr.get("state") != "open"
            or pr.get("draft")
            or pr.get("mergeable") is not False
            or head != item.get("approved_head_sha")
            or not isinstance(node_id, str)
            or not node_id
            or pr.get("auto_merge") is not None
            or node_id in _queued_ids([node_id])
        ):
            return
        _recover_delivery(item, head, "delivered_pr")
    except (httpx.HTTPError, ValueError) as exc:
        _error(item["task_id"], "recover", exc)


def _record_merged(repo: str, item: dict, pr: dict) -> bool:
    """Observation is not permission to accept a different delivery head."""
    if operator_target(item["task_id"]):
        return False
    from factory.orchestration import factory_conductor as conductor

    from factory.orchestration.models import SwarmTask
    from factory.orchestration.factory_controls import granted_delivery_surface

    with _read_session() as db:
        row = db.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == item["task_id"])
        ).one()
        task = db.get(SwarmTask, item["task_id"]).model_dump()
        direction = json.loads(row.direction_json or "{}")
        branch, _number = granted_delivery_surface(direction)
        if branch:
            task["delivery_branch"] = branch
            task["delivery_adoption"] = direction.get("delivery_adoption")
    approved = item.get("approved_head_sha", item["head_sha"])
    if (
        not pr.get("merged")
        or not conductor.delivery_identity_matches(task, pr)
        or not isinstance(approved, str)
        or not re.fullmatch(r"[0-9a-f]{40}", approved)
        or (pr.get("head") or {}).get("sha") != approved
        or not re.fullmatch(r"[0-9a-f]{40}", pr.get("merge_commit_sha") or "")
    ):
        _refuse(item, "merged_delivery_identity_mismatch")
        return False
    detail = {
        "pr_number": item["pr_number"],
        "merge_commit_sha": pr["merge_commit_sha"],
        "armed_by_factory": bool(item["armed"]),
    }
    if item["armed"]:
        detail["rollout_verified"] = None
    if _record(item["task_id"], "merged", **detail):
        _record_ci(repo, item, pr)
    item["merged"] = True
    item["merge_commit_sha"] = pr["merge_commit_sha"]
    return True


def _observe_merge(repo: str, item: dict) -> None:
    if operator_target(item["task_id"]):
        return
    """Read merged state even when publication or a previous arming refused."""
    if item["merged"]:
        return
    try:
        pr = github_get(repo, f"pulls/{item['pr_number']}")
        if pr.get("merged"):
            _record_merged(repo, item, pr)
    except (httpx.HTTPError, ValueError) as exc:
        _error(item["task_id"], "observe_merge", exc)


def _observe(repo: str, item: dict) -> None:
    if operator_target(item["task_id"]):
        return
    number = item["pr_number"]
    try:
        pr = github_get(repo, f"pulls/{number}")
    except (httpx.HTTPError, ValueError) as exc:
        _error(item["task_id"], "observe", exc)
        return
    if pr.get("merged"):
        _record_merged(repo, item, pr)
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
    if _fence_ungranted_queue_entry(repo, item, pr):
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


def _verify_rollout(repo: str, item: dict) -> bool:
    """One bounded observation per minute; unknown evidence keeps landing open."""
    if operator_target(item["task_id"]):
        return False
    from factory.orchestration import factory_rollout

    if item.get("rollout_verified"):
        return True
    previous = item.get("rollout_observation") or {}
    if previous.get("observed_at"):
        observed = datetime.fromisoformat(previous["observed_at"])
        if (_now() - aware(observed)).total_seconds() < 60:
            return False
    merge_sha = item.get("merge_commit_sha")
    if not merge_sha:
        try:
            pr = github_get(repo, f"pulls/{item['pr_number']}")
            if pr.get("merged"):
                merge_sha = pr.get("merge_commit_sha")
        except (httpx.HTTPError, ValueError) as exc:
            _error(item["task_id"], "rollout_identity", exc)
            return False
    result = factory_rollout.verify(repo, merge_sha, item["pr_number"])
    if result["verified"]:
        _record(
            item["task_id"],
            "rollout_verified",
            pr_number=item["pr_number"],
            approved_head_sha=item["approved_head_sha"],
            **result,
        )
        item["rollout_verified"] = True
        return True
    detail = {
        **result,
        "observed_at": _now().isoformat(),
        "pr_number": item["pr_number"],
    }
    _append(item["task_id"], "rollout_observed", **detail)
    item["rollout_observation"] = detail
    wait = _rollout_wait(item, _now())
    if wait is not None:
        task_id = wait["task_id"]
        _record(
            task_id,
            "rollout_wait_reported",
            **{key: value for key, value in wait.items() if key != "task_id"},
        )
        if wait["waited_minutes"] >= ROLLOUT_ESCALATE_MINUTES and not item.get(
            "rollout_escalated"
        ):
            from factory.orchestration.factory_conductor import _notify_person_once

            application = wait["application"] or "unknown"
            resource = wait["resource"] or "unknown"
            reason = wait["reason"] or "unknown"
            if _notify_person_once(
                task_id,
                f"Factory PR #{wait['pr_number']} (issue #{wait['issue_number']}) "
                f"has waited {wait['waited_minutes']} minutes on rollout: "
                f"{application} {resource} ({reason}). Landing keeps polling.",
                kind="rollout",
            ):
                _record(task_id, "rollout_escalated", pr_number=item["pr_number"])
                item["rollout_escalated"] = True
    return False


def _close_issue(repo: str, item: dict) -> None:
    if operator_target(item["task_id"]):
        return
    issue, number = item["issue_number"], item["pr_number"]
    from factory.orchestration.factory_gates import live_checks, pending_live_validation
    from factory.orchestration.factory_conductor import closes_issue

    if not _verify_rollout(repo, item):
        return
    with _read_session() as db:
        ready = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == item["task_id"],
                FactoryAudit.action == "delivery_ready",
            )
            .order_by(FactoryAudit.id.desc())
        ).first()
        evidence = json.loads(ready.detail_json).get("evidence") if ready else None
        receipt = db.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == item["task_id"])
        ).one()
        direction = json.loads(receipt.direction_json) if receipt.direction_json else {}
        # The delivery snapshot may predate a gate recorded during this tick.
        item["conductor_gates"] = [
            *(item.get("conductor_gates") or []),
            *direction.get("conductor_gates", []),
        ]
    if ready is not None:
        result = finish_task(item["task_id"], "succeeded", ACTOR, evidence=evidence)
        if not result["ok"] or result.get("state", "succeeded") != "succeeded":
            return
    if live_checks(item):
        _record(
            item["task_id"],
            "repository_delivery_complete",
            issue_number=issue,
            pr_number=number,
            closed_by_factory=False,
            operational_acceptance_pending=True,
        )
        item["closed"] = True  # Landing is complete; the issue remains open.
        return
    if item.get("dependency_review"):
        _record(
            item["task_id"],
            "repository_delivery_complete",
            pr_number=number,
            closed_by_factory=False,
            dependency_review=True,
        )
        item["closed"] = True
        return
    try:
        current = github_get(repo, f"issues/{issue}")
        closed_here = current.get("state") == "open"
        if closed_here:
            pr = github_get(repo, f"pulls/{number}")
            # Missing durable gates are not evidence that acceptance is complete.
            # A repository-only PR must never acquire issue-closing authority just
            # because the infrastructure rollout succeeded.
            if pending_live_validation(pr.get("body")) or not closes_issue(
                pr.get("body"), repo, issue
            ):
                _record(
                    item["task_id"],
                    "repository_delivery_complete",
                    issue_number=issue,
                    pr_number=number,
                    closed_by_factory=False,
                    operational_acceptance_pending=True,
                    acceptance_evidence="pr_does_not_confirm_issue_completion",
                )
                item["closed"] = True
                return
            github_write(
                repo,
                f"issues/{issue}/comments",
                {
                    "body": (
                        f"Closed by the factory after verifying publication and "
                        f"the managed live rollout of pull request #{number}."
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


def _publish_review_gate(item: dict) -> bool:
    """Publish the exact-head review check before giving this PR to merge."""
    if operator_target(item["task_id"]):
        return False
    from factory import review_publisher

    evidence = review_publisher.collect(item["task_id"])
    result = review_publisher.publish(evidence)
    if result["action"] in ("published", "already_published"):
        if result["action"] == "published":
            _append(
                item["task_id"],
                "review_published",
                pr_number=item["pr_number"],
                head_sha=result["head_sha"],
                review_run_id=result["review_run_id"],
                check_id=result["check_id"],
                conclusion=result["conclusion"],
            )
        if isinstance(evidence, review_publisher.ReviewEvidence):
            return result["conclusion"] == "success"
    reason = (
        evidence.reason
        if isinstance(evidence, review_publisher.Refusal)
        else result.get("reason", "review_publish_failed")
    )
    _append(
        item["task_id"],
        "review_publish_refused",
        pr_number=item["pr_number"],
        reason=reason,
    )
    _refuse(item, "review_not_published", review_reason=reason)
    return False


def _handoff_human_delivery(repo: str, item: dict) -> bool:
    """Fence landing, disarm any pending merge and leave the PR with its owner.

    An unreadable ownership or unsuccessful disarm also blocks this tick.
    """
    if operator_target(item["task_id"]):
        return True
    from factory.orchestration.factory_controls import (
        HUMAN_HANDOFF_EVIDENCE,
        human_handoff,
    )
    from factory.orchestration.factory_intake_loop import _label_names

    repo = item.get("repo", repo)
    try:
        owned = human_handoff(item["task_id"], ACTOR)
        if not owned:
            issue = github_get(repo, f"issues/{item['issue_number']}")
            owned = "human" in _label_names(issue)
            if owned:
                human_handoff(item["task_id"], ACTOR, observed=True)
        if not owned:
            return False
        pull = github_get(repo, f"pulls/{item['pr_number']}")
        if not pull.get("merged"):
            node_id = pull.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                raise ValueError("human handoff pull request has no node id")
            if pull.get("auto_merge") and not _disarm(repo, pull):
                return True
            # Queue membership remains possible with auto_merge=None.
            if node_id in _queued_ids([node_id]):
                github_graphql(_DEQUEUE_PULL_REQUEST, {"pullRequestId": node_id})
                if node_id in _queued_ids([node_id]):
                    raise ValueError("human handoff pull request remains queued")
        finish_task(
            item["task_id"], "cancelled", ACTOR, evidence=HUMAN_HANDOFF_EVIDENCE
        )
        return True
    except (httpx.HTTPError, ValueError) as exc:
        _error(item["task_id"], "human_ownership", exc)
        return True


def _enforce_landing_hold(repo: str, item: dict) -> bool:
    """Re-read the receipt before any landing mutation and disarm existing work."""
    from factory.orchestration import factory_controls as controls

    if operator_target(item["task_id"]):
        try:
            with controls._locked_session() as (db, _control):
                row = controls._receipt(db, item["task_id"])
                if not controls.landing_held(row):
                    return False
                pull = github_get(row.repo, f"pulls/{item['pr_number']}")
                node_id = pull.get("node_id")
                if pull.get("merged") or not node_id:
                    return True
                if pull.get("auto_merge") is not None and not _disarm(row.repo, pull):
                    return True
                if node_id in _queued_ids([node_id]):
                    github_graphql(_DEQUEUE_PULL_REQUEST, {"pullRequestId": node_id})
                    if node_id in _queued_ids([node_id]):
                        raise ValueError("held operator delivery remains queued")
            return True
        except (httpx.HTTPError, ValueError) as exc:
            _error(item["task_id"], "landing_hold", exc)
            return True
    try:
        with controls._read_session() as db:
            row = controls._receipt(db, item["task_id"])
            if not controls.landing_held(row):
                return False
            repo = row.repo
        pull = github_get(repo, f"pulls/{item['pr_number']}")
        if pull.get("merged"):
            _refuse(item, "held_delivery_already_merged")
            return True
        node_id = pull.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("held delivery pull request has no node id")
        if pull.get("auto_merge") is not None and not _disarm(repo, pull):
            return True
        if node_id in _queued_ids([node_id]):
            github_graphql(_DEQUEUE_PULL_REQUEST, {"pullRequestId": node_id})
            if node_id in _queued_ids([node_id]):
                raise ValueError("held delivery remains queued")
        _refuse(item, "receipt_landing_held")
        return True
    except (httpx.HTTPError, ValueError) as exc:
        _error(item["task_id"], "landing_hold", exc)
        return True


def landing_tick(policy: dict) -> None:
    """Advance every settled delivery one landing step, arming at most one.

    Observation runs before arming on purpose: an armed pull request that
    merged during this tick frees the single arming slot in the same tick that
    records the merge, rather than holding the next delivery for another
    fifteen seconds.
    """
    try:
        from factory import review_publisher

        merge_enabled = auto_merge_enabled(policy)
        publish_enabled = review_publisher.enabled()
        deliveries = [
            item
            for item in _deliveries(policy, include_refused=True, rotate=True)
            if not _enforce_landing_hold(policy["repo"], item)
            and not _fence_ungranted_queue_entry(policy["repo"], item)
            and not operator_target(item["task_id"])
            and (not item.get("paused") or item.get("owned_merge_observed"))
        ]
        repo = policy["repo"]
        if merge_enabled:
            _recover_refused(policy)
        # Hold cleanup also runs while global merging and publishing are off.
        deliveries = [
            item for item in deliveries if not _handoff_human_delivery(repo, item)
        ]
        for item in deliveries:
            item["refused"] = bool(item.get("refusal"))
            _observe_merge(repo, item)
            if item["merged"] and not item["closed"]:
                _close_issue(repo, item)
        # A paused delivery with an observed owned merge can finish verification,
        # but can never publish a review, recover, or arm an unmerged PR.
        deliveries = [item for item in deliveries if not item.get("paused")]
        if not merge_enabled:
            for item in deliveries:
                _recover_conflict_without_arming(repo, item)
        if publish_enabled:
            candidates = [
                item
                for item in deliveries
                if not item["refused"] and arm_eligible(item)
            ]
            for item in candidates[:REVIEW_PUBLISH_BATCH]:
                _publish_review_gate(item)
            for item in candidates[REVIEW_PUBLISH_BATCH:]:
                item["review_publish_pending"] = True
        if not merge_enabled:
            return
        for item in deliveries:
            if holding(item) and not item["refused"]:
                _observe(repo, item)
            if item["merged"] and not item["closed"]:
                _close_issue(repo, item)
        live = [item for item in deliveries if not item["refused"]]
        holder = next((item for item in live if holding(item)), None)
        waiting = [
            item
            for item in live
            if arm_eligible(item) and not item.get("review_publish_pending")
        ]
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
