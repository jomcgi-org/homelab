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

from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re

import httpx
from sqlmodel import select

from core.github import GITHUB_API
from swarm.factory_controls import (
    _audit,
    _locked_session,
    _now,
    _read_session,
    auto_merge_enabled,
)
from swarm.factory_models import FactoryAudit, FactoryReceipt

logger = logging.getLogger(__name__)

ACTOR = "factory:landing"
# The newest settled deliveries this tick considers. Landing follows approval
# by minutes, so a receipt that is not in this window has already landed or
# has been left to a human, and re-reading the whole history every fifteen
# seconds would buy nothing.
LANDING_BATCH = 20
LANDING_WINDOW_HOURS = 168
LANDING_ERROR_SECONDS = 3600
WRITE_TIMEOUT_SECONDS = 15
RESPONSE_LIMIT_BYTES = 1_000_000
_PR_URL = re.compile(r"/pull/([0-9]+)$")
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
    from swarm.factory_conductor import github_get as read

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

    Every landing step happens once per task, and the reconciler reaches each
    of them on every tick until the next one takes, so the audit table is the
    fence as well as the record.
    """
    with _locked_session() as (db, _control):
        existing = db.exec(
            select(FactoryAudit.id).where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action == action,
            )
        ).first()
        if existing is not None:
            return False
        _audit(db, ACTOR, action, task_id=task_id, **detail)
    return True


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


def _audited(db, task_id: str, action: str) -> bool:
    return (
        db.exec(
            select(FactoryAudit.id).where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action == action,
            )
        ).first()
        is not None
    )


def _delivery_pr(db, task_id: str) -> tuple[int, str | None] | None:
    """The pull request a succeeded task delivered, from its settlement evidence.

    Advisory and refine tasks also settle succeeded and carry no pull request,
    so an absent ``pr_url`` is what separates a delivery from a comment.
    """
    rows = db.exec(
        select(FactoryAudit.detail_json)
        .where(
            FactoryAudit.task_id == task_id,
            FactoryAudit.action == "finish_task",
        )
        .order_by(FactoryAudit.id.desc())
    ).all()
    for raw in rows:
        evidence = json.loads(raw).get("evidence") or {}
        url = evidence.get("pr_url")
        match = _PR_URL.search(url) if isinstance(url, str) else None
        if match is not None:
            head = evidence.get("head_sha")
            return int(match.group(1)), head if isinstance(head, str) else None
    return None


def _deliveries(policy: dict) -> list[dict]:
    repo = policy["repo"]
    cutoff = _now() - timedelta(hours=LANDING_WINDOW_HOURS)
    result = []
    with _read_session() as db:
        rows = db.exec(
            select(FactoryReceipt)
            .where(
                FactoryReceipt.repo == repo,
                FactoryReceipt.state == "succeeded",
                FactoryReceipt.task_id.is_not(None),
            )
            .order_by(FactoryReceipt.id.desc())
            .limit(LANDING_BATCH)
        ).all()
        # Oldest first among the newest batch: arming is serial, so the
        # delivery that has waited longest takes the free slot.
        for row in reversed(rows):
            if _aware(row.updated_at) < cutoff:
                continue
            delivery = _delivery_pr(db, row.task_id)
            if delivery is None:
                continue
            number, head = delivery
            result.append(
                {
                    "task_id": row.task_id,
                    "issue_number": row.issue_number,
                    "pr_number": number,
                    "head_sha": head,
                    "armed": _audited(db, row.task_id, "merge_armed"),
                    "refused": _audited(db, row.task_id, "merge_arm_refused"),
                    "merged": _audited(db, row.task_id, "merged"),
                    "closed": _audited(db, row.task_id, "issue_closed"),
                }
            )
    return result


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
            _record(
                item["task_id"],
                "merge_arm_refused",
                pr_number=number,
                reason="pull request is not open and ready",
            )
            item["refused"] = True
            return
        node_id = pr.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            _record(
                item["task_id"],
                "merge_arm_refused",
                pr_number=number,
                reason="pull request has no node id",
            )
            item["refused"] = True
            return
        github_graphql(_ARM_AUTO_MERGE, {"pullRequestId": node_id})
    except GraphQLRefused as exc:
        # A refused arming is final for this task. Retrying a mutation GitHub
        # has already declined would spend the request budget to be declined
        # again, and the refusal is on the board for an operator to read.
        _record(item["task_id"], "merge_arm_refused", pr_number=number, reason=exc.code)
        item["refused"] = True
        return
    except (httpx.HTTPError, ValueError) as exc:
        _error(item["task_id"], "arm", exc)
        return
    _record(
        item["task_id"],
        "merge_armed",
        pr_number=number,
        head_sha=item["head_sha"],
        merge_method="rebase",
    )
    item["armed"] = True


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
    elif pr.get("state") == "closed":
        _record(
            item["task_id"],
            "merge_arm_refused",
            pr_number=number,
            reason="pull request closed without merging",
        )
        item["refused"] = True


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
                        f"Closed by the factory: pull request #{number} merged. "
                        "The pull request body carried no closing keyword, so "
                        "this issue is being closed against the merge instead."
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
        deliveries = _deliveries(policy)
        for item in deliveries:
            if item["armed"] and not item["merged"] and not item["refused"]:
                _observe(repo, item)
            if item["merged"] and not item["closed"]:
                _close_issue(repo, item)
        holder = next(
            (
                item
                for item in deliveries
                if item["armed"] and not item["merged"] and not item["refused"]
            ),
            None,
        )
        for item in deliveries:
            if item["armed"] or item["refused"] or item["merged"]:
                continue
            if holder is not None:
                _defer(item, holder)
                return
            _arm(repo, item)
            if item["merged"] and not item["closed"]:
                # Arming found it already merged by hand. Close the issue in
                # this tick rather than holding it for another fifteen
                # seconds behind a step that has already run.
                _close_issue(repo, item)
            return
    except Exception:  # noqa: BLE001 - landing is optional and never stops the lane
        logger.exception("factory landing failed")
