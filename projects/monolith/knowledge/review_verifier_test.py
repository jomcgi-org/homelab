"""GitHub verifier: only what the response establishes, everything else due."""

from datetime import datetime, timedelta, timezone

import pytest

from knowledge.review_verifier import (
    BudgetExhausted,
    GitHubResponse,
    GitHubVerifier,
    Predicate,
    SourceUnavailable,
    extract_predicates,
    httpx_fetcher,
)

REPO = "jomcgi-org/homelab"
HEAD = "de02262a35e221804ead81d6e7fe15fa87b416e8"
T0 = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


class Fake:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        route = self.routes[path]
        if isinstance(route, Exception):
            raise route
        return (
            route if isinstance(route, GitHubResponse) else GitHubResponse(200, route)
        )


def issue(number, state="open", pull=False):
    body = {"number": number, "state": state}
    if pull:
        body["pull_request"] = {}
    return body


def pull(number, *, state="open", merged=False, draft=False, sha=HEAD):
    return {
        "number": number,
        "state": state,
        "merged": merged,
        "draft": draft,
        "head": {"sha": sha},
    }


def runs(*conclusions, sha=HEAD, status="completed"):
    return {
        "total_count": len(conclusions),
        "check_runs": [
            {"head_sha": sha, "status": status, "conclusion": c} for c in conclusions
        ],
    }


def combined(state=None, sha=HEAD):
    """The combined commit status: where a commit-status gate like pr-checks lives."""
    statuses = [{"state": state}] if state else []
    return {"state": state or "pending", "sha": sha, "total_count": len(statuses), "statuses": statuses}


def status_path(sha):
    return f"/repos/{REPO}/commits/{sha}/status?per_page=100"


def verify(routes, title, content=None, **options):
    fake = Fake(routes)
    verifier = GitHubVerifier(fake, repo=REPO, clock=Clock(), **options)
    return verifier.verify(title=title, content=content), fake


def test_issue_and_pr_open_closed_merged_draft_are_verified():
    routes = {
        f"/repos/{REPO}/issues/1": issue(1, "open"),
        f"/repos/{REPO}/issues/2": issue(2, "closed", pull=True),
        f"/repos/{REPO}/pulls/2": pull(2, state="closed", merged=True),
        f"/repos/{REPO}/issues/3": issue(3, pull=True),
        f"/repos/{REPO}/pulls/3": pull(3, draft=True),
    }
    verdict, _ = verify(
        routes,
        "Issue #1 is open",
        "PR #2 is merged.\nPull request #3 is draft.",
    )
    assert verdict.status == "success"
    assert verdict.evidence == [
        f"{REPO}#1 is open",
        f"{REPO}#2 is merged",
        f"{REPO}#3 is an open draft",
    ]
    assert T0 < verdict.observed_at <= T0 + timedelta(seconds=10)


@pytest.mark.parametrize(
    ("title", "routes", "reason"),
    [
        (
            "Issue #1 is open",
            {f"/repos/{REPO}/issues/1": issue(1, "closed")},
            "is closed, not open",
        ),
        (
            "PR #2 is merged",
            {
                f"/repos/{REPO}/issues/2": issue(2, "closed", pull=True),
                f"/repos/{REPO}/pulls/2": pull(2, state="closed"),
            },
            "is not merged",
        ),
        (
            "PR #3 is draft",
            {
                f"/repos/{REPO}/issues/3": issue(3, pull=True),
                f"/repos/{REPO}/pulls/3": pull(3, draft=False),
            },
            "is not an open draft",
        ),
        (
            "Issue #4 is open",
            {f"/repos/{REPO}/issues/4": GitHubResponse(404, {})},
            "not found",
        ),
    ],
)
def test_changed_state_is_failed_and_never_success(title, routes, reason):
    verdict, _ = verify(routes, title)
    assert verdict.status == "failed"
    assert reason in verdict.reason


