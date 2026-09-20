"""Retire factory pull requests that no longer have an active delivery owner."""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import quote

import httpx
from sqlalchemy import or_
from sqlmodel import select

from factory.orchestration.factory_controls import (
    _audit,
    _locked_session,
    _read_session,
    delivery_branch_owner,
    granted_delivery_surface,
)
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt
from factory.orchestration.models import SwarmNodeRun

logger = logging.getLogger(__name__)

ACTOR = "factory:pr-lifecycle"
FACTORY_BRANCH_PREFIX = "factory/"
PR_SWEEP_BATCH = 20
PR_SWEEP_INTERVAL_SECONDS = 900
SETTLEMENT_BATCH = 20
COMMENT_PAGE_SIZE = 100
COMMENT_MAX_PAGES = 3
_ACTIVE_STATES = ("admitted", "uncertain")
_SETTLED_PR_STATES = ("escalated", "cancelled", "failed")
_PR_URL = re.compile(r"/pull/([0-9]+)$")
_CLOSE_KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_CONVERT_TO_DRAFT = """
mutation ConvertFactoryPullToDraft($pullRequestId: ID!) {
  convertPullRequestToDraft(input: {pullRequestId: $pullRequestId}) {
    pullRequest { number isDraft }
  }
}
"""
_last_sweep_at: float | None = None


def github_get(repo: str, suffix: str) -> dict:
    from factory.orchestration.factory_conductor import github_get as read

    return read(repo, suffix)


def github_list(repo: str, suffix: str) -> list:
    from factory.orchestration.factory_conductor import github_list as read

    return read(repo, suffix)


def github_write(
    repo: str, suffix: str, payload: dict, *, method: str = "POST"
) -> object:
    from factory.orchestration.factory_landing import github_write as write

    return write(repo, suffix, payload, method=method)


def github_graphql(query: str, variables: dict) -> dict:
    from factory.orchestration.factory_landing import github_graphql as write

    return write(query, variables)


def closing_issue_numbers(body: object, repo: str) -> list[int]:
    """Same-repository issues this body asks GitHub to close on merge."""
    if not isinstance(body, str):
        return []
    reference = (
        rf"(?:(?:https?://github\.com/{re.escape(repo)}/issues/)|"
        rf"(?:{re.escape(repo)})?#)([0-9]+)\b"
    )
    pattern = rf"(?<![A-Za-z0-9_]){_CLOSE_KEYWORD}\s*:?\s+{reference}"
    numbers = []
    for match in re.finditer(pattern, body, re.IGNORECASE):
        number = int(match.group(1))
        if 1 <= number <= 2**31 - 1 and number not in numbers:
            numbers.append(number)
    return numbers


def _direction(row: FactoryReceipt) -> dict:
    try:
        value = json.loads(row.direction_json) if row.direction_json else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _owned_surface(row: FactoryReceipt) -> tuple[str, int | None]:
    direction = _direction(row)
    branch, number = granted_delivery_surface(direction)
    return branch or f"factory/{row.task_id}", number


def _active_receipts(repo: str, issue_number: int) -> list[FactoryReceipt]:
    with _read_session() as db:
        return list(
            db.exec(
                select(FactoryReceipt).where(
                    FactoryReceipt.repo == repo,
                    FactoryReceipt.issue_number == issue_number,
                    FactoryReceipt.state.in_(_ACTIVE_STATES),
                    FactoryReceipt.task_id.is_not(None),
                )
            ).all()
        )


def _same_repo_factory_pull(pr: object, repo: str) -> bool:
    if not isinstance(pr, dict):
        return False
    head = pr.get("head") or {}
    return (
        pr.get("state") == "open"
        and isinstance(head.get("ref"), str)
        and head["ref"].startswith(FACTORY_BRANCH_PREFIX)
        and (head.get("repo") or {}).get("full_name") == repo
    )


def _pull_for_owner(repo: str, row: FactoryReceipt) -> dict | None:
    branch, number = _owned_surface(row)
    if number is not None:
        pull = github_get(repo, f"pulls/{number}")
        return pull if _same_repo_factory_pull(pull, repo) else None
    owner = repo.split("/", 1)[0]
    pulls = github_list(
        repo,
        "pulls?state=open&head="
        + quote(f"{owner}:{branch}", safe="")
        + "&per_page=20&page=1",
    )
    matches = [
        pull
        for pull in pulls
        if _same_repo_factory_pull(pull, repo)
        and (pull.get("head") or {}).get("ref") == branch
    ]
    if len(matches) > 1:
        raise ValueError("running task owns more than one open pull request")
    return matches[0] if matches else None


