"""Authoritative GitHub verification of the PR and issue state a volatile fact asserts.

Every claim sentence must fully match explicit ASCII clause templates: a
reference's state, head SHA or checks, or checks at an explicit SHA. Complete
clauses may join with "and" or ", and", each with its own subject. Every named
reference and SHA produces a predicate; unmatched wording makes the whole
note unsupported and leaves it due. Why: denylist bypasses across five reviews
required a grammar that accepts only claims it fully understands. The verifier
does no database work and never renews anything.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple

from knowledge.freshness import (
    _OPERATIONAL_SUBJECT,
    _OUTSTANDING_GATE,
    _PROVENANCE_SECTION,
    sentences,
    utc,
)

MAX_REFERENCES = 5
MAX_CHECK_RUNS = 100

_NUM_TOKEN = r"[1-9][0-9]{0,6}"
_REPO_TOKEN = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_REF_TOKEN = (
    rf"(?:(?:PR|pull[ \t]+request|issue)[ \t]+#{_NUM_TOKEN}"
    rf"|#{_NUM_TOKEN}|{_REPO_TOKEN}#{_NUM_TOKEN}"
    rf"|https://github\.com/{_REPO_TOKEN}/(?:pull|issues)/{_NUM_TOKEN})"
)
_REF = re.compile(
    rf"https://github\.com/(?P<url_repo>{_REPO_TOKEN})/"
    rf"(?:pull|issues)/(?P<url_number>{_NUM_TOKEN})"
    rf"|(?P<repo>{_REPO_TOKEN})#(?P<number>{_NUM_TOKEN})"
    rf"|(?:(?:PR|pull[ \t]+request|issue)[ \t]+)?#(?P<bare>{_NUM_TOKEN})",
    re.IGNORECASE | re.ASCII,
)
# Case-insensitive keywords never make uppercase hex a supported SHA.
# A SHA must contain a letter, including SHAs consisting entirely of a-f.
_SHA_TOKEN = r"(?-i:(?=[0-9a-f]*[a-f])[0-9a-f]{7,40})"
_CHECK_TOKEN = (
    r"(?:passing|passed|green|succeeded|success|failing|failed|failure|red|"
    r"pending|running|queued)"
)
_JOIN_TOKEN = r"(?:,[ \t]+and[ \t]+|[ \t]+and[ \t]+)"
_JOIN = re.compile(_JOIN_TOKEN, re.IGNORECASE | re.ASCII)
# Each slot is a named capture consumed below. There are no free-text slots.
_TEMPLATE_SPECS = (
    (
        "state",
        rf"(?P<ref>{_REF_TOKEN})[ \t]+is[ \t]+(?P<term>open|closed|merged|draft|a[ \t]+draft)",
    ),
    ("state", rf"(?P<ref>{_REF_TOKEN})[ \t]+(?P<term>has[ \t]+been[ \t]+merged)"),
    ("head", rf"(?P<ref>{_REF_TOKEN})[ \t]+head[ \t]+is[ \t]+(?P<sha>{_SHA_TOKEN})"),
    (
        "checks",
        rf"(?P<ref>{_REF_TOKEN})[ \t]+checks[ \t]+are[ \t]+(?P<term>{_CHECK_TOKEN})",
    ),
    (
        "checks",
        rf"(?P<ref>{_REF_TOKEN})[ \t]+checks[ \t]+(?:are[ \t]+)?(?P<term>{_CHECK_TOKEN})[ \t]+at[ \t]+(?P<sha>{_SHA_TOKEN})",
    ),
    (
        "checks",
        rf"Checks[ \t]+(?:are[ \t]+)?(?P<term>{_CHECK_TOKEN})[ \t]+at[ \t]+(?P<sha>{_SHA_TOKEN})",
    ),
    (
        "checks",
        rf"Checks[ \t]+at[ \t]+(?P<sha>{_SHA_TOKEN})[ \t]+are[ \t]+(?P<term>{_CHECK_TOKEN})",
    ),
)
_TEMPLATES = tuple(
    (kind, re.compile(pattern, re.IGNORECASE | re.ASCII))
    for kind, pattern in _TEMPLATE_SPECS
)
# Fullmatch the sentence before extracting its individually captured clauses.
_CLAUSE_TOKEN = (
    "(?:"
    + "|".join(
        re.sub(r"\(\?P<[a-z]+>", "(?:", pattern) for _, pattern in _TEMPLATE_SPECS
    )
    + ")"
)
_SENTENCE_TEMPLATE = re.compile(
    rf"{_CLAUSE_TOKEN}(?:{_JOIN_TOKEN}{_CLAUSE_TOKEN})*", re.IGNORECASE | re.ASCII
)
_RUN = re.compile(
    r"\b(?:workflow[ \t]+)?(?:run|job)(?:[ \t]+id)?[ \t]*#?\d{4,}\b", re.IGNORECASE
)
_GATE_WORDS = re.compile(
    r"\b(remaining|remains?|awaiting|live validation|acceptance|gates?)\b",
    re.IGNORECASE,
)
# Any operational subject fails closed: GitHub lifecycle and check data
# establish nothing about pilots, deploys, rollouts or approvals, whatever
# the obligation wording around them. deploy\w* and pilot\w* cover inflected
# forms (deployed, deploying, piloted) the shared vocabulary does not name.
_OPERATIONAL_ANY = re.compile(
    rf"\b(?:{_OPERATIONAL_SUBJECT}|deploy\w*|pilot\w*)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(not|never|no longer|isn't|wasn't|aren't|weren't|without|unmerged|"
    r"unless|until)\b|n't\b",
    re.IGNORECASE,
)
_CHECK_TERMS = {
    "passing": "success",
    "passed": "success",
    "green": "success",
    "succeeded": "success",
    "success": "success",
    "failed": "failure",
    "failing": "failure",
    "failure": "failure",
    "red": "failure",
    "pending": "pending",
    "running": "pending",
    "queued": "pending",
}
_GOOD = {"success", "neutral", "skipped"}


class SourceUnavailable(Exception):
    """The source could not answer (transport error, rate limit, 5xx)."""


class BudgetExhausted(Exception):
    """The run's request budget ended; leave the note for a later run."""


