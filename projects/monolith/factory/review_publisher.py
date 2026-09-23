"""Publish the trusted factory review check from durable control-plane evidence."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from core.github import GITHUB_API
from sqlmodel import select

from factory.orchestration.factory_controls import (
    _audit,
    _locked_session,
    _read_session,
)
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt
from factory.orchestration.models import SwarmNodeRun, SwarmTask

CHECK_NAME = "factory/review"
ACTOR = "factory:review-publisher"
PUBLISH_ENABLED_ENV = "FACTORY_REVIEW_PUBLISH_ENABLED"
PUBLISHER_TOKEN_ENV = "FACTORY_REVIEW_PUBLISHER_TOKEN"
SESSION_EVIDENCE_URL = "https://private.jomcgi.dev/agents/session/{session_id}"
WRITE_TIMEOUT_SECONDS = 15
RESPONSE_LIMIT_BYTES = 1_000_000
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class ReviewEvidence:
    task_id: str
    repo: str
    pr_number: int
    branch: str
    head_sha: str
    review_run_id: int
    review_session_id: int
    policy_version: int
    details_url: str
    published_check_id: int | None = None


@dataclass(frozen=True)
class Invalidation:
    repo: str
    pr_number: int
    head_sha: str
    review_run_id: int
    review_session_id: int
    details_url: str
    published_check_id: int | None = None


@dataclass(frozen=True)
class Refusal:
    task_id: str
    reason: str
    invalidation: Invalidation | None = None


def enabled() -> bool:
    """Only an explicit true enables publication."""
    return os.environ.get(PUBLISH_ENABLED_ENV, "").lower() == "true"


def _default_token_provider() -> str | None:
    """Return only the App installation token dedicated to check publication."""
    return os.environ.get(PUBLISHER_TOKEN_ENV) or None


def _artifact(run: SwarmNodeRun) -> tuple[str, dict]:
    try:
        outcome = json.loads(run.outcome_json or "{}")
    except (TypeError, ValueError):
        return "invalid", {}
    stored = outcome.get("artifact")
    if not isinstance(stored, dict):
        return "invalid", {}
    errors = stored.get("errors")
    value = outcome.get("value", stored.get("value"))
    if (
        stored.get("status") != "ok"
        or not isinstance(value, dict)
        or not isinstance(errors, list)
        or errors
    ):
        return "invalid", {}
    return "ok", value


def _implementation(node_key: str) -> bool:
    return (
        node_key.startswith(("implement_", "integrate_"))
        or re.fullmatch(r"correct_[0-9]+", node_key) is not None
    )


def _published_check(
    db, task_id: str, head_sha: str, conclusion: str, review_run_id: int | None = None
) -> int | None:
    rows = db.exec(
        select(FactoryAudit)
        .where(
            FactoryAudit.task_id == task_id,
            FactoryAudit.action == "review_published",
        )
        .order_by(FactoryAudit.id.desc())
    ).all()
    for row in rows:
        try:
            detail = json.loads(row.detail_json)
        except (TypeError, ValueError):
            continue
        if detail.get("head_sha") != head_sha or detail.get("conclusion") != conclusion:
            continue
        if review_run_id is not None and detail.get("review_run_id") != review_run_id:
            continue
        check_id = detail.get("check_id")
        if type(check_id) is int and check_id > 0:
            return check_id
    return None


def _refusal(task_id: str, reason: str) -> Refusal:
    return Refusal(task_id=task_id, reason=reason)


def collect(task_id: str) -> ReviewEvidence | Refusal:
    """Collect an exact-head approval using only durable trusted assignments."""
    from factory.orchestration import factory_landing as landing
    from factory.orchestration.factory_controls import granted_delivery_surface

    with _read_session() as db:
        task = db.get(SwarmTask, task_id)
        receipt = db.exec(
            select(FactoryReceipt).where(FactoryReceipt.task_id == task_id)
        ).first()
        if task is None or receipt is None:
            return _refusal(task_id, "task_missing")
        if receipt.state not in ("succeeded", "landing"):
            return _refusal(task_id, "delivery_not_settled")
        if not receipt.policy_json:
            return _refusal(task_id, "policy_missing")
        try:
            policy = json.loads(receipt.policy_json)
        except (TypeError, ValueError):
            return _refusal(task_id, "policy_invalid")
        if not isinstance(policy, dict) or policy.get("repo") != receipt.repo:
            return _refusal(task_id, "policy_invalid")
        if task.repo != receipt.repo or task.base_branch != policy.get("base_branch"):
            return _refusal(task_id, "policy_identity_mismatch")

        admission = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action == "admit_next",
            )
            .order_by(FactoryAudit.id.desc())
        ).first()
        try:
            admission_detail = json.loads(admission.detail_json) if admission else {}
        except (TypeError, ValueError):
            admission_detail = {}
        policy_version = admission_detail.get("policy_version")
        if type(policy_version) is not int or policy_version < 0:
            return _refusal(task_id, "policy_version_missing")

        delivery = landing._delivery_prs(db, [task_id]).get(task_id)
        if delivery is None:
            return _refusal(task_id, "delivery_missing")
        pr_number, approved_head = delivery
        if not isinstance(approved_head, str) or _SHA.fullmatch(approved_head) is None:
            return _refusal(task_id, "approved_head_missing")

        runs = db.exec(
            select(SwarmNodeRun)
            .where(SwarmNodeRun.task_id == task_id)
            .order_by(SwarmNodeRun.id)
        ).all()
        reviews = [run for run in runs if run.node_key.startswith("review_")]
        if not reviews:
            return _refusal(task_id, "review_missing")
        review = reviews[-1]
        if review.status != "succeeded":
            return _refusal(task_id, "review_not_succeeded")
        validation, artifact = _artifact(review)
        if validation != "ok":
            return _refusal(task_id, "review_artifact_invalid")
        if review.id is None or review.session_id is None:
            return _refusal(task_id, "review_identity_missing")

        implementers = [run for run in runs if _implementation(run.node_key)]
        if not implementers:
            return _refusal(task_id, "implementation_missing")
        if any(run.session_id is None for run in implementers):
            return _refusal(task_id, "implementation_identity_missing")
        if any(run.session_id == review.session_id for run in implementers):
            return _refusal(task_id, "review_not_independent")

        if (
            artifact.get("pr_number") != pr_number
            or artifact.get("head_sha") != approved_head
            or review.head_sha != approved_head
        ):
            return _refusal(task_id, "review_identity_mismatch")

        try:
            direction = (
                json.loads(receipt.direction_json) if receipt.direction_json else None
            )
            granted_branch, granted_pr = granted_delivery_surface(direction)
        except (TypeError, ValueError):
            return _refusal(task_id, "delivery_surface_invalid")
        branch = granted_branch or f"factory/{task_id}"
        if granted_pr is not None and granted_pr != pr_number:
            return _refusal(task_id, "pr_identity_changed")

        prior_success_check_id = _published_check(db, task_id, approved_head, "success")
        approval_check_id = _published_check(
            db, task_id, approved_head, "success", review.id
        )
        failure_check_id = _published_check(
            db, task_id, approved_head, "failure", review.id
        )

    try:
        pull = landing.github_get(receipt.repo, f"pulls/{pr_number}")
    except (httpx.HTTPError, ValueError):
        return _refusal(task_id, "github_unavailable")
    if not isinstance(pull, dict):
        return _refusal(task_id, "github_response_invalid")
    head = pull.get("head") or {}
    base = pull.get("base") or {}
    if pull.get("number") != pr_number:
        return _refusal(task_id, "pr_identity_changed")
    if pull.get("state") != "open" or pull.get("draft"):
        return _refusal(task_id, "pr_not_open")
    if (
        head.get("ref") != branch
        or (head.get("repo") or {}).get("full_name") != receipt.repo
        or base.get("ref") != task.base_branch
    ):
        return _refusal(task_id, "pr_identity_changed")
    if head.get("sha") != approved_head:
        return _refusal(task_id, "head_moved")

    details_url = SESSION_EVIDENCE_URL.format(session_id=review.session_id)
    if artifact.get("verdict") == "approve":
        return ReviewEvidence(
            task_id=task_id,
            repo=receipt.repo,
            pr_number=pr_number,
            branch=branch,
            head_sha=approved_head,
            review_run_id=review.id,
            review_session_id=review.session_id,
            policy_version=policy_version,
            details_url=details_url,
            published_check_id=approval_check_id,
        )
    if (
        artifact.get("verdict") == "changes_requested"
        and prior_success_check_id is not None
    ):
        return Refusal(
            task_id=task_id,
            reason="review_superseded",
            invalidation=Invalidation(
                repo=receipt.repo,
                pr_number=pr_number,
                head_sha=approved_head,
                review_run_id=review.id,
                review_session_id=review.session_id,
                details_url=details_url,
                published_check_id=failure_check_id,
            ),
        )
    return _refusal(task_id, "review_not_approved")


def _github_post(repo: str, suffix: str, payload: dict, token: str) -> dict:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None:
        raise ValueError("invalid repository")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "monolith-factory-review-publisher",
    }
    with httpx.Client(timeout=WRITE_TIMEOUT_SECONDS) as client:
        response = client.post(
            f"{GITHUB_API}/repos/{repo}/{suffix}", headers=headers, json=payload
        )
        response.raise_for_status()
    if len(response.content) > RESPONSE_LIMIT_BYTES:
        raise ValueError("GitHub response exceeds factory limit")
    body = response.json()
    if not isinstance(body, dict):
        raise TypeError("GitHub returned a non-object")
    return body


def _audit_skipped(task_id: str, reason: str) -> None:
    with _locked_session() as (db, _control):
        _audit(db, ACTOR, "review_publish_skipped", task_id=task_id, reason=reason)


def publish(
    evidence: ReviewEvidence | Refusal,
    token_provider: Callable[[], str | None] = _default_token_provider,
) -> dict:
    """Publish success, or invalidate an earlier success with a failure check."""
    target: ReviewEvidence | Invalidation
    conclusion: str
    summary: str
    if isinstance(evidence, ReviewEvidence):
        target = evidence
        conclusion = "success"
        summary = "Independent factory review approved this exact pull request head."
    elif evidence.invalidation is not None:
        target = evidence.invalidation
        conclusion = "failure"
        summary = evidence.reason
    else:
        return {"action": "refused", "reason": evidence.reason}

    if target.published_check_id is not None:
        return {
            "action": "already_published",
            "conclusion": conclusion,
            "check_id": target.published_check_id,
            "head_sha": target.head_sha,
            "review_run_id": target.review_run_id,
        }
    token = token_provider()
    if not isinstance(token, str) or not token:
        _audit_skipped(evidence.task_id, "publisher_token_missing")
        return {"action": "skipped", "reason": "publisher_token_missing"}

    body = _github_post(
        target.repo,
        "check-runs",
        {
            "name": CHECK_NAME,
            "head_sha": target.head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "details_url": target.details_url,
            "external_id": str(target.review_run_id),
            "output": {"title": CHECK_NAME, "summary": summary},
        },
        token,
    )
    check_id = body.get("id")
    if type(check_id) is not int or check_id <= 0:
        raise ValueError("GitHub check run response has no id")
    return {
        "action": "published",
        "conclusion": conclusion,
        "check_id": check_id,
        "head_sha": target.head_sha,
        "review_run_id": target.review_run_id,
    }
