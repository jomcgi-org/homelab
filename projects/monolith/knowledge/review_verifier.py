"""Authoritative GitHub verification of the PR and issue state a volatile fact asserts.

Only predicates the GitHub response establishes are verified: issue or PR
open/closed, PR merged and draft, a PR head SHA, and check runs tied to one
exact SHA. Every other state term (a workflow run or job, "ready", "blocked",
a free-text acceptance gate) is unsupported: the verdict records why and the
fact stays due. The verifier does no database work and never renews anything.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple

from knowledge.freshness import (
    _OUTSTANDING_GATE,
    _PROVENANCE_SECTION,
    _SHA,
    _STATE,
    has_instance,
    sentences,
    utc,
)

MAX_REFERENCES = 5
MAX_CHECK_RUNS = 100

_REF = re.compile(
    r"github\.com/(?P<urepo>[\w.-]+/[\w.-]+)/(?:pull|issues)/(?P<unum>\d+)"
    r"|(?P<repo>[\w.-]+/[\w.-]+)#(?P<num>\d{1,7})\b"
    r"|\b(?:pull request|pull|pr|issue)s?[ \t]*#?(?P<wnum>\d{1,7})\b"
    r"|(?<![\w/&])#(?P<bare>\d{1,7})\b",
    re.IGNORECASE,
)
_RUN = re.compile(
    r"\b(?:workflow[ \t]+)?(?:run|job)(?:[ \t]+id)?[ \t]*#?\d{4,}\b", re.IGNORECASE
)
_LEFTOVER_REF = re.compile(r"#\d|\b\d{1,7}\b")
_LEFTOVER_NUM = re.compile(r"\d{1,7}")
_CHECK_WORD = re.compile(r"\b(checks?|ci|check[- ]runs?)\b", re.IGNORECASE)
_GATE_WORDS = re.compile(
    r"\b(remaining|remains?|awaiting|live validation|acceptance|gates?)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(not|never|no longer|isn't|wasn't|aren't|weren't|without|unmerged|"
    r"unless|until)\b|n't\b",
    re.IGNORECASE,
)
_STATE_TERMS = {
    "open": "open",
    "opened": "open",
    "reopened": "open",
    "closed": "closed",
    "merged": "merged",
    "draft": "draft",
}
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


def _refs(sentence: str, default_repo: str) -> list[tuple[int, str, int]]:
    """(position, repository, number) for every PR or issue the sentence names."""
    found = []
    for match in _REF.finditer(sentence):
        if match["unum"]:
            found.append((match.start(), match["urepo"], int(match["unum"])))
        elif match["num"]:
            found.append((match.start(), match["repo"], int(match["num"])))
        elif match["wnum"]:
            found.append((match.start(), default_repo, int(match["wnum"])))
        else:
            found.append((match.start(), default_repo, int(match["bare"])))
    return found


def _subject(position: int, refs: list[tuple[int, str, int]]):
    """The reference a state term describes: the last one before it, else the first after."""
    before = [ref for ref in refs if ref[0] < position]
    return max(before) if before else min(refs)


def _distinct_shas(sentence: str) -> list[str]:
    """Commit SHAs named, an abbreviation and its full form counting once."""
    tokens = {
        token
        for token in _SHA.findall(sentence)
        if any(c.isdigit() for c in token) and any(c in "abcdef" for c in token)
    }
    return sorted(
        token
        for token in tokens
        if not any(other != token and other.startswith(token) for other in tokens)
    )


def extract_predicates(
    *, title: str, content: str | None, default_repo: str
) -> list[Predicate]:
    """Predicates the claim asserts, or ``_Unsupported`` naming the first gap.

    Fails closed: a renewal extends the whole note, so every sentence that
    states a state or an acceptance gate must produce a predicate tied to a
    concrete instance. A state claim that refers back to an earlier sentence
    ("Its checks are failing") or a gate written as its own sentence cannot be
    verified, so the whole claim is unsupported rather than partly renewed.
    Every distinct named reference anywhere in the claim must also be covered
    by a predicate: a follow-on sentence that names a reference inherits the
    earlier state, so per-sentence coverage would renew a strict subset.
    A number left over after parsed references and SHAs are stripped is an
    uncovered reference the patterns above do not name ("PRs 6821 and 6822",
    "PR #6821/#6822", "Issues 5, 6 and 7"), and is likewise unsupported.
    """
    claim = _PROVENANCE_SECTION.sub("", content or "")
    predicates: list[Predicate] = []
    note_named: set[tuple[str, int]] = set()
    note_covered: set[tuple[str, int]] = set()
    for sentence in sentences(f"{title}\n{claim}"):
        if _GATE_WORDS.search(sentence) or _OUTSTANDING_GATE.search(sentence):
            raise _Unsupported("acceptance gate is not verifiable from GitHub")
        note_named.update(
            (repo, number) for _, repo, number in _refs(sentence, default_repo)
        )
        if not _STATE.search(sentence):
            continue
        if not has_instance(sentence):
            raise _Unsupported("state claim names no concrete instance")
        if _NEGATION.search(sentence):
            raise _Unsupported("negated or conditional state claim")
        refs = _refs(sentence, default_repo)
        shas = _distinct_shas(sentence)
        if _RUN.search(sentence):
            raise _Unsupported("workflow run or job state is not verifiable")
        stripped = _SHA.sub(" ", _REF.sub(" ", sentence))
        if _LEFTOVER_REF.search(stripped):
            number = _LEFTOVER_NUM.search(stripped).group(0)
            raise _Unsupported(
                f"reference #{int(number)} is not covered by a verifiable predicate"
            )
        checks = bool(_CHECK_WORD.search(sentence))
        consumed: set[str] = set()
        named = {(repo, number) for _, repo, number in refs}
        covered: set[tuple[str, int]] = set()
        for match in _STATE.finditer(sentence):
            term = match.group(1).lower()
            if term in _STATE_TERMS:
                if not refs:
                    raise _Unsupported(f"state {term!r} names no PR or issue")
                _, repo, number = _subject(match.start(), refs)
                predicates.append(Predicate("state", repo, number, _STATE_TERMS[term]))
                covered.add((repo, number))
            elif term == "head":
                if not refs or len(shas) != 1:
                    raise _Unsupported("head claim needs one PR and one SHA")
                _, repo, number = _subject(match.start(), refs)
                predicates.append(Predicate("head", repo, number, "head", sha=shas[0]))
                covered.add((repo, number))
                consumed.add(shas[0])
            elif term in _CHECK_TERMS and checks:
                if len(shas) > 1:
                    raise _Unsupported("checks claim names more than one SHA")
                if shas:
                    if len(named) > 1:
                        raise _Unsupported(
                            "checks at a SHA name more than one reference"
                        )
                    repo = refs[0][1] if refs else default_repo
                    number = refs[0][2] if refs else None
                    predicates.append(
                        Predicate(
                            "checks", repo, number, _CHECK_TERMS[term], sha=shas[0]
                        )
                    )
                    consumed.add(shas[0])
                    covered.update(named)
                elif refs:
                    _, repo, number = _subject(match.start(), refs)
                    predicates.append(
                        Predicate("checks", repo, number, _CHECK_TERMS[term])
                    )
                    covered.add((repo, number))
                else:
                    raise _Unsupported("checks claim names no PR or SHA")
            else:
                raise _Unsupported(f"state {term!r} is not verifiable from GitHub")
        if set(shas) - consumed:
            raise _Unsupported("SHA assertion is not verifiable from GitHub")
        note_covered.update(covered)
    if note_named - note_covered:
        _, number = sorted(note_named - note_covered)[0]
        raise _Unsupported(
            f"reference #{number} is not covered by a verifiable predicate"
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
        if body.get("number") != number or body.get("state") not in {"open", "closed"}:
            raise SourceUnavailable("malformed_response")
        return body

    def _pull(self, repo: str, number: int) -> dict:
        issue = self._issue(repo, number)
        if "pull_request" not in issue:
            raise _Unsupported(f"#{number} is not a pull request")
        body = self._get(f"/repos/{repo}/pulls/{number}")
        head = body.get("head")
        if (
            body.get("number") != number
            or body.get("state") not in {"open", "closed"}
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
        if not isinstance(runs, list) or not isinstance(total, int):
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
        if not isinstance(statuses, list) or not isinstance(total, int):
            raise SourceUnavailable("malformed_response")
        if not statuses:
            return None
        if total > MAX_CHECK_RUNS or total > len(statuses):
            raise _Unsupported("more commit statuses than one response establishes")
        if not str(body.get("sha", "")).startswith(sha.lower()):
            raise SourceUnavailable("commit statuses are not tied to the SHA")
        states = {
            status.get("state") if isinstance(status, dict) else None
            for status in statuses
        }
        if not states <= {"success", "pending", "failure", "error"}:
            raise SourceUnavailable("malformed_response")
        if states & {"failure", "error"}:
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