def _successor(
    repo: str, issue_number: int, candidate_number: int
) -> tuple[dict, FactoryReceipt] | None:
    """A fresh newer PR whose exact branch is held by a running receipt."""
    survivors = []
    for row in _active_receipts(repo, issue_number):
        pull = _pull_for_owner(repo, row)
        if pull is None:
            continue
        number = pull.get("number")
        if (
            type(number) is int
            and number > candidate_number
            and issue_number in closing_issue_numbers(pull.get("body"), repo)
        ):
            survivors.append((pull, row))
    return min(survivors, key=lambda pair: pair[0]["number"]) if survivors else None


def _owner_of_pull(repo: str, pull: dict) -> str | None:
    branch = (pull.get("head") or {}).get("ref")
    if not isinstance(branch, str):
        return None
    with _read_session() as db:
        return delivery_branch_owner(db, repo, branch)


def _comment_marker(number: int, action: str) -> str:
    return f"<!-- factory-pr-{action}:{number} -->"


def _ensure_comment(repo: str, number: int, marker: str, text: str) -> None:
    for page in range(1, COMMENT_MAX_PAGES + 1):
        comments = github_list(
            repo,
            f"issues/{number}/comments?per_page={COMMENT_PAGE_SIZE}&page={page}",
        )
        if any(
            isinstance(comment, dict) and marker in str(comment.get("body") or "")
            for comment in comments
        ):
            return
        if len(comments) < COMMENT_PAGE_SIZE:
            github_write(
                repo,
                f"issues/{number}/comments",
                {"body": f"{marker}\n{text}"},
            )
            return
    raise ValueError("pull request comment discovery incomplete")


def _record(action: str, *, task_id: str | None = None, **detail: object) -> None:
    with _locked_session() as (db, _control):
        _audit(db, ACTOR, action, task_id=task_id, **detail)


def _record_retirement_intent(repo: str, number: int, **detail: object) -> None:
    """Persist the close authority once before the non-transactional API write."""
    repo_fragment = f'"repo":{json.dumps(repo)}'
    number_fragment = f'"pr_number":{number}'
    with _locked_session() as (db, _control):
        previous = db.exec(
            select(FactoryAudit.detail_json).where(
                FactoryAudit.action == "factory_pr_retired",
                FactoryAudit.detail_json.contains(repo_fragment),
                or_(
                    FactoryAudit.detail_json.contains(number_fragment + ","),
                    FactoryAudit.detail_json.contains(number_fragment + "}"),
                ),
            )
        ).all()
        for raw in previous:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if value.get("repo") == repo and value.get("pr_number") == number:
                return
        _audit(
            db,
            ACTOR,
            "factory_pr_retired",
            repo=repo,
            pr_number=number,
            phase="close_requested",
            **detail,
        )


def _latest_sweep_page(repo: str) -> int:
    with _read_session() as db:
        rows = db.exec(
            select(FactoryAudit.detail_json)
            .where(FactoryAudit.action == "factory_pr_sweep_progress")
            .order_by(FactoryAudit.id.desc())
            .limit(20)
        ).all()
    for raw in rows:
        try:
            detail = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if detail.get("repo") == repo and type(detail.get("next_page")) is int:
            return max(1, detail["next_page"])
    return 1


def _record_sweep_page(repo: str, page: int, count: int) -> None:
    next_page = page + 1 if count == PR_SWEEP_BATCH else 1
    _record(
        "factory_pr_sweep_progress",
        repo=repo,
        page=page,
        examined=count,
        next_page=next_page,
    )


def _retirement_reason(
    repo: str, pull: dict
) -> tuple[int, dict | None, FactoryReceipt | None] | None:
    number = pull["number"]
    closed_issue = None
    survivor = None
    survivor_row = None
    survivor_issue = None
    for issue_number in closing_issue_numbers(pull.get("body"), repo):
        issue = github_get(repo, f"issues/{issue_number}")
        found = _successor(repo, issue_number, number)
        if found is not None and survivor is None:
            survivor, survivor_row = found
            survivor_issue = issue_number
        if issue.get("state") == "closed" and closed_issue is None:
            closed_issue = issue_number
    if survivor is not None and survivor_issue is not None:
        return survivor_issue, survivor, survivor_row
    if closed_issue is not None:
        return closed_issue, None, None
    return None