class GitHubResponse(NamedTuple):
    status: int
    body: object


Fetch = Callable[[str], GitHubResponse]


@dataclass(frozen=True)
class Predicate:
    kind: str  # state | head | checks
    repo: str
    number: int | None
    expected: str
    sha: str | None = None


@dataclass(frozen=True)
class Verdict:
    status: str  # success | failed | unavailable | unsupported
    reason: str
    evidence: list[str] = field(default_factory=list)
    observed_at: datetime | None = None


class _NotFound(Exception):
    """GitHub has no such PR, issue or commit."""


class _Unsupported(Exception):
    pass


class _Mismatch(Exception):
    """GitHub answered, and the claimed state is not the actual state."""


def extract_predicates(
    *, title: str, content: str | None, default_repo: str
) -> list[Predicate]:
    """Fully captured template predicates, or ``_Unsupported`` for the whole note.

    Diagnostics may only reject. Acceptance requires a full sentence match,
    followed by a full match for each explicit-subject clause. No reference,
    SHA or qualifying wording can be discarded during extraction.
    """
    claim = _PROVENANCE_SECTION.sub("", content or "")
    predicates: list[Predicate] = []
    for sentence in sentences(f"{title}\n{claim}"):
        sentence = sentence.strip(" \t").removesuffix(".")
        if (
            _GATE_WORDS.search(sentence)
            or _OUTSTANDING_GATE.search(sentence)
            or _OPERATIONAL_ANY.search(sentence)
        ):
            raise _Unsupported("acceptance gate is not verifiable from GitHub")
        if _RUN.search(sentence):
            raise _Unsupported("workflow run or job state is not verifiable")
        if _NEGATION.search(sentence):
            raise _Unsupported("negated or conditional state claim")
        if _SENTENCE_TEMPLATE.fullmatch(sentence) is None:
            raise _Unsupported(
                f"claim does not match a supported template: {sentence[:60]!r}"
            )
        for clause in _JOIN.split(sentence):
            for kind, template in _TEMPLATES:
                match = template.fullmatch(clause)
                if match is None:
                    continue
                captures = match.groupdict()
                repo, number = default_repo, None
                if captures.get("ref") is not None:
                    ref = _REF.fullmatch(captures["ref"])
                    repo = ref["url_repo"] or ref["repo"] or default_repo
                    number = int(ref["url_number"] or ref["number"] or ref["bare"])
                term = captures.get("term", "head").lower()
                if kind == "state":
                    term = re.split(r"[ \t]+", term)[-1]
                elif kind == "checks":
                    term = _CHECK_TERMS[term]
                predicates.append(
                    Predicate(kind, repo, number, term, sha=captures.get("sha"))
                )
                break
            else:
                # Keep extraction fail-closed if the sentence grammar changes.
                raise _Unsupported(
                    f"clause does not match a supported template: {clause[:60]!r}"
                )
    if not predicates:
        raise _Unsupported("no verifiable predicate")
    deduped = list(dict.fromkeys(predicates))
    numbered = {(p.repo, p.number) for p in deduped if p.number is not None}
    if len(numbered) > MAX_REFERENCES:
        raise _Unsupported("too many references to verify")
    return deduped


