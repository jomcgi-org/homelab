"""A typed, bounded terminal disposition for work fulfilled outside the factory.

A factory task whose requested change was completed through another merged
pull request should reach an attributable terminal state without a duplicate
PR, repeated implementation, or a coordinator-only database operation. This
module is the deterministic side of that disposition: the conductor proposes
an external pull request with its exact head, and this validation decides
whether the task may settle on that evidence.

Settling here never counts as a successful end-to-end factory delivery. The
existing task-branch delivery check in ``verify_delivery`` is untouched, and
the evidence this module returns settles the receipt as ``cancelled`` with a
provenance state of ``externally_fulfilled``, so factory success metrics keep
counting only task-branch deliveries that passed exact-head review. The
``finish`` decision path is unchanged; the conductor reaches this module
through the separate ``settle_external`` decision action.
"""

from __future__ import annotations

import re

EXTERNAL_STATE = "externally_fulfilled"
"""Provenance state recorded on the settlement evidence, never a factory win."""

_SHA = re.compile(r"[0-9a-f]{40}")
_CHECK = "pr-checks"


class ExternalDispositionRefused(ValueError):
    """A named refusal the planner reads as evidence, mirroring DeliveryRefused."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _readers():
    """The bounded GitHub readers, imported lazily and in one place."""
    from factory.orchestration import factory_conductor as conductor

    return conductor.github_get, conductor.github_list, conductor.closes_issue


def verify_external_disposition(
    task: dict,
    pr_number: int,
    head_sha: str,
) -> dict:
    """Confirm a merged external PR covers this task's issue at the exact head.

    The checks are ordered so a disposition that is unready for a bigger
    reason reports that reason: identity and shape first, then scope, then
    exact-head validation and review evidence.
    """
    github_get, github_list, closes_issue = _readers()
    repo = task.get("repo")
    base_branch = task.get("base_branch")
    issue_number = task.get("issue_number")
    if not isinstance(repo, str) or not repo:
        raise ExternalDispositionRefused(
            "external_evidence_invalid", "this task names no repository"
        )
    if not isinstance(base_branch, str) or not base_branch:
        raise ExternalDispositionRefused(
            "external_evidence_invalid", "this task names no base branch"
        )
    if type(issue_number) is not int or issue_number <= 0:
        raise ExternalDispositionRefused(
            "external_evidence_invalid", "this task names no issue to settle"
        )
    if type(pr_number) is not int or pr_number <= 0:
        raise ExternalDispositionRefused(
            "external_evidence_invalid", "an external disposition names a PR number"
        )
    if not isinstance(head_sha, str) or _SHA.fullmatch(head_sha) is None:
        raise ExternalDispositionRefused(
            "external_evidence_invalid", "an external disposition names an exact head"
        )
    pr = github_get(repo, f"pulls/{pr_number}")
    if not isinstance(pr, dict):
        raise ExternalDispositionRefused(
            "external_evidence_invalid", "the external PR could not be read"
        )
    merged = pr.get("merged") is True or (
        pr.get("state") == "closed" and pr.get("merged_at")
    )
    if not merged:
        raise ExternalDispositionRefused(
            "external_not_merged",
            f"PR #{pr_number} is not merged, so it settles nothing",
        )
    head = pr.get("head") or {}
    if head.get("sha") != head_sha:
        raise ExternalDispositionRefused(
            "external_head_stale",
            "the proposed head is not the external PR head",
        )
    from factory.orchestration import factory_conductor as conductor

    if head.get("ref") == conductor.delivery_branch(task):
        raise ExternalDispositionRefused(
            "external_is_task_branch",
            "a PR on the task branch settles through finish, not externally",
        )
    base = pr.get("base") or {}
    if base.get("ref") != base_branch:
        raise ExternalDispositionRefused(
            "external_base_mismatch",
            "the external PR does not target this task's base branch",
        )
    base_repo = (base.get("repo") or {}).get("full_name")
    if base_repo is not None and base_repo != repo:
        raise ExternalDispositionRefused(
            "external_base_mismatch",
            "the external PR lives in another repository",
        )
    if not closes_issue(pr.get("body"), repo, issue_number):
        raise ExternalDispositionRefused(
            "external_scope_not_covered",
            f"PR #{pr_number} does not close issue #{issue_number}, "
            "so it does not cover the requested scope",
        )
    checks = github_get(repo, f"commits/{head_sha}/status")
    contexts = {
        entry.get("context"): entry.get("state")
        for entry in (checks.get("statuses") or [])
        if isinstance(entry, dict)
    }
    from factory.orchestration.factory_conductor import ADVISORY_CHECK_CONTEXTS

    failed = {
        context: state
        for context, state in contexts.items()
        if state != "success" and context not in ADVISORY_CHECK_CONTEXTS
    }
    if contexts.get(_CHECK) != "success" or failed:
        raise ExternalDispositionRefused(
            "external_checks_pending",
            "required checks have not passed at the external head",
        )
    reviews = github_list(repo, f"pulls/{pr_number}/reviews?per_page=100")
    approved = any(
        isinstance(review, dict)
        and review.get("state") == "APPROVED"
        and review.get("commit_id") == head_sha
        for review in reviews
    )
    if not approved:
        raise ExternalDispositionRefused(
            "external_review_missing",
            "no approving review at the external head was found",
        )
    pr_url = pr.get("html_url") or f"https://github.com/{repo}/pull/{pr_number}"
    return {
        "pr_url": pr_url,
        "head_sha": head_sha,
        "state": EXTERNAL_STATE,
        "reason": (
            f"externally fulfilled by merged PR #{pr_number} at {head_sha}; "
            "factory delivery not claimed"
        ),
    }
