"""Default-off, trusted PR and merge-queue dependency evidence publication.

Only server-fetched GitHub data and durable factory receipts grant a success.
The queue comparison deliberately refuses combined groups containing a bot.
Activation and merge-time freshness remain operational acceptance in #6799.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from core.github import GITHUB_API
from sqlmodel import select

from factory.orchestration import dependency_prs as deps
from factory.orchestration.factory_controls import (
    _audit,
    _locked_session,
    _read_session,
)
from factory.orchestration.factory_models import FactoryAudit, FactoryReceipt
from factory.orchestration.models import SwarmNodeRun
from factory.review_publisher import (
    RESPONSE_LIMIT_BYTES,
    WRITE_TIMEOUT_SECONDS,
    _github_post,
)

CHECK_NAME = "factory/dependency-evidence"
ACTOR = "factory:dependency-gate"
ENABLED_ENV = "FACTORY_DEPENDENCY_GATE_ENABLED"
TOKEN_ENV = "FACTORY_DEPENDENCY_GATE_TOKEN"
QUEUE_QUERY = """
query DependencyQueue($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: $branch) {
      entries(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id position
          baseCommit { oid }
          headCommit { oid }
          pullRequest { number headRefOid }
        }
      }
    }
  }
}
"""


def enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "").lower() == "true"


class GitHub:
    """Every gate read and write uses the dedicated App installation token."""

    def __init__(self, token: str):
        self.token = token

    def request(self, path: str, payload=None):
        with (
            httpx.Client(timeout=WRITE_TIMEOUT_SECONDS) as client,
            client.stream(
                "POST" if payload is not None else "GET",
                f"{GITHUB_API}/{path}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                **({"json": payload} if payload is not None else {}),
            ) as response,
        ):
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > RESPONSE_LIMIT_BYTES:
                    raise ValueError("GitHub gate response is truncated")
        return json.loads(data)

    def get(self, repo: str, suffix: str) -> dict:
        value = self.request(f"repos/{repo}/{suffix}")
        if not isinstance(value, dict):
            raise TypeError("GitHub gate response is malformed")
        return value

    def rows(self, repo: str, suffix: str) -> list:
        value = self.request(f"repos/{repo}/{suffix}")
        if not isinstance(value, list):
            raise TypeError("GitHub gate response is malformed")
        return value

    def queue(self, repo: str, branch: str) -> list[dict]:
        owner, name = repo.split("/")
        body = self.request(
            "graphql",
            {
                "query": QUEUE_QUERY,
                "variables": {"owner": owner, "name": name, "branch": branch},
            },
        )
        if not isinstance(body, dict) or body.get("errors"):
            raise ValueError("queue evidence unavailable")
        connection = body["data"]["repository"]["mergeQueue"]["entries"]
        if connection["pageInfo"]["hasNextPage"] is not False:
            raise ValueError("queue evidence truncated")
        entries = connection["nodes"]
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError("queue evidence malformed")
        for position, entry in enumerate(entries, 1):
            if (
                entry["position"] != position
                or not isinstance(entry["id"], str)
                or not entry["id"]
                or type(entry["pullRequest"]["number"]) is not int
                or entry["pullRequest"]["number"] <= 0
                or any(
                    not isinstance(sha, str) or not deps.SHA.fullmatch(sha)
                    for sha in (
                        entry["baseCommit"]["oid"],
                        entry["headCommit"]["oid"],
                        entry["pullRequest"]["headRefOid"],
                    )
                )
            ):
                raise ValueError("queue evidence malformed")
        return entries


def audit(action: str, **detail) -> None:
    with _locked_session() as (db, _control):
        _audit(db, ACTOR, action, **detail)


@dataclass(frozen=True)
class Approval:
    receipt_id: int
    task_id: str
    evidence: dict
    runs: list[dict]


def approval(repo: str, number: int) -> Approval:
    """Only the latest authorized generation may approve this PR."""
    with _read_session() as db:
        receipt = db.exec(
            select(FactoryReceipt)
            .where(
                FactoryReceipt.repo == repo,
                FactoryReceipt.issue_number == number,
                FactoryReceipt.task_class == "judgment-analysis",
            )
            .order_by(FactoryReceipt.generation.desc(), FactoryReceipt.id.desc())
        ).first()
        if (
            receipt is None
            or receipt.state not in ("succeeded", "landing")
            or not receipt.task_id
            or receipt.task_paused
            or receipt.cancellation_requested
        ):
            raise ValueError("dependency approval missing or unsettled")
        invalidated = db.exec(
            select(FactoryAudit).where(
                FactoryAudit.task_id == receipt.task_id,
                FactoryAudit.action == "dependency_approval_invalidated",
            )
        ).first()
        if invalidated is not None:
            raise ValueError(
                "dependency approval invalidated; fresh authorized generation required"
            )
        evidence = json.loads(receipt.direction_json or "{}")["dependency_review"]
        runs = db.exec(
            select(SwarmNodeRun).where(SwarmNodeRun.task_id == receipt.task_id)
        ).all()
        return Approval(
            receipt.id,
            receipt.task_id,
            evidence,
            [
                {**run.model_dump(), "pin": json.loads(run.pin_json or "{}")}
                for run in runs
            ],
        )


def dependency(pull: dict, policy: dict) -> bool:
    """Refs force evidence. They never grant author or review authority."""
    user = pull["user"]
    if (
        type(user["id"]) is not int
        or user["id"] <= 0
        or user["type"] not in ("User", "Bot")
        or not isinstance(user["login"], str)
        or not user["login"]
    ):
        raise ValueError("PR author identity malformed")
    ids = (policy.get("intake") or {}).get("dependency_pr_author_ids", [])
    if not isinstance(ids, list) or any(
        type(value) is not int or value <= 0 for value in ids
    ):
        raise ValueError("dependency author IDs malformed")
    return (
        user["id"] in ids
        or user["type"] == "Bot"
        or pull["head"]["ref"].lower().startswith(("renovate/", "dependabot/"))
    )


def validate_pull(pull: dict, repo: str, branch: str, number: int, head: str) -> None:
    if (
        pull["number"] != number
        or pull["state"] != "open"
        or pull["draft"] is not False
        or pull["head"]["sha"] != head
        or not deps.SHA.fullmatch(head)
        or pull["base"]["ref"] != branch
        or pull["base"]["repo"]["full_name"].lower() != repo.lower()
        or pull["head"]["repo"]["full_name"].lower() != repo.lower()
    ):
        raise ValueError("PR identity or head moved")


def comparable(value: dict) -> str:
    """Allow only commit-ID changes, with byte-identical reviewed content.

    Blob SHAs bind file contents, including unrepresented dependencies. Old
    receipts without file SHAs require fresh review under a new generation.
    """
    files = value["files"]
    if not files or any(
        not isinstance(row.get("sha"), str)
        or not deps.SHA.fullmatch(row["sha"])
        or row.get("status")
        not in (
            "added",
            "removed",
            "modified",
            "renamed",
            "copied",
            "changed",
            "unchanged",
        )
        or any(
            type(row.get(key)) is not int or row[key] < 0
            for key in ("additions", "deletions")
        )
        for row in files
    ):
        raise ValueError("reviewed file evidence malformed or stale")
    return json.dumps(
        {
            key: value[key]
            for key in (
                "pr_number",
                "author_id",
                "author_login",
                "files",
                "dependency_changes",
                "open_alerts",
            )
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def evaluate(
    repo: str, branch: str, pull: dict, policy: dict, github: GitHub, entry=None
) -> str:
    head = pull["head"]["sha"]
    validate_pull(pull, repo, branch, pull["number"], head)
    if not dependency(pull, policy):
        return "non-dependency PR"
    approved = approval(repo, pull["number"])
    try:
        evidence = approved.evidence
        encoded = json.dumps(
            {key: value for key, value in evidence.items() if key != "evidence_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        )
        if hashlib.sha256(encoded.encode()).hexdigest() != evidence["evidence_sha256"]:
            raise ValueError("approved evidence digest malformed")
        if evidence["head_sha"] != head or evidence["author_id"] != pull["user"]["id"]:
            raise ValueError("approved PR head or author moved")
        deps.verify_assessments(evidence, approved.runs)
        comparison = None
        if entry is not None:
            if entry["pullRequest"]["headRefOid"] != head:
                raise ValueError("queued PR head moved")
            comparison = (entry["baseCommit"]["oid"], entry["headCommit"]["oid"])
        current = deps.snapshot(
            repo,
            pull,
            read_get=github.get,
            read_list=github.rows,
            comparison=comparison,
        )
        if comparable(current) != comparable(evidence):
            raise ValueError(
                "dependency evidence changed; fresh authorized generation required"
            )
        return "independent safe assessments match fresh dependency evidence"
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        audit(
            "dependency_approval_invalidated",
            task_id=approved.task_id,
            receipt_id=approved.receipt_id,
            repo=repo,
            pr_number=pull["number"],
        )
        raise


def publish(repo: str, head: str, conclusion: str, reason: str, token: str) -> None:
    body = _github_post(
        repo,
        "check-runs",
        {
            "name": CHECK_NAME,
            "head_sha": head,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": CHECK_NAME, "summary": reason},
        },
        token,
    )
    if type(body.get("id")) is not int or body["id"] <= 0:
        raise ValueError("dependency check response malformed")
    audit(
        "dependency_gate_published",
        repo=repo,
        head_sha=head,
        conclusion=conclusion,
        check_id=body["id"],
    )


FAILURES = (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError)


def check(
    repo: str,
    branch: str,
    pull: dict,
    policy: dict,
    github: GitHub,
    token: str,
    entries=None,
    results=None,
) -> None:
    head = entries[-1]["headCommit"]["oid"] if entries else pull["head"]["sha"]
    try:
        # The last queue entry's head can include every predecessor. A group
        # with a bot and another PR is refused rather than attributing combined
        # dependency/file changes to one reviewed receipt.
        if entries and len(entries) > 1:
            for entry in entries:
                member = github.get(repo, f"pulls/{entry['pullRequest']['number']}")
                validate_pull(
                    member,
                    repo,
                    branch,
                    entry["pullRequest"]["number"],
                    entry["pullRequest"]["headRefOid"],
                )
                if dependency(member, policy):
                    raise ValueError(
                        "combined dependency queue group requires isolated review"
                    )
            reason = "non-dependency merge group"
        else:
            reason = evaluate(
                repo, branch, pull, policy, github, entries[0] if entries else None
            )
        if entries and github.queue(repo, branch)[: len(entries)] != entries:
            raise ValueError("queue base or merge-group head moved")
        latest = github.get(repo, f"pulls/{pull['number']}")
        validate_pull(latest, repo, branch, pull["number"], pull["head"]["sha"])
        if (
            latest["user"] != pull["user"]
            or latest["base"]["sha"] != pull["base"]["sha"]
        ):
            raise ValueError("PR identity or base moved")
        conclusion = "success"
    except FAILURES as exc:
        conclusion, reason = (
            "failure",
            "dependency evidence unavailable, changed or unapproved",
        )
        # Exception text can contain attacker-controlled API responses.
        audit(
            "dependency_gate_refused",
            repo=repo,
            head_sha=head,
            error=type(exc).__name__,
        )
    if results is None:
        publish(repo, head, conclusion, reason, token)
    elif head not in results or conclusion == "failure":
        # A shared commit may appear on several PRs. A human PR must not
        # overwrite a refusal for a bot PR sharing that commit.
        results[head] = (conclusion, reason)


def tick(policy: dict) -> dict:
    if not enabled():
        audit("dependency_gate_skipped", reason="publisher_disabled")
        return {"action": "skipped", "reason": "publisher_disabled"}
    token = os.environ.get(TOKEN_ENV)
    if not token:
        audit("dependency_gate_skipped", reason="publisher_token_missing")
        return {"action": "skipped", "reason": "publisher_token_missing"}
    github = GitHub(token)
    repo, branch = policy["repo"], policy["base_branch"]
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("repository malformed")
        # Read the complete bounded inventory before publishing any successes.
        pulls = deps._pages(
            repo, f"pulls?{urlencode({'state': 'open', 'base': branch})}", github.rows
        )
        entries = github.queue(repo, branch)
        results = {}
        for listed in pulls:
            pull = github.get(repo, f"pulls/{listed['number']}")
            check(repo, branch, pull, policy, github, token, results=results)
        for index, entry in enumerate(entries):
            pull = github.get(repo, f"pulls/{entry['pullRequest']['number']}")
            check(
                repo, branch, pull, policy, github, token, entries[: index + 1], results
            )
        for head, (conclusion, reason) in results.items():
            publish(repo, head, conclusion, reason, token)
        return {"action": "checked", "pulls": len(pulls), "queue_entries": len(entries)}
    except FAILURES as exc:
        audit(
            "dependency_gate_skipped",
            reason="github_or_evidence_unavailable",
            error=type(exc).__name__,
        )
        # Revoke every known target if discovery failed after targets were read.
        # Unknown targets receive no check and cannot satisfy a required context.
        for pull in locals().get("pulls", []):
            try:
                publish(
                    repo,
                    pull["head"]["sha"],
                    "failure",
                    "gate discovery unavailable",
                    token,
                )
            except FAILURES:
                audit(
                    "dependency_gate_skipped", reason="failure_publication_unavailable"
                )
        for entry in locals().get("entries", []):
            try:
                publish(
                    repo,
                    entry["headCommit"]["oid"],
                    "failure",
                    "gate discovery unavailable",
                    token,
                )
            except FAILURES:
                audit(
                    "dependency_gate_skipped", reason="failure_publication_unavailable"
                )
        return {"action": "refused", "reason": "github_or_evidence_unavailable"}