class GitHubVerifier:
    """Verify a note's predicates against GitHub through an injected fetcher.

    Responses are cached per verifier so notes asserting the same PR share one
    request, and every distinct request spends from a shared budget.
    """

    def __init__(
        self,
        fetch: Fetch,
        *,
        repo: str,
        clock: Callable[[], datetime],
        max_requests: int = 60,
    ) -> None:
        self._fetch = fetch
        self._repo = repo
        self._clock = clock
        self._remaining = max_requests
        self._cache: dict[str, tuple[GitHubResponse, datetime]] = {}
        self._used: list[datetime] = []
        self.requests = 0

    def _get(self, path: str, *, body_type: type = dict) -> object:
        cached = self._cache.get(path)
        if cached is None:
            if self._remaining <= 0:
                raise BudgetExhausted(path)
            self._remaining -= 1
            self.requests += 1
            started = utc(self._clock())
            cached = (self._fetch(path), started)
            self._cache[path] = cached
        response, fetched_at = cached
        self._used.append(fetched_at)
        if response.status == 404:
            raise _NotFound(path)
        if response.status != 200:
            raise SourceUnavailable(f"http_{response.status}")
        if not isinstance(response.body, body_type):
            raise SourceUnavailable("malformed_response")
        return response.body

    def verify(self, *, title: str, content: str | None) -> Verdict:
        try:
            predicates = extract_predicates(
                title=title, content=content, default_repo=self._repo
            )
        except _Unsupported as exc:
            return Verdict("unsupported", str(exc))
        if any(p.repo.lower() != self._repo.lower() for p in predicates):
            return Verdict("unsupported", "repository is not verifiable")
        self._used = []
        evidence: list[str] = []
        unavailable: str | None = None
        for predicate in predicates:
            try:
                evidence.append(self._evaluate(predicate))
            except SourceUnavailable as exc:
                unavailable = unavailable or str(exc)
            except _NotFound as exc:
                return Verdict("failed", f"not found: {exc}", evidence, self._seen())
            except _Mismatch as exc:
                return Verdict("failed", str(exc), evidence, self._seen())
            except _Unsupported as exc:
                return Verdict("unsupported", str(exc))
        if unavailable is not None:
            return Verdict("unavailable", unavailable, [], None)
        return Verdict("success", "verified", evidence, self._seen())

    def _seen(self) -> datetime | None:
        """The oldest response used: the evidence is no fresher than that."""
        return min(self._used) if self._used else None

    def _issue(self, repo: str, number: int) -> dict:
        body = self._get(f"/repos/{repo}/issues/{number}")
        if (
            type(body.get("number")) is not int
            or body["number"] != number
            or not isinstance(body.get("state"), str)
            or body["state"] not in {"open", "closed"}
        ):
            raise SourceUnavailable("malformed_response")
        return body

    def _pull(self, repo: str, number: int) -> dict:
        issue = self._issue(repo, number)
        if "pull_request" not in issue:
            raise _Unsupported(f"#{number} is not a pull request")
        body = self._get(f"/repos/{repo}/pulls/{number}")
        head = body.get("head")
        if (
            type(body.get("number")) is not int
            or body["number"] != number
            or not isinstance(body.get("state"), str)
            or body["state"] not in {"open", "closed"}
            or not isinstance(body.get("merged"), bool)
            or not isinstance(body.get("draft"), bool)
            or not isinstance(head, dict)
            or not re.fullmatch(r"[0-9a-f]{40}", str(head.get("sha")))
        ):
            raise SourceUnavailable("malformed_response")
        return body

    def _evaluate(self, p: Predicate) -> str:
        label = f"{p.repo}#{p.number}"
        if p.kind == "state":
            if p.expected in {"open", "closed"}:
                actual = self._issue(p.repo, p.number)["state"]
                if actual != p.expected:
                    raise _Mismatch(f"{label} is {actual}, not {p.expected}")
                return f"{label} is {actual}"
            pull = self._pull(p.repo, p.number)
            if p.expected == "merged":
                if pull["merged"] is not True:
                    raise _Mismatch(f"{label} is not merged (state {pull['state']})")
                return f"{label} is merged"
            if pull["state"] != "open" or pull["draft"] is not True:
                raise _Mismatch(f"{label} is not an open draft")
            return f"{label} is an open draft"
        if p.kind == "head":
            pull = self._pull(p.repo, p.number)
            sha = pull["head"]["sha"]
            if not sha.startswith(p.sha):
                raise _Mismatch(f"{label} head is {sha[:12]}, not {p.sha}")
            return f"{label} head is {sha[:12]}"
        sha = p.sha
        if sha is None:
            sha = self._pull(p.repo, p.number)["head"]["sha"]
        elif p.number is not None:
            self._pull(p.repo, p.number)
            associated = self._get(
                f"/repos/{p.repo}/commits/{sha}/pulls?per_page=100", body_type=list
            )
            if any(
                not isinstance(pull, dict)
                or type(pull.get("number")) is not int
                or pull["number"] < 1
                for pull in associated
            ):
                raise SourceUnavailable("malformed_response")
            if not any(pull["number"] == p.number for pull in associated):
                if len(associated) >= MAX_CHECK_RUNS:
                    raise _Unsupported("PR association response may be truncated")
                raise _Mismatch(f"{label} is not associated with {sha}")
        outcomes = [
            outcome
            for outcome in (self._check_runs(p.repo, sha), self._statuses(p.repo, sha))
            if outcome is not None
        ]
        if not outcomes:
            raise _Mismatch(f"no checks exist for {sha[:12]}")
        # Failure outranks pending; success needs every source to be success.
        if "failure" in outcomes:
            actual = "failure"
        elif "pending" in outcomes:
            actual = "pending"
        else:
            actual = "success"
        if actual != p.expected:
            raise _Mismatch(f"checks at {sha[:12]} are {actual}, not {p.expected}")
        return f"checks at {sha[:12]} are {actual}"

    def _check_runs(self, repo: str, sha: str) -> str | None:
        body = self._get(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")
        runs = body.get("check_runs")
        total = body.get("total_count")
        if not isinstance(runs, list) or type(total) is not int or total < 0:
            raise SourceUnavailable("malformed_response")
        if total < len(runs) or (total and not runs):
            raise SourceUnavailable("malformed_response")
        if total > MAX_CHECK_RUNS or total > len(runs):
            raise _Unsupported("more check runs than one response establishes")
        if any(
            not isinstance(run, dict)
            or not str(run.get("head_sha", "")).startswith(sha.lower())
            for run in runs
        ):
            raise SourceUnavailable("check runs are not tied to the SHA")
        if not runs:
            return None
        if any(
            not isinstance(run.get("status"), str)
            or run["status"] not in {"completed", "in_progress", "queued"}
            or (
                run.get("status") == "completed"
                and not isinstance(run.get("conclusion"), str)
            )
            for run in runs
        ):
            raise SourceUnavailable("malformed_response")
        if any(
            run.get("status") == "completed" and run.get("conclusion") not in _GOOD
            for run in runs
        ):
            return "failure"
        if any(run.get("status") != "completed" for run in runs):
            return "pending"
        if all(run.get("conclusion") in _GOOD for run in runs):
            return "success"
        return "failure"

    def _statuses(self, repo: str, sha: str) -> str | None:
        """The combined commit status: where a required gate such as pr-checks lives."""
        body = self._get(f"/repos/{repo}/commits/{sha}/status?per_page=100")
        statuses = body.get("statuses")
        total = body.get("total_count")
        if not isinstance(statuses, list) or type(total) is not int or total < 0:
            raise SourceUnavailable("malformed_response")
        if total < len(statuses) or (total and not statuses):
            raise SourceUnavailable("malformed_response")
        if total > MAX_CHECK_RUNS or total > len(statuses):
            raise _Unsupported("more commit statuses than one response establishes")
        if not str(body.get("sha", "")).startswith(sha.lower()):
            raise SourceUnavailable("commit statuses are not tied to the SHA")
        if not statuses:
            return None
        states = [
            status.get("state") if isinstance(status, dict) else None
            for status in statuses
        ]
        if any(
            not isinstance(state, str)
            or state not in {"success", "pending", "failure", "error"}
            for state in states
        ):
            raise SourceUnavailable("malformed_response")
        if any(state in {"failure", "error"} for state in states):
            return "failure"
        return "pending" if "pending" in states else "success"


def httpx_fetcher(client, *, token: str = "") -> Fetch:
    """Production fetcher: a transport failure is unavailable, never a verdict."""
    import httpx
    from core.github import GITHUB_API

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "monolith-knowledge-review",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def fetch(path: str) -> GitHubResponse:
        try:
            response = client.get(f"{GITHUB_API}{path}", headers=headers)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(type(exc).__name__) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        return GitHubResponse(response.status_code, body)

    return fetch
