"""Bounded primary evidence for prose review, fetched by the server, not the model.

Only the configured repository is reachable. PR/issue fields come from GitHub;
files come from one pinned main commit. Evidence is data, never instructions.
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
from core.github import GITHUB_API, GITHUB_REPO, _github_headers

MAX_REQUESTS = 16
MAX_SOURCES = 12
MAX_BYTES = 48_000
DEADLINE_SECONDS = 90
_REF = re.compile(
    r"(?:\b(?:PR|pull request|issue)\s*#|(?<!\w)#)([1-9][0-9]{0,6})\b", re.IGNORECASE
)
_URL_REF = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/(?:pull|issues)/([1-9][0-9]{0,6})"
)
_REPO_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)(?:/|\b)")
_NAMED_REF = re.compile(r"([\w.-]+/[\w.-]+)#([1-9][0-9]{0,6})\b")
_URL_FILE = re.compile(
    r"https://github\.com/[\w.-]+/[\w.-]+/blob/[^/\s]+/((?:projects|docs|bazel)/[\w./-]+\.[A-Za-z0-9]+)"
)
_FILE = re.compile(
    r"(?<![\w/.-])((?:projects|docs|bazel)/[\w./-]+\.[A-Za-z0-9]+)(?::[0-9]+)?"
)


class EvidenceUnavailable(ValueError):
    pass


class EvidenceUnsupported(EvidenceUnavailable):
    """Coverage cannot improve by retrying the same claim revision."""


def collect_sources(
    claim: dict, *, fetch=None, repo: str = GITHUB_REPO
) -> tuple[list[dict], datetime | None]:
    """No retries; one shared request, byte and elapsed-time bound per claim."""
    if fetch is None:
        with httpx.Client(timeout=10, follow_redirects=False) as client:

            def read(path):
                response = client.get(GITHUB_API + path, headers=_github_headers())
                if response.status_code != 200:
                    raise EvidenceUnavailable(f"GitHub returned {response.status_code}")
                return response.json()

            return collect_sources(claim, fetch=read, repo=repo)
    prose = f"{claim['title']}\n{claim.get('content') or ''}"
    if claim.get("scope") not in (None, f"repo:{repo}"):
        raise EvidenceUnsupported("claim scope does not match the evidence repository")
    if len(prose.encode()) > 32_000:
        raise EvidenceUnsupported("claim exceeds complete-review size bound")
    urls = _URL_REF.findall(prose)
    named = _NAMED_REF.findall(prose)
    if any(owner.lower() != repo.lower() for owner in _REPO_URL.findall(prose)) or any(
        owner.lower() != repo.lower() for owner, _ in named
    ):
        raise EvidenceUnsupported("claim references another repository")
    numbers = sorted(
        {int(n) for n in _REF.findall(prose)} | {int(n) for _, n in urls + named}
    )
    paths = sorted(set(_FILE.findall(prose)) | set(_URL_FILE.findall(prose)))
    if len(numbers) > 3 or len(paths) > 4:
        raise EvidenceUnsupported("claim exceeds source coverage bounds")
    if any(".." in PurePosixPath(path).parts for path in paths):
        raise EvidenceUnsupported("invalid repository path")
    sources = []
    started = time.monotonic()
    requests = 0
    observed = None

    def get(path):
        nonlocal requests, observed
        if requests >= MAX_REQUESTS or time.monotonic() - started >= DEADLINE_SECONDS:
            raise EvidenceUnavailable("source collection budget exhausted")
        requests += 1
        before = datetime.now(UTC)
        observed = observed or before
        try:
            result = fetch(path)
        except (httpx.HTTPError, ValueError) as exc:
            raise EvidenceUnavailable(str(exc)) from exc
        if not isinstance(result, dict):
            raise EvidenceUnavailable("malformed GitHub response")
        return result

    def add(subject, url, body):
        sources.append(
            {
                "id": f"s{len(sources) + 1}",
                "subject": subject,
                "url": url,
                "text": json.dumps(body, sort_keys=True)
                if isinstance(body, dict)
                else body,
            }
        )
        if len(sources) > MAX_SOURCES or len(json.dumps(sources).encode()) > MAX_BYTES:
            raise EvidenceUnsupported("source evidence exceeds byte bound")

    for number in numbers:
        subject = f"github:{number}"
        path = f"/repos/{repo}/issues/{number}"
        issue = get(path)
        if issue.get("number") != number or issue.get("state") not in {
            "open",
            "closed",
        }:
            raise EvidenceUnavailable("malformed issue identity or state")
        add(
            subject,
            f"https://api.github.com{path}",
            {
                k: issue.get(k)
                for k in (
                    "number",
                    "state",
                    "title",
                    "labels",
                    "closed_at",
                    "updated_at",
                )
            },
        )
        if "pull_request" not in issue:
            continue
        path = f"/repos/{repo}/pulls/{number}"
        pull = get(path)
        sha = pull.get("head", {}).get("sha")
        if (
            pull.get("number") != number
            or not re.fullmatch(r"[0-9a-f]{40}", str(sha))
            or pull.get("state") not in {"open", "closed"}
            or type(pull.get("draft")) is not bool
            or type(pull.get("merged")) is not bool
        ):
            raise EvidenceUnavailable("malformed pull request identity or head")
        fields = {
            k: pull.get(k)
            for k in (
                "number",
                "state",
                "draft",
                "merged",
                "merged_at",
                "merge_commit_sha",
            )
        }
        fields["head"] = {"sha": sha, "ref": pull["head"].get("ref")}
        fields["base"] = {k: pull.get("base", {}).get(k) for k in ("sha", "ref")}
        add(subject, f"https://api.github.com{path}", fields)
        for suffix in ("status", "check-runs"):
            path = f"/repos/{repo}/commits/{sha}/{suffix}?per_page=100"
            checks = get(path)
            if checks.get("total_count", 101) > 100:
                raise EvidenceUnavailable("checks exceed one complete page")
            if suffix == "check-runs":
                runs = checks.get("check_runs")
                if (
                    not isinstance(runs, list)
                    or len(runs) != checks.get("total_count")
                    or any(
                        not isinstance(r, dict) or r.get("head_sha") != sha
                        for r in runs
                    )
                ):
                    raise EvidenceUnavailable("incomplete check runs")
                checks = {
                    "sha": sha,
                    "check_runs": [
                        {
                            k: r.get(k)
                            for k in (
                                "name",
                                "head_sha",
                                "status",
                                "conclusion",
                                "completed_at",
                            )
                        }
                        for r in runs
                    ],
                }
            else:
                statuses = checks.get("statuses")
                if (
                    checks.get("sha") != sha
                    or not isinstance(statuses, list)
                    or len(statuses) != checks.get("total_count")
                ):
                    raise EvidenceUnavailable("incomplete commit statuses")
                checks = {
                    "sha": sha,
                    "state": checks.get("state"),
                    "statuses": [
                        {k: r.get(k) for k in ("context", "state", "updated_at")}
                        for r in statuses
                    ],
                }
            add(subject, f"https://api.github.com{path}", checks)
    if paths:
        main = get(f"/repos/{repo}/git/ref/heads/main").get("object", {}).get("sha")
        if not re.fullmatch(r"[0-9a-f]{40}", str(main)):
            raise EvidenceUnavailable("missing main commit")
        for path in paths:
            body = get(f"/repos/{repo}/contents/{quote(path, safe='/')}?ref={main}")
            if (
                body.get("type") != "file"
                or body.get("path") != path
                or body.get("encoding") != "base64"
                or body.get("size", MAX_BYTES + 1) > MAX_BYTES
            ):
                raise EvidenceUnavailable("unsupported file representation")
            try:
                content = base64.b64decode(body["content"]).decode("utf-8")
            except (KeyError, ValueError, UnicodeError) as exc:
                raise EvidenceUnavailable("invalid source file") from exc
            if len(content.encode()) != body.get("size"):
                raise EvidenceUnavailable("source file size mismatch")
            add(
                f"file:{path}", f"https://github.com/{repo}/blob/{main}/{path}", content
            )
    return sources, observed