def _retire_pull(repo: str, listed: dict) -> None:
    number = listed.get("number")
    if type(number) is not int or number <= 0:
        return
    pull = github_get(repo, f"pulls/{number}")
    if not _same_repo_factory_pull(pull, repo):
        return
    if _owner_of_pull(repo, pull) is not None:
        return
    reason = _retirement_reason(repo, pull)
    if reason is None:
        return
    issue_number, survivor, survivor_row = reason
    if survivor is not None:
        survivor_number = survivor["number"]
        survivor_branch = survivor["head"]["ref"]
        text = (
            f"Factory lifecycle retired PR #{number} because issue #{issue_number} "
            f"has newer PR #{survivor_number} on `{survivor_branch}`, owned by "
            f"running task `{survivor_row.task_id}`. PR #{survivor_number} remains "
            "open and was not modified."
        )
    else:
        survivor_number = None
        survivor_branch = None
        text = (
            f"Factory lifecycle retired PR #{number} because its closing issue "
            f"#{issue_number} is closed. No newer running-task survivor was "
            "found, so this retirement is for the closed issue rather than a handoff."
        )
    _record(
        "factory_pr_retirement_prepared",
        repo=repo,
        pr_number=number,
        issue_number=issue_number,
        survivor_pr_number=survivor_number,
    )
    _ensure_comment(repo, number, _comment_marker(number, "retired"), text)
    # Re-read both GitHub and ownership after the comment. A successor can be
    # admitted while the comments endpoint is in flight, and closing its new
    # delivery target from the older observation would be the unsafe retry this
    # path exists to prevent.
    fresh = github_get(repo, f"pulls/{number}")
    if not _same_repo_factory_pull(fresh, repo) or _owner_of_pull(repo, fresh):
        return
    fresh_reason = _retirement_reason(repo, fresh)
    if fresh_reason is None:
        return
    fresh_issue, fresh_survivor, _fresh_row = fresh_reason
    if (fresh_survivor or {}).get("number") != survivor_number:
        return
    _record_retirement_intent(
        repo,
        number,
        branch=fresh["head"]["ref"],
        issue_number=fresh_issue,
        survivor_pr_number=survivor_number,
        survivor_branch=survivor_branch,
    )
    github_write(repo, f"pulls/{number}", {"state": "closed"}, method="PATCH")
    _record(
        "factory_pr_retirement_completed",
        repo=repo,
        pr_number=number,
        branch=fresh["head"]["ref"],
        issue_number=fresh_issue,
        survivor_pr_number=survivor_number,
        survivor_branch=survivor_branch,
    )


def sweep_stale_prs(repo: str) -> None:
    """Inspect at most 20 open PRs, advancing a durable page cursor each tick."""
    page = _latest_sweep_page(repo)
    try:
        pulls = github_list(
            repo,
            "pulls?state=open&sort=created&direction=asc"
            f"&per_page={PR_SWEEP_BATCH}&page={page}",
        )
    except (httpx.HTTPError, ValueError, TypeError):
        logger.warning("factory PR sweep listing failed", exc_info=True)
        return
    for pull in pulls[:PR_SWEEP_BATCH]:
        if not _same_repo_factory_pull(pull, repo):
            continue
        try:
            _retire_pull(repo, pull)
        except Exception:
            logger.warning(
                "factory PR retirement failed for %s", pull.get("number"), exc_info=True
            )
    _record_sweep_page(repo, page, min(len(pulls), PR_SWEEP_BATCH))


def _artifact_pr(row: SwarmNodeRun) -> int | None:
    try:
        outcome = json.loads(row.outcome_json or "{}")
    except (TypeError, ValueError):
        return None
    artifact = outcome.get("value") or outcome.get("artifact") or {}
    number = artifact.get("pr_number") if isinstance(artifact, dict) else None
    return number if type(number) is int and number > 0 else None


def _settlement_pr(db, row: FactoryReceipt) -> int | None:
    direction = _direction(row)
    try:
        _branch, number = granted_delivery_surface(direction)
    except ValueError:
        number = None
    if number is not None:
        return number
    runs = db.exec(
        select(SwarmNodeRun)
        .where(SwarmNodeRun.task_id == row.task_id)
        .order_by(SwarmNodeRun.id.desc())
    ).all()
    for run in runs:
        number = _artifact_pr(run)
        if number is not None:
            return number
    finishes = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id == row.task_id,
            FactoryAudit.action == "finish_task",
        )
        .order_by(FactoryAudit.id.desc())
    ).all()
    for finish in finishes:
        evidence = json.loads(finish.detail_json).get("evidence") or {}
        url = evidence.get("pr_url") if isinstance(evidence, dict) else None
        match = _PR_URL.search(url) if isinstance(url, str) else None
        if match:
            return int(match.group(1))
    return None


def _settlement_reason(db, row: FactoryReceipt) -> str:
    finish = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id == row.task_id,
            FactoryAudit.action == "finish_task",
        )
        .order_by(FactoryAudit.id.desc())
    ).first()
    if finish is not None:
        detail = json.loads(finish.detail_json)
        evidence = detail.get("evidence") or {}
        if isinstance(evidence, dict) and evidence.get("reason"):
            return str(evidence["reason"])[:1000]
    return f"task settled {row.state}"


