"""Bounded GitHub producers for the autonomous factory queue.

The producers only observe GitHub and persist factory receipts. Admission,
budgets, concurrency and dispatch remain owned by the existing factory loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from swarm import config
from swarm.factory_controls import (
    FEEDER_ACTOR,
    _audit,
    _locked_session,
    _now,
    intake_policy,
)
from swarm.factory_intake import receive_issue
from swarm.factory_models import FactoryAudit, FactoryReceipt

logger = logging.getLogger(__name__)

ACTOR = FEEDER_ACTOR
RECENT_COMMITS = 5
OPEN_PULLS = 10
MAX_CANDIDATES_PER_TICK = 10
SWEEP_SECONDS = 3600
PLAN_STALE_DAYS = 30
FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required"}
REGISTER_PREFIXES = ("docs/code-health/", "loom/docs/code-health/")
PLAN_FILES = ("SPEC.md",)


@dataclass(frozen=True)
class Candidate:
    feeder: str
    source_key: str
    issue_number: int
    title: str
    body: str
    task_class: str


def _bounded(value: object, limit: int = 12000) -> str:
    return str(value or "")[:limit]


def _files(commit: dict) -> list[dict]:
    return [item for item in commit.get("files") or [] if isinstance(item, dict)]


def register_diff_candidates(commit: dict, issue_number: int) -> list[Candidate]:
    """Create one mechanical task for each changed loom register."""
    sha = str(commit.get("sha") or "")
    result = []
    for item in _files(commit):
        path = str(item.get("filename") or "")
        if not path.startswith(REGISTER_PREFIXES):
            continue
        patch = _bounded(item.get("patch"), 8000)
        result.append(
            Candidate(
                feeder="register-diff",
                source_key=f"register-diff:{sha}:{path}",
                issue_number=issue_number,
                title=f"Resolve code-health register change in {path}",
                body=(
                    f"A merged change at {sha} changed the code-health register "
                    f"{path}. Inspect the register diff below, implement one bounded "
                    "machine-verifiable cleanup, and remove or reduce the cited entry.\n\n"
                    f"Register patch:\n{patch}"
                ),
                task_class="mechanical-refactor",
            )
        )
    return result


def ci_failure_candidates(pull: dict, checks: dict) -> list[Candidate]:
    """Create advisory first-responder tasks for failed exact-head checks."""
    number = pull.get("number")
    sha = str((pull.get("head") or {}).get("sha") or "")
    if type(number) is not int or not sha:
        return []
    result = []
    for check in checks.get("check_runs") or []:
        if (
            not isinstance(check, dict)
            or check.get("conclusion") not in FAILED_CONCLUSIONS
        ):
            continue
        name = _bounded(check.get("name"), 256)
        identity = check.get("external_id") or check.get("id") or name
        result.append(
            Candidate(
                feeder="ci-failure",
                source_key=f"ci-failure:{sha}:{identity}",
                issue_number=number,
                title=f"Diagnose failed CI check {name} on PR #{number}",
                body=(
                    f"Pull request #{number} at exact head {sha} has failed check "
                    f"{name}. Read the check evidence and post one concise first-pass "
                    "diagnosis as a comment on that pull request. Quote the failing "
                    "assertion or target and distinguish task-caused failure from "
                    "infrastructure or flake. Do not create a branch or pull request.\n\n"
                    f"Details: {_bounded(check.get('details_url'), 1000)}"
                ),
                task_class="advisory-diagnosis",
            )
        )
    return result


def renovate_candidate(pull: dict) -> Candidate | None:
    """Create changelog-grounded advisory triage for an open Renovate PR."""
    number = pull.get("number")
    user = str((pull.get("user") or {}).get("login") or "").lower()
    title = str(pull.get("title") or "")
    sha = str((pull.get("head") or {}).get("sha") or "")
    if type(number) is not int or not sha:
        return None
    if user not in {"renovate[bot]", "renovate-bot"} and not title.lower().startswith(
        ("chore(deps):", "fix(deps):")
    ):
        return None
    return Candidate(
        feeder="renovate",
        source_key=f"renovate:{number}:{sha}",
        issue_number=number,
        title=f"Triage Renovate PR #{number}: {_bounded(title, 300)}",
        body=(
            f"Review Renovate pull request #{number} at exact head {sha}. Read the "
            "upstream changelog and this repository's usage, then post one concise "
            "risk and compatibility comment on the pull request. Do not create a "
            "branch or pull request."
        ),
        task_class="advisory-triage",
    )


def stpa_candidate(
    commit: dict, issue_number: int, stpa_path: str, stpa_revision: str
) -> Candidate | None:
    """Create a judgment task when system code moved without its STPA model."""
    paths = {str(item.get("filename") or "") for item in _files(commit)}
    prefix = stpa_path[: -len("STPA.md")]
    changed = sorted(path for path in paths if path.startswith(prefix))
    if not changed or stpa_path in paths:
        return None
    sha = str(commit.get("sha") or "")
    return Candidate(
        feeder="stpa-staleness",
        source_key=f"stpa-staleness:{stpa_path}:{stpa_revision}",
        issue_number=issue_number,
        title=f"Refresh stale STPA model {stpa_path}",
        body=(
            f"Merged change {sha} modified the system owning {stpa_path} without "
            "updating its safety model. Use the repository STPA workflow to assess "
            "the changed control structure and update the model where evidence "
            f"requires it. Changed paths: {', '.join(changed[:20])}."
        ),
        task_class="judgment-analysis",
    )


def plan_candidate(
    commit: dict,
    issue_number: int,
    plan_path: str,
    plan_revision: str,
    plan_updated_at: datetime,
    *,
    now: datetime | None = None,
) -> Candidate | None:
    """Create one judgment task per stale plan revision, not per later commit."""
    now = now or datetime.now(timezone.utc)
    if plan_updated_at > now - timedelta(days=PLAN_STALE_DAYS):
        return None
    paths = {str(item.get("filename") or "") for item in _files(commit)}
    if plan_path in paths or not any(not path.endswith(".md") for path in paths):
        return None
    sha = str(commit.get("sha") or "")
    return Candidate(
        feeder="plan-staleness",
        source_key=f"plan-staleness:{plan_path}:{plan_revision}",
        issue_number=issue_number,
        title=f"Reconcile stale repository plan {plan_path}",
        body=(
            f"The repository plan {plan_path} has not changed for at least "
            f"{PLAN_STALE_DAYS} days while merged implementation {sha} moved the "
            "repository. Compare the plan with current code and accepted architecture, "
            "then make only evidence-backed updates."
        ),
        task_class="judgment-analysis",
    )


def enqueue(candidate: Candidate, policy: dict, *, session=None) -> dict:
    """Persist one event receipt behind the durable factory switch."""
    repo = policy["repo"]
    return receive_issue(
        repo,
        candidate.issue_number,
        candidate.title,
        candidate.body,
        f"https://github.com/{repo}/issues/{candidate.issue_number}",
        ACTOR,
        generation=policy.get("generation", 0),
        task_class=candidate.task_class,
        source_key=candidate.source_key,
        requires_issue_close=False,
        require_enabled=True,
        session=session,
    )


def _stamp_due() -> bool:
    """Reserve one hourly producer sweep while the factory is enabled."""
    cutoff = _now() - timedelta(seconds=SWEEP_SECONDS)
    with _locked_session() as (db, control):
        if control.state != "enabled":
            return False
        latest = db.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action == "feeders_swept")
            .order_by(FactoryAudit.id.desc())
        ).first()
        if latest is not None:
            created = latest.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= cutoff:
                return False
        _audit(db, ACTOR, "feeders_swept")
        return True


def _remaining_today(policy: dict) -> int:
    """Reuse the intake daily bound for feeder receipts of either lane."""
    cutoff = _now() - timedelta(hours=24)
    with _locked_session() as (db, control):
        if control.state != "enabled":
            return 0
        used = len(
            db.exec(
                select(FactoryReceipt.id).where(
                    FactoryReceipt.actor == ACTOR,
                    FactoryReceipt.created_at >= cutoff,
                )
            ).all()
        )
    return max(0, intake_policy(policy)["max_per_day"] - used)


def _github():
    # Lazy to avoid a package cycle: the conductor owns the bounded HTTP seam
    # and invokes this module from its tick.
    from swarm.factory_conductor import github_get, github_list

    return github_get, github_list


def _associated_issue(repo: str, sha: str, github_list) -> int | None:
    pulls = github_list(repo, f"commits/{sha}/pulls?per_page=1")
    if not pulls:
        return None
    number = pulls[0].get("number") if isinstance(pulls[0], dict) else None
    return number if type(number) is int and number > 0 else None


def _timestamp(commit: dict) -> datetime | None:
    raw = ((commit.get("commit") or {}).get("committer") or {}).get("date")
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def discover(policy: dict) -> list[Candidate]:
    """Poll bounded existing GitHub inputs and classify all five feeder types."""
    github_get, github_list = _github()
    repo, branch = policy["repo"], policy["base_branch"]
    pulls = github_list(repo, f"pulls?state=open&per_page={OPEN_PULLS}")
    candidates: list[Candidate] = []
    for pull in pulls[:OPEN_PULLS]:
        if not isinstance(pull, dict):
            continue
        renovate = renovate_candidate(pull)
        if renovate is not None:
            candidates.append(renovate)
        sha = str((pull.get("head") or {}).get("sha") or "")
        if sha:
            checks = github_get(repo, f"commits/{sha}/check-runs?per_page=20")
            candidates.extend(ci_failure_candidates(pull, checks))

    commits = github_list(repo, f"commits?sha={branch}&per_page={RECENT_COMMITS}")
    detailed: list[tuple[dict, int]] = []
    for summary in commits[:RECENT_COMMITS]:
        sha = str(summary.get("sha") or "") if isinstance(summary, dict) else ""
        if not sha:
            continue
        number = _associated_issue(repo, sha, github_list)
        if number is None:
            continue
        detailed.append((github_get(repo, f"commits/{sha}"), number))

    stpa_documents: dict[str, str | None] = {}
    for commit, number in detailed:
        candidates.extend(register_diff_candidates(commit, number))
        projects = {
            path.split("/", 2)[1]
            for path in (str(item.get("filename") or "") for item in _files(commit))
            if path.startswith("projects/") and len(path.split("/", 2)) == 3
        }
        for project in sorted(projects)[:RECENT_COMMITS]:
            path = f"projects/{project}/STPA.md"
            if path not in stpa_documents:
                try:
                    document = github_get(repo, f"contents/{path}?ref={branch}")
                    stpa_documents[path] = str(document.get("sha") or "")
                except Exception as exc:  # A missing STPA is not a stale STPA.
                    if (
                        getattr(getattr(exc, "response", None), "status_code", None)
                        == 404
                    ):
                        stpa_documents[path] = None
                    else:
                        raise
            revision = stpa_documents[path]
            if not revision:
                continue
            candidate = stpa_candidate(commit, number, path, revision)
            if candidate is not None:
                candidates.append(candidate)

    if detailed:
        trigger, number = detailed[0]
        for path in PLAN_FILES:
            history = github_list(repo, f"commits?sha={branch}&path={path}&per_page=1")
            if not history or not isinstance(history[0], dict):
                continue
            updated = _timestamp(history[0])
            revision = str(history[0].get("sha") or "")
            if updated is not None and revision:
                candidate = plan_candidate(trigger, number, path, revision, updated)
                if candidate is not None:
                    candidates.append(candidate)

    unique = {candidate.source_key: candidate for candidate in candidates}
    return list(unique.values())[:MAX_CANDIDATES_PER_TICK]


def feeder_tick(policy: dict) -> list[dict]:
    """Run one real producer sweep, inert by default and failure-isolated."""
    if not config.feeders_enabled() or not _stamp_due():
        return []
    remaining = _remaining_today(policy)
    if remaining == 0:
        return []
    try:
        candidates = discover(policy)
    except Exception:
        logger.exception("factory feeder discovery failed")
        return []
    produced = []
    for candidate in candidates[:remaining]:
        try:
            produced.append(enqueue(candidate, policy))
        except Exception:
            logger.exception("factory %s feeder failed", candidate.feeder)
    return produced


__all__ = [
    "Candidate",
    "ci_failure_candidates",
    "discover",
    "enqueue",
    "feeder_tick",
    "plan_candidate",
    "register_diff_candidates",
    "renovate_candidate",
    "stpa_candidate",
]
