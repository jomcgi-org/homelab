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
    """Coverage cannot improve without a claim or relevant source change."""

    def __init__(self, message, *, identity=None):
        super().__init__(message)
        self.identity = identity


def collect_sources(
    claim: dict,
    *,
    fetch=None,
    bounded_fetch=None,
    initial_bytes=0,
    repo: str = GITHUB_REPO,
    main_sha: str | None = None,
) -> tuple[list[dict], datetime | None]:
    """Bound each claim; initial_bytes includes a batch's already-fetched pin."""
    from knowledge import review_coverage as coverage

    staged = coverage.enabled()
    if fetch is None:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            deadline_at = time.monotonic() + DEADLINE_SECONDS
            wire_bytes = 0

            def charge(size):
                nonlocal wire_bytes
                wire_bytes += size
                if wire_bytes > coverage.MAX_TRANSFER_BYTES:
                    raise EvidenceUnsupported("source transport byte bound exceeded")

            def read(path, max_bytes=coverage.MAX_RESPONSE_BYTES):
                if staged:
                    return coverage.read_response(
                        client,
                        path,
                        deadline_at=deadline_at,
                        charge=charge,
                        max_bytes=max_bytes,
                    )
                response = client.get(GITHUB_API + path, headers=_github_headers())
                if response.status_code != 200:
                    raise EvidenceUnavailable(f"GitHub returned {response.status_code}")
                return response.json()

            return collect_sources(
                claim,
                fetch=read,
                bounded_fetch=read,
                initial_bytes=initial_bytes,
                repo=repo,
                main_sha=main_sha,
            )
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
    literal_clauses = coverage.literal_clauses(claim) if staged else None
    if literal_clauses is not None:
        # Strings inside a literal are evidence to locate, not additional
        # repository pointers or GitHub lifecycle subjects.
        numbers = []
        paths = sorted({path for path, _ in literal_clauses})
    if len(numbers) > 3 or len(paths) > 4:
        raise EvidenceUnsupported("claim exceeds source coverage bounds")
    if any(".." in PurePosixPath(path).parts for path in paths):
        raise EvidenceUnsupported("invalid repository path")
    sources = []
    started = time.monotonic()
    requests = 0
    transferred = initial_bytes
    observed = None

    def get(path, max_bytes=None):
        nonlocal requests, observed, transferred
        if staged and requests >= MAX_REQUESTS:
            raise EvidenceUnsupported("source collection request bound exceeded")
        if requests >= MAX_REQUESTS or time.monotonic() - started >= DEADLINE_SECONDS:
            raise EvidenceUnavailable("source collection budget exhausted")
        requests += 1
        before = datetime.now(UTC)
        observed = observed or before
        try:
            result = (
                bounded_fetch(path, max_bytes=max_bytes)
                if bounded_fetch is not None and max_bytes is not None
                else fetch(path)
            )
        except EvidenceUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise EvidenceUnavailable(str(exc)) from exc
        if not isinstance(result, dict):
            raise EvidenceUnavailable("malformed GitHub response")
        if staged:
            size = coverage.response_size(result)
            transferred += size
            if (
                size > coverage.MAX_RESPONSE_BYTES
                or max_bytes is not None
                and size > max_bytes
                or transferred > coverage.MAX_TRANSFER_BYTES
            ):
                raise EvidenceUnsupported("source response byte bound exceeded")
            if time.monotonic() - started >= DEADLINE_SECONDS:
                raise EvidenceUnavailable("source collection deadline exhausted")
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
        main = main_sha or get(f"/repos/{repo}/git/ref/heads/main").get(
            "object", {}
        ).get("sha")
        if not re.fullmatch(r"[0-9a-f]{40}", str(main)):
            raise EvidenceUnavailable("missing main commit")
        if staged:
            try:
                identity = coverage.file_identity(
                    paths, main, lambda suffix: get(f"/repos/{repo}" + suffix)
                )
            except EvidenceUnsupported as exc:
                raise EvidenceUnsupported(
                    str(exc),
                    identity={
                        "version": coverage.VERSION,
                        "files": dict.fromkeys(paths),
                    },
                ) from exc
            try:
                # A rejected streaming response still charges its final chunk.
                projected = transferred + coverage.STREAM_CHUNK_BYTES
                for item in identity["files"].values():
                    if item["type"] != "blob" or item["mode"] not in {
                        "100644",
                        "100755",
                    }:
                        raise EvidenceUnsupported("unsupported source file type")
                    if item["size"] > coverage.MAX_FILE_BYTES:
                        raise EvidenceUnsupported("source decode byte bound exceeded")
                    projected += coverage.blob_response_bound(item)
                if projected > coverage.MAX_TRANSFER_BYTES:
                    raise EvidenceUnsupported(
                        "source aggregate transport byte bound exceeded"
                    )
                contents = {}
                for path, item in identity["files"].items():
                    body = get(
                        f"/repos/{repo}/git/blobs/{item['sha']}",
                        max_bytes=coverage.blob_response_bound(item),
                    )
                    contents[path] = coverage.decode_blob(body, item)
                complete = [
                    {
                        "id": f"s{len(sources) + index}",
                        "subject": f"file:{path}",
                        "url": f"https://github.com/{repo}/blob/{main}/{path}",
                        "text": content,
                    }
                    for index, (path, content) in enumerate(contents.items(), 1)
                ]
                if coverage.serialized_size(sources + complete) > MAX_BYTES:
                    complete = coverage.witnesses(claim, contents, identity, repo, main)
                if (
                    len(sources + complete) > MAX_SOURCES
                    or coverage.serialized_size(sources + complete) > MAX_BYTES
                ):
                    raise EvidenceUnsupported("source evidence exceeds byte bound")
                return sources + complete, observed
            except EvidenceUnsupported as exc:
                raise EvidenceUnsupported(str(exc), identity=identity) from exc
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