def _settlement_done(task_id: str) -> bool:
    with _read_session() as db:
        return (
            db.exec(
                select(FactoryAudit.id).where(
                    FactoryAudit.task_id == task_id,
                    FactoryAudit.action == "factory_pr_settlement_complete",
                )
            ).first()
            is not None
        )


def _settlement_candidate(row: FactoryReceipt) -> tuple[int | None, str]:
    with _read_session() as db:
        current = db.get(FactoryReceipt, row.id)
        return _settlement_pr(db, current), _settlement_reason(db, current)


def _settlement_successor(repo: str, row: FactoryReceipt, pull: dict) -> str | None:
    branch = (pull.get("head") or {}).get("ref")
    if not isinstance(branch, str):
        return None
    with _read_session() as db:
        return delivery_branch_owner(db, repo, branch, exclude_receipt_id=row.id)


def _draft_settled_pull(repo: str, row: FactoryReceipt) -> None:
    if not row.task_id or _settlement_done(row.task_id):
        return
    number, reason = _settlement_candidate(row)
    if number is None:
        _record(
            "factory_pr_settlement_complete",
            task_id=row.task_id,
            repo=repo,
            receipt_id=row.id,
            outcome="no_pull_request",
        )
        return
    pull = github_get(repo, f"pulls/{number}")
    if not _same_repo_factory_pull(pull, repo):
        _record(
            "factory_pr_settlement_complete",
            task_id=row.task_id,
            repo=repo,
            receipt_id=row.id,
            pr_number=number,
            outcome="pull_not_actionable",
        )
        return
    successor = _settlement_successor(repo, row, pull)
    if successor is not None:
        _record(
            "factory_pr_settlement_complete",
            task_id=row.task_id,
            repo=repo,
            receipt_id=row.id,
            pr_number=number,
            outcome="adopted_by_successor",
            successor_task_id=successor,
        )
        return
    branch = pull["head"]["ref"]
    marker = _comment_marker(number, f"settled-{row.id}")
    text = (
        f"Factory receipt `{row.id}` for task `{row.task_id}` settled "
        f"`{row.state}`: {reason}. This pull request is left as a draft. "
        f"Re-admitting issue #{row.issue_number} adopts branch `{branch}` and "
        "continues this delivery instead of opening a replacement."
    )
    _ensure_comment(repo, number, marker, text)
    # Ownership is checked again after the comment. If a re-admission adopted
    # the branch meanwhile, its task owns readiness and this settlement stops.
    fresh = github_get(repo, f"pulls/{number}")
    successor = _settlement_successor(repo, row, fresh)
    if successor is not None:
        _record(
            "factory_pr_settlement_complete",
            task_id=row.task_id,
            repo=repo,
            receipt_id=row.id,
            pr_number=number,
            outcome="adopted_by_successor",
            successor_task_id=successor,
        )
        return
    if not _same_repo_factory_pull(fresh, repo):
        return
    if not fresh.get("draft"):
        node_id = fresh.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("settled pull request has no node id")
        github_graphql(_CONVERT_TO_DRAFT, {"pullRequestId": node_id})
    _record(
        "factory_pr_settlement_complete",
        task_id=row.task_id,
        repo=repo,
        receipt_id=row.id,
        pr_number=number,
        branch=branch,
        outcome="drafted",
        settlement=row.state,
    )


def draft_settled_prs(repo: str) -> None:
    with _read_session() as db:
        complete = select(FactoryAudit.task_id).where(
            FactoryAudit.action == "factory_pr_settlement_complete",
            FactoryAudit.task_id.is_not(None),
        )
        rows = list(
            db.exec(
                select(FactoryReceipt)
                .where(
                    FactoryReceipt.repo == repo,
                    FactoryReceipt.state.in_(_SETTLED_PR_STATES),
                    FactoryReceipt.task_id.is_not(None),
                    FactoryReceipt.task_id.not_in(complete),
                )
                .order_by(FactoryReceipt.updated_at, FactoryReceipt.id)
                .limit(SETTLEMENT_BATCH)
            ).all()
        )
    for row in rows:
        try:
            _draft_settled_pull(repo, row)
        except Exception:
            logger.warning(
                "factory settled PR handling failed for receipt %s",
                row.id,
                exc_info=True,
            )


def reconcile_tick(policy: dict) -> None:
    """Run settlement and stale-PR lifecycle work on the conductor tick."""
    global _last_sweep_at

    repo = policy.get("repo")
    if not isinstance(repo, str):
        return
    draft_settled_prs(repo)
    now = time.monotonic()
    if _last_sweep_at is None or now - _last_sweep_at >= PR_SWEEP_INTERVAL_SECONDS:
        _last_sweep_at = now
        sweep_stale_prs(repo)
