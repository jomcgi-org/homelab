"""Watch factory merges for reverts, so a merge can count as a lasting outcome.

A merged delivery is a positive outcome only if it stays merged. For seven days
after each factory ``merged`` audit this sweep reads the base branch's history
for a commit that reverts it, and writes one terminal row per task:
``reverted`` when it finds one, or ``revert_window_closed`` once the window
passes clean. Both rows are once-only, so the sweep is idempotent and a task
that has either row is never read again.

A revert is recognised by the forms GitHub and ``git revert`` write: a
``This reverts commit <sha>`` line naming the merge commit, a ``Reverts
owner/repo#N`` line, or a subject starting ``Revert`` that cites ``#N``. With
rebase merges a pull request's merge commit is only its last rebased commit,
so a partial revert of an earlier commit is not seen; the PR-number forms
catch reverts made through GitHub's button.

Reads are bounded: one sweep per ``SWEEP_SECONDS`` per process, and at most
``MAX_PAGES`` pages of history. A window is closed only when the pages read
reach back to the oldest merge being judged, so a truncated read can find a
revert but never clear one.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta

from sqlmodel import select

from factory.orchestration import factory_landing as landing
from factory.orchestration.factory_controls import _read_session
from factory.orchestration.factory_models import FactoryAudit

logger = logging.getLogger(__name__)

WINDOW_DAYS = 7
# Merges are followed a little past the window, so a sweep outage of up to a
# day still closes them. Older merges, including every merge from before this
# sweep existed, are never judged and read as unknown.
GRACE_DAYS = 2
SWEEP_SECONDS = 900
CANDIDATE_LIMIT = 200
PAGE_SIZE = 50
MAX_PAGES = 10
TERMINAL = ("reverted", "revert_window_closed")

_REVERTS_SHA = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
_REVERTS_PR = re.compile(r"Reverts [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#([0-9]+)")
_PR_REF = re.compile(r"#([0-9]+)")

_last_sweep: float | None = None


def _candidates(db, now) -> list[dict]:
    """Factory merges inside the window plus grace with no terminal row yet."""
    judged = select(FactoryAudit.task_id).where(
        FactoryAudit.action.in_(TERMINAL), FactoryAudit.task_id.is_not(None)
    )
    rows = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.action == "merged",
            FactoryAudit.task_id.is_not(None),
            FactoryAudit.task_id.not_in(judged),
            FactoryAudit.created_at >= now - timedelta(days=WINDOW_DAYS + GRACE_DAYS),
        )
        .order_by(FactoryAudit.id)
        .limit(CANDIDATE_LIMIT)
    ).all()
    result = []
    for row in rows:
        detail = json.loads(row.detail_json)
        if not isinstance(detail.get("pr_number"), int):
            continue
        result.append(
            {
                "task_id": row.task_id,
                "pr_number": detail["pr_number"],
                "merge_commit_sha": detail.get("merge_commit_sha"),
                "merged_at": landing._aware(row.created_at),
            }
        )
    return result


def revert_evidence(message: str) -> tuple[set[str], set[int]]:
    """The commit SHAs and pull request numbers one commit message reverts."""
    shas = set(_REVERTS_SHA.findall(message))
    prs = {int(n) for n in _REVERTS_PR.findall(message)}
    subject = message.split("\n", 1)[0]
    if subject.startswith("Revert"):
        prs.update(int(n) for n in _PR_REF.findall(subject))
    return shas, prs


def _matches(candidate: dict, shas: set[str], prs: set[int]) -> bool:
    if candidate["pr_number"] in prs:
        return True
    merge = candidate.get("merge_commit_sha")
    return isinstance(merge, str) and any(merge.startswith(sha) for sha in shas)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return landing._aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _history(repo: str, branch: str, since) -> tuple[list[dict], bool]:
    """Commits on ``branch`` since ``since``, newest first, and whether complete."""
    commits: list[dict] = []
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    for page in range(1, MAX_PAGES + 1):
        batch = landing.github_list(
            repo,
            f"commits?sha={branch}&since={stamp}&per_page={PAGE_SIZE}&page={page}",
        )
        commits.extend(entry for entry in batch if isinstance(entry, dict))
        if len(batch) < PAGE_SIZE:
            return commits, True
    return commits, False


def sweep(policy: dict) -> None:
    """Judge every pending factory merge against the base branch's history."""
    now = landing._now()
    with _read_session(None) as db:
        candidates = _candidates(db, now)
    if not candidates:
        return
    oldest = min(candidate["merged_at"] for candidate in candidates)
    commits, complete = _history(
        policy["repo"], policy.get("base_branch") or "main", oldest
    )
    open_candidates = list(candidates)
    for commit in commits:
        message = str((commit.get("commit") or {}).get("message") or "")
        shas, prs = revert_evidence(message)
        if not shas and not prs:
            continue
        committed = ((commit.get("commit") or {}).get("committer") or {}).get("date")
        when = _parse_time(committed)
        for candidate in list(open_candidates):
            if not _matches(candidate, shas, prs):
                continue
            # A revert after the window closed is not this outcome's revert.
            if when is not None and when - candidate["merged_at"] > timedelta(
                days=WINDOW_DAYS
            ):
                continue
            landing._record(
                candidate["task_id"],
                "reverted",
                pr_number=candidate["pr_number"],
                merge_commit_sha=candidate["merge_commit_sha"],
                revert_sha=commit.get("sha"),
                revert_committed_at=committed,
                detected_at=now.isoformat(),
            )
            open_candidates.remove(candidate)
    if not complete:
        return
    for candidate in open_candidates:
        if now - candidate["merged_at"] >= timedelta(days=WINDOW_DAYS):
            landing._record(
                candidate["task_id"],
                "revert_window_closed",
                pr_number=candidate["pr_number"],
                merge_commit_sha=candidate["merge_commit_sha"],
                window_days=WINDOW_DAYS,
                checked_at=now.isoformat(),
            )


def revert_tick(policy: dict) -> None:
    """Run the sweep at most once per ``SWEEP_SECONDS``; never raise."""
    global _last_sweep
    clock = time.monotonic()
    if _last_sweep is not None and clock - _last_sweep < SWEEP_SECONDS:
        return
    _last_sweep = clock
    try:
        sweep(policy)
    except Exception:  # noqa: BLE001 - outcome tracking never stops the lane
        logger.warning("factory revert sweep failed", exc_info=True)