def test_head_sha_and_checks_are_tied_to_that_sha():
    routes = {
        f"/repos/{REPO}/commits/{HEAD[:7]}/check-runs?per_page=100": runs("success"),
        status_path(HEAD[:7]): combined(),
        status_path(HEAD): combined("success"),
        f"/repos/{REPO}/issues/6821": issue(6821, pull=True),
        f"/repos/{REPO}/pulls/6821": pull(6821),
        f"/repos/{REPO}/commits/{HEAD}/check-runs?per_page=100": runs(
            "success", "skipped"
        ),
    }
    verdict, _ = verify(
        routes, f"PR #6821 head {HEAD[:7]} has checks passing at {HEAD}"
    )
    assert verdict.status == "success", verdict
    assert any("head is de02262a35e2" in line for line in verdict.evidence)
    assert any(
        "checks at de02262a35e2 are success" in line for line in verdict.evidence
    )


def test_checks_on_a_pr_resolve_its_head_and_a_moved_head_fails():
    other = "a" * 39 + "1"
    routes = {
        f"/repos/{REPO}/issues/7": issue(7, pull=True),
        f"/repos/{REPO}/pulls/7": pull(7, sha=other),
        f"/repos/{REPO}/commits/{other}/check-runs?per_page=100": runs(
            "failure", sha=other
        ),
        status_path(other): combined(sha=other),
    }
    assert verify(routes, "PR #7 checks are failing")[0].status == "success"
    assert verify(routes, "PR #7 checks are passing")[0].status == "failed"
    moved, _ = verify(routes, f"PR #7 head {HEAD[:7]} checks are green")
    assert moved.status == "failed" and "head is aaaaaaaaaaaa" in moved.reason


def test_pending_incomplete_and_empty_check_runs():
    sha = HEAD[:9]
    path = f"/repos/{REPO}/commits/{sha}/check-runs?per_page=100"
    none = {status_path(sha): combined()}

    def check(run_body, title="passing", extra=none):
        return verify({path: run_body, **extra}, f"Checks at {sha} are {title}")[0]

    pending = runs("success", None, status="in_progress")
    assert check(pending, "pending").status == "success"
    assert check(pending).status == "failed"
    assert check(runs()).status == "failed"
    over = {"total_count": 101, "check_runs": [{"head_sha": HEAD}] * 100}
    assert check(over).status == "unsupported"
    assert check(runs("success", sha="f" * 40)).status == "unavailable"


def test_a_commit_status_is_folded_into_checks():
    """pr-checks is a commit status: check runs alone cannot establish success."""
    sha = HEAD
    path = f"/repos/{REPO}/commits/{sha}/check-runs?per_page=100"

    def check(run_body, state, title):
        routes = {path: run_body, status_path(sha): combined(state)}
        return verify(routes, f"Checks at {sha} are {title}")[0]

    # Passing runs do not make a pending or failed required status pass.
    assert check(runs("success"), "pending", "passing").status == "failed"
    assert check(runs("success"), "failure", "passing").status == "failed"
    assert check(runs("success"), "error", "passing").status == "failed"
    assert check(runs("success"), "success", "passing").status == "success"
    # Failure outranks pending, whichever source reports it.
    assert check(runs("success"), "failure", "failing").status == "success"
    assert check(runs("failure"), "pending", "failing").status == "success"
    assert check(runs("failure"), "pending", "pending").status == "failed"
    assert check(runs("success", None, status="queued"), "failure", "pending").status == (
        "failed"
    )
    # A status-only commit (no check runs) is judged on the status alone.
    assert check(runs(), "success", "passing").status == "success"
    assert check(runs(), "pending", "pending").status == "success"
    # Neither source has anything: there is nothing to verify as passing.
    assert check(runs(), None, "passing").status == "failed"


def test_commit_status_must_be_tied_to_the_sha_and_fully_returned():
    sha = HEAD
    path = f"/repos/{REPO}/commits/{sha}/check-runs?per_page=100"
    title = f"Checks at {sha} are passing"
    untied = combined("success", sha="f" * 40)
    assert verify({path: runs("success"), status_path(sha): untied}, title)[0].status == (
        "unavailable"
    )
    truncated = {**combined("success"), "total_count": 101}
    assert verify({path: runs("success"), status_path(sha): truncated}, title)[
        0
    ].status == "unsupported"
    odd = {**combined("success"), "statuses": [{"state": "weird"}]}
    assert verify({path: runs("success"), status_path(sha): odd}, title)[0].status == (
        "unavailable"
    )
    down = {path: runs("success"), status_path(sha): GitHubResponse(502, {})}
    assert verify(down, title)[0].status == "unavailable"


@pytest.mark.parametrize(
    "response",
    [
        GitHubResponse(500, {}),
        GitHubResponse(403, {"message": "rate limit"}),
        GitHubResponse(200, ["not", "an", "object"]),
        GitHubResponse(200, {"number": 1, "state": "weird"}),
        SourceUnavailable("ConnectError"),
    ],
)
def test_unavailable_source_is_not_a_verdict_on_the_claim(response):
    verdict, _ = verify({f"/repos/{REPO}/issues/1": response}, "Issue #1 is open")
    assert verdict.status == "unavailable"
    assert verdict.evidence == [] and verdict.observed_at is None


@pytest.mark.parametrize(
    ("title", "content", "reason"),
    [
        ("Operational acceptance for #6812 remains outstanding", None, "acceptance gate"),
        ("Issue #1 is blocked on #2", None, "blocked"),
        ("PR #1 is ready for review", None, "ready"),
        ("PR #1 is approved", None, "approved"),
        ("Run 1234567 failed", None, "workflow run or job"),
        ("Job 99999 is running", None, "workflow run or job"),
        ("PR #1 is not merged", None, "negated"),
        ("Main is at abcdef1 and open", None, "names no PR"),
        ("PR #1 is green", None, "green"),
        ("The head is de02262a35e2 and open", None, "head claim"),
        ("PR #1 head is open", None, "head claim"),
        ("Issue #1 is open, see abcdef1", None, "SHA assertion"),
        # A state or gate in its own sentence is never partly verified: the
        # renewal would extend the whole note.
        (
            "PR #6806 checks are passing at a24da077",
            "Operational acceptance is still outstanding.",
            "acceptance gate",
        ),
        (
            "PR #6806 is open",
            "Its checks are failing.",
            "names no concrete instance",
        ),
        (
            "PR #6806 is open",
            "It is blocked on the live validation gate.",
            "acceptance gate",
        ),
        ("PR #6806 is open", "Live validation remaining.", "acceptance gate"),
        ("PR #6806 is open", "Still awaiting review.", "acceptance gate"),
        ("Issue #1 is open", "The rollout is complete.", "names no concrete instance"),
    ],
)
def test_unsupported_and_free_text_gates_stay_due_with_a_reason(title, content, reason):
    verdict, fake = verify({}, title, content)
    assert verdict.status == "unsupported"
    assert reason in verdict.reason
    assert fake.calls == []


def test_other_repository_and_too_many_references_are_unsupported():
    assert (
        verify({}, "other/repo#5 is open")[0].reason == "repository is not verifiable"
    )
    many = " ".join(f"#{n} is open." for n in range(1, 8))
    verdict, _ = verify({}, many)
    assert verdict.reason == "too many references to verify"


def test_issue_number_that_is_not_a_pull_request_cannot_be_merged():
    routes = {f"/repos/{REPO}/issues/9": issue(9)}
    verdict, _ = verify(routes, "Issue #9 is merged")
    assert verdict.status == "unsupported"


def test_evidence_and_provenance_sections_assert_nothing():
    content = "Durable claim, PR #1 is open.\n\n## Evidence\n- PR #2 is merged\n"
    predicates = extract_predicates(title="t", content=content, default_repo=REPO)
    assert [p.number for p in predicates] == [1]


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("PR #6821 and PR #6822 are open", "reference #6821 is not covered"),
        (f"PR #1 and PR #2 head is {HEAD}", "reference #1 is not covered"),
        ("PR #1 and PR #2 checks are passing", "reference #1 is not covered"),
        (f"#1 and #2 checks are passing at {HEAD}", "more than one reference"),
        (f"#1 and #2 checks pass at {HEAD}", "no verifiable predicate"),
        (
            f"PR #1 is open and PR #2 checks are passing at {HEAD}",
            "more than one reference",
        ),
        ("other/repo#1 and PR #1 are open", "reference #1 is not covered"),
    ],
)
def test_uncovered_or_ambiguous_references_are_unsupported(title, reason):
    verdict, fake = verify({}, title)
    assert verdict.status == "unsupported"
    assert reason in verdict.reason
    assert fake.calls == []


def test_each_reference_with_its_own_state_is_covered():
    predicates = extract_predicates(
        title="PR #1 is open and PR #2 is closed", content=None, default_repo=REPO
    )
    assert predicates == [
        Predicate("state", REPO, 1, "open"),
        Predicate("state", REPO, 2, "closed"),
    ]


def test_one_reference_at_a_sha_and_repeated_identical_references_stay_supported():
    predicates = extract_predicates(
        title="PR #6806 checks are passing at a24da077",
        content=None,
        default_repo=REPO,
    )
    assert predicates == [Predicate("checks", REPO, None, "success", sha="a24da077")]
    for title, expected in [
        ("PR #1 and PR #1 are open", Predicate("state", REPO, 1, "open")),
        (f"PR #1 and PR #1 head is {HEAD}", Predicate("head", REPO, 1, "head", sha=HEAD)),
        ("PR #1 and PR #1 checks are passing", Predicate("checks", REPO, 1, "success")),
        (
            f"PR #1 and PR #1 checks are passing at {HEAD}",
            Predicate("checks", REPO, None, "success", sha=HEAD),
        ),
    ]:
        assert extract_predicates(title=title, content=None, default_repo=REPO) == [expected]


def test_shared_responses_spend_one_request_and_budget_stops_the_run():
    routes = {f"/repos/{REPO}/issues/1": issue(1), f"/repos/{REPO}/issues/2": issue(2)}
    fake = Fake(routes)
    verifier = GitHubVerifier(fake, repo=REPO, clock=Clock(), max_requests=1)
    assert verifier.verify(title="Issue #1 is open", content=None).status == "success"
    assert verifier.verify(title="#1 is open", content=None).status == "success"
    with pytest.raises(BudgetExhausted):
        verifier.verify(title="Issue #2 is open", content=None)
    assert fake.calls == [f"/repos/{REPO}/issues/1"]
    assert verifier.requests == 1


def test_evidence_time_is_the_oldest_response_used():
    clock = Clock()
    fake = Fake(
        {f"/repos/{REPO}/issues/1": issue(1), f"/repos/{REPO}/issues/2": issue(2)}
    )
    verifier = GitHubVerifier(fake, repo=REPO, clock=clock)
    first = verifier.verify(title="Issue #1 is open", content=None)
    second = verifier.verify(
        title="Issue #1 is open and issue #2 is open", content=None
    )
    assert second.observed_at == first.observed_at


def test_httpx_fetcher_maps_transport_errors_and_bodies():
    import httpx

    def handler(request):
        assert request.headers["authorization"] == "Bearer tok"
        if request.url.path.endswith("/boom"):
            raise httpx.ConnectError("down")
        if request.url.path.endswith("/text"):
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetch = httpx_fetcher(client, token="tok")
        assert fetch("/x") == GitHubResponse(200, {"ok": True})
        assert fetch("/text") == GitHubResponse(200, None)
        with pytest.raises(SourceUnavailable):
            fetch("/boom")
