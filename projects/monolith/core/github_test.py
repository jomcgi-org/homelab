"""Tests for shared GitHub repository configuration and client."""

import asyncio
import importlib
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from core import github
from core.github import fetch_issue_states, fetch_merged_pull_requests


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def get_once(request, monkeypatch):
    """Run each transport contract against both real httpx client variants."""
    monkeypatch.setenv("GITHUB_API_TOKEN", "fake-shared-get")
    asynchronous = request.param
    client_name = "AsyncClient" if asynchronous else "Client"
    real_client = getattr(httpx, client_name)

    def install(handler):
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(github.httpx, client_name, client)

        async def get(url, *, follow_redirects=False):
            options = dict(
                timeout=7.5,
                follow_redirects=follow_redirects,
                user_agent="monolith-get-contract-test",
            )
            if asynchronous:
                return await github.async_github_get(url, **options)
            return github.github_get(url, **options)

        return get

    return install


@pytest.mark.asyncio
async def test_get_headers_timeout_and_fresh_token(get_once, monkeypatch):
    requests = []
    responses = []
    url = "https://api.github.com/repos/example/repo/branches?per_page=100"

    def handler(request):
        requests.append(request)
        response = httpx.Response(200, json={"ok": True})
        responses.append(response)
        return response

    get = get_once(handler)
    for token in (None, "fake-first", "fake-changed", None):
        if token is None:
            monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)
        else:
            monkeypatch.setenv("GITHUB_API_TOKEN", token)
        response = await get(url)
        assert response is responses[-1]
        request = requests[-1]
        assert request.method == "GET"
        assert str(request.url) == url
        assert request.headers["Accept"] == "application/vnd.github+json"
        # Deliberate normalization beyond the old local adapters' headers.
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert request.headers["User-Agent"] == "monolith-get-contract-test"
        assert request.headers.get("Authorization") == (
            f"Bearer {token}" if token else None
        )
        assert request.extensions["timeout"] == {
            key: 7.5 for key in ("connect", "read", "write", "pool")
        }
    assert len(requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("follow_redirects", [False, True])
async def test_get_repository_301_policy(get_once, follow_redirects):
    requests = []
    old_url = "https://api.github.com/repos/old-owner/repo"
    new_url = "https://api.github.com/repositories/12345"

    def handler(request):
        requests.append(str(request.url))
        if str(request.url) == old_url:
            return httpx.Response(301, headers={"Location": new_url})
        return httpx.Response(200, json={"full_name": "new-owner/repo"})

    response = await get_once(handler)(old_url, follow_redirects=follow_redirects)
    assert requests == ([old_url, new_url] if follow_redirects else [old_url])
    assert response.status_code == (200 if follow_redirects else 301)
    assert len(response.history) == int(follow_redirects)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 429, 502, 503, 504])
async def test_get_returns_raw_failure_without_retry_or_raising(get_once, status):
    calls = []
    original = httpx.Response(status, text="upstream failure")

    def handler(request):
        calls.append(request)
        return original

    response = await get_once(handler)("https://api.github.com/repos/example/repo")
    assert response is original
    assert response.status_code == status
    assert response.text == "upstream failure"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
async def test_get_propagates_original_transport_exception(get_once, error_type):
    calls = []
    original = error_type("synthetic transport failure")

    def handler(request):
        calls.append(request)
        raise original

    with pytest.raises(error_type) as caught:
        await get_once(handler)("https://api.github.com/repos/example/repo")
    assert caught.value is original
    assert len(calls) == 1


def _read_github_repo() -> str:
    from core import github

    return importlib.reload(github).GITHUB_REPO


def test_github_repo_defaults_to_current_slug(monkeypatch):
    monkeypatch.delenv("GITHUB_REPO", raising=False)

    assert _read_github_repo() == "jomcgi-org/homelab"


def test_github_repo_environment_override(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "example/alternate-repo")

    assert _read_github_repo() == "example/alternate-repo"


@pytest.mark.parametrize(
    ("token", "expected"),
    [(None, "Bearer env-token"), ("explicit", "Bearer explicit"), ("", None)],
)
def test_github_headers_token_precedence(monkeypatch, token, expected):
    monkeypatch.setenv("GITHUB_API_TOKEN", "env-token")

    headers = github.github_headers("agent", token)

    assert headers["User-Agent"] == "agent"
    assert headers.get("Authorization") == expected


def test_fetch_issue_states_merged_closing_refs_and_unresolved_alias(monkeypatch):
    monkeypatch.setenv("GITHUB_API_TOKEN", "test-token")

    def handler(request):
        body = json.loads(request.content)
        assert body["variables"] == {"owner": "example", "name": "repo"}
        assert "issue_5927: issue(number: 5927)" in body["query"]
        assert (
            "closedByPullRequestsReferences(first: 20, includeClosedPrs: true)"
            in body["query"]
        )
        assert "issue_0" not in body["query"]
        assert "issue_-1" not in body["query"]
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "issue_5927": {
                            "number": 5927,
                            "state": "CLOSED",
                            "closedAt": "2026-09-28T12:00:00Z",
                            "closedByPullRequestsReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [
                                    {
                                        "number": 6412,
                                        "merged": True,
                                        "mergedAt": "2026-09-28T11:00:00Z",
                                    },
                                    {
                                        "number": 6543,
                                        "merged": True,
                                        "mergedAt": "2026-09-28T12:00:00Z",
                                    },
                                    {"number": 9998, "merged": False, "mergedAt": None},
                                ],
                            },
                        },
                        "issue_5784": {
                            "number": 5784,
                            "state": "OPEN",
                            "closedAt": None,
                            "closedByPullRequestsReferences": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": False},
                            },
                        },
                        "issue_9999": None,
                    }
                },
                "errors": [
                    {
                        "type": "NOT_FOUND",
                        "path": ["repository", "issue_9999"],
                        "message": "Could not resolve to an Issue",
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = fetch_issue_states(
            [5927, 5784, 9999, 0, -1], repo="example/repo", client=client
        )
    assert [row["number"] for row in rows] == [5784, 5927]
    assert rows[0]["state"] == "OPEN"
    assert rows[0]["closed_at"] is None
    assert rows[0]["closing_prs"] == []
    assert rows[1]["closing_prs"] == [6412, 6543]
    assert rows[1]["last_closing_merge_at"] == datetime(
        2026, 9, 28, 12, tzinfo=timezone.utc
    )
    assert rows[1]["closed_at"] == datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "error",
    [
        {"type": "FORBIDDEN", "path": ["repository", "issue_5927"]},
        {"type": "NOT_FOUND", "path": ["repository"]},
    ],
)
def test_fetch_issue_states_does_not_hide_genuine_graphql_failures(error):
    def handler(request):
        return httpx.Response(
            200, json={"data": {"repository": {"issue_5927": None}}, "errors": [error]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="GitHub GraphQL error"):
            fetch_issue_states([5927], repo="example/repo", client=client)


def test_fetch_issue_states_reuses_retry_policy(monkeypatch):
    delays = []
    monkeypatch.setattr(github.time, "sleep", delays.append)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": {"repository": {"issue_5927": None}}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_issue_states([5927], repo="example/repo", client=client) == []
        assert fetch_issue_states([], client=client) == []
        assert fetch_issue_states([0, -1], client=client) == []
    assert len(calls) == 2
    assert delays == [2]


def test_fetch_issue_states_rejects_truncated_closing_refs():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "issue_5927": {
                            "number": 5927,
                            "state": "CLOSED",
                            "closedAt": None,
                            "closedByPullRequestsReferences": {
                                "nodes": [
                                    {
                                        "number": n,
                                        "merged": True,
                                        "mergedAt": "2026-09-28T12:00:00Z",
                                    }
                                    for n in range(1, 21)
                                ],
                                "pageInfo": {"hasNextPage": True},
                            },
                        },
                    }
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            RuntimeError, match="closing refs truncated for issue #5927"
        ):
            fetch_issue_states([5927], repo="example/repo", client=client)


def test_fetch_merged_pull_requests_pages_and_filters_old_merges():
    since = datetime(2026, 6, 9, tzinfo=timezone.utc)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        assert request.method == "POST"
        assert request.url.path == "/graphql"
        request_body = json.loads(request.content)
        assert "first: 40" in request_body["query"]
        assert "first: 100" not in request_body["query"]
        variables = request_body["variables"]
        recent_page = variables["cursor"] is None
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": (
                                [
                                    {
                                        "number": 7,
                                        "title": "feat: snapshot",
                                        "mergedAt": "2026-09-07T12:00:00Z",
                                        "updatedAt": "2026-09-07T12:00:00Z",
                                        "additions": 4,
                                        "deletions": 2,
                                        "changedFiles": 1,
                                        "body": "Codex",
                                    }
                                ]
                                if recent_page
                                else [
                                    {
                                        "number": 6,
                                        "title": "fix: old",
                                        "mergedAt": "2026-01-01T00:00:00Z",
                                        "updatedAt": "2026-01-01T00:00:00Z",
                                        "additions": 1,
                                        "deletions": 1,
                                        "changedFiles": 1,
                                        "body": "",
                                    }
                                ]
                            ),
                            "pageInfo": {
                                "hasNextPage": recent_page,
                                "endCursor": "page-2" if recent_page else None,
                            },
                        }
                    }
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        pulls = fetch_merged_pull_requests(since, repo="example/repo", client=client)

    assert pulls == [
        {
            "number": 7,
            "title": "feat: snapshot",
            "merged_at": "2026-09-07T12:00:00Z",
            "additions": 4,
            "deletions": 2,
            "changed_files": 1,
            "body": "Codex",
        }
    ]
    assert calls == 2


def test_fetch_merged_pull_requests_raises_for_graphql_errors():
    def handler(_request):
        return httpx.Response(200, json={"errors": [{"message": "API error"}]})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(RuntimeError, match="API error"),
    ):
        fetch_merged_pull_requests(
            datetime(2026, 6, 9, tzinfo=timezone.utc),
            repo="example/repo",
            client=client,
        )


@pytest.mark.parametrize("status_code", [502, 503, 504])
def test_fetch_merged_pull_requests_retries_server_errors(
    monkeypatch, caplog, status_code
):
    calls = 0
    sleeps = []
    monkeypatch.setattr(github.time, "sleep", sleeps.append)

    def handler(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status_code, text="unavailable")
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            },
        )

    with (
        caplog.at_level(logging.WARNING, logger="core.github"),
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
    ):
        pulls = fetch_merged_pull_requests(
            datetime(2026, 6, 9, tzinfo=timezone.utc),
            repo="example/repo",
            client=client,
        )

    assert pulls == []
    assert calls == 2
    assert sleeps == [2]
    assert caplog.messages == [
        f"fetch_merged_pull_requests: retrying on {status_code} after attempt 1/4"
    ]


def test_fetch_merged_pull_requests_raises_after_four_server_errors(monkeypatch):
    calls = 0
    sleeps = []
    monkeypatch.setattr(github.time, "sleep", sleeps.append)

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="unavailable")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        fetch_merged_pull_requests(
            datetime(2026, 6, 9, tzinfo=timezone.utc),
            repo="example/repo",
            client=client,
        )

    assert calls == 4
    assert sleeps == [2, 4, 8]


def test_fetch_merged_pull_requests_does_not_retry_client_errors(monkeypatch):
    calls = 0
    sleeps = []
    monkeypatch.setattr(github.time, "sleep", sleeps.append)

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="unauthorized")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        fetch_merged_pull_requests(
            datetime(2026, 6, 9, tzinfo=timezone.utc),
            repo="example/repo",
            client=client,
        )

    assert calls == 1
    assert sleeps == []


def test_fetch_merged_pull_requests_retries_transport_errors(monkeypatch):
    calls = 0
    sleeps = []
    monkeypatch.setattr(github.time, "sleep", sleeps.append)

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection failed", request=request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        pulls = fetch_merged_pull_requests(
            datetime(2026, 6, 9, tzinfo=timezone.utc),
            repo="example/repo",
            client=client,
        )

    assert pulls == []
    assert calls == 2
    assert sleeps == [2]


def test_fetch_merged_pull_requests_raises_when_next_cursor_is_missing():
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": [
                                {
                                    "number": 7,
                                    "title": "feat: snapshot",
                                    "mergedAt": "2026-09-07T12:00:00Z",
                                    "updatedAt": "2026-09-07T12:00:00Z",
                                    "additions": 4,
                                    "deletions": 2,
                                    "changedFiles": 1,
                                    "body": "",
                                }
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": None},
                        }
                    }
                }
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ValueError, match="omitted the next cursor"),
    ):
        fetch_merged_pull_requests(
            datetime(2026, 6, 9, tzinfo=timezone.utc),
            repo="example/repo",
            client=client,
        )


@pytest.mark.parametrize(("token", "expected"), [("secret-token", True), (None, False)])
def test_fetch_merged_pull_requests_sets_authorization_only_with_token(
    monkeypatch, token, expected
):
    if token is None:
        monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GITHUB_API_TOKEN", token)

    def handler(request):
        assert ("authorization" in request.headers) is expected
        if expected:
            assert request.headers["authorization"] == "Bearer secret-token"
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert (
            fetch_merged_pull_requests(
                datetime(2026, 6, 9, tzinfo=timezone.utc),
                repo="example/repo",
                client=client,
            )
            == []
        )


def test_fetch_merged_pull_requests_stops_at_watermark_overlap():
    calls = 0
    watermark = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": [
                                {
                                    "number": 7,
                                    "title": "feat: snapshot",
                                    "mergedAt": "2026-09-05T10:00:00Z",
                                    "updatedAt": "2026-09-05T10:00:00Z",
                                    "additions": 4,
                                    "deletions": 2,
                                    "changedFiles": 1,
                                    "body": "",
                                }
                            ],
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "unneeded-page",
                            },
                        }
                    }
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        pulls = fetch_merged_pull_requests(
            datetime(2026, 6, 9, tzinfo=timezone.utc),
            watermark=watermark,
            repo="example/repo",
            client=client,
        )

    assert [pull["number"] for pull in pulls] == [7]
    assert calls == 1


def test_fetch_merged_pull_requests_warns_at_page_cap(monkeypatch, caplog):
    calls = 0
    monkeypatch.setattr(github, "_MAX_PULL_PAGES", 2)

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": [
                                {
                                    "number": calls,
                                    "title": "feat: snapshot",
                                    "mergedAt": "2026-09-07T12:00:00Z",
                                    "updatedAt": "2026-09-07T12:00:00Z",
                                    "additions": 4,
                                    "deletions": 2,
                                    "changedFiles": 1,
                                    "body": "",
                                }
                            ],
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": f"page-{calls + 1}",
                            },
                        }
                    }
                }
            },
        )

    with (
        caplog.at_level(logging.WARNING, logger="core.github"),
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
    ):
        pulls = github.fetch_merged_pull_requests(
            datetime(2026, 6, 9, tzinfo=timezone.utc),
            repo="example/repo",
            client=client,
        )

    assert len(pulls) == 2
    assert calls == 2
    assert "pagination hit the 2-page cap" in caplog.text


def _issue_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(github.httpx, "AsyncClient", client)


def test_create_issue_posts_exactly_the_given_fields(monkeypatch):
    monkeypatch.setenv("GITHUB_API_TOKEN", "fake-issue-token")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={"number": 42, "html_url": "ignored"})

    _issue_transport(monkeypatch, handler)

    number = github.create_issue(
        "owner/repo", "A title", "A body", ["one", "two"], user_agent="test-agent"
    )

    assert number == 42
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://api.github.com/repos/owner/repo/issues"
    assert json.loads(seen[0].content) == {
        "title": "A title",
        "body": "A body",
        "labels": ["one", "two"],
    }
    assert seen[0].headers["authorization"] == "Bearer fake-issue-token"
    assert seen[0].headers["user-agent"] == "test-agent"
    assert seen[0].extensions["timeout"] == {
        "connect": 2,
        "write": 2,
        "read": 3,
        "pool": 1,
    }
    assert github._ISSUE_WRITE_TIMEOUT_SECONDS == 8


@pytest.mark.parametrize(
    "response,error",
    [
        (httpx.Response(403, json={"message": "no"}), httpx.HTTPStatusError),
        (httpx.Response(301, headers={"location": "/x"}), httpx.HTTPStatusError),
        (httpx.Response(201, json={"number": "42"}), ValueError),
        (httpx.Response(201, json=[]), ValueError),
        (httpx.Response(201, content=b"x" * 1_000_001), ValueError),
    ],
)
def test_create_issue_fails_closed(monkeypatch, response, error):
    calls = []

    def handler(request):
        calls.append(request)
        return response

    _issue_transport(monkeypatch, handler)

    with pytest.raises(error):
        github.create_issue("owner/repo", "t", "b", [], user_agent="test-agent")
    assert len(calls) == 1


def test_create_issue_rejects_a_malformed_repository(monkeypatch):
    _issue_transport(monkeypatch, lambda request: pytest.fail("no request expected"))
    with pytest.raises(ValueError):
        github.create_issue("owner/repo/../x", "t", "b", [], user_agent="test-agent")


def test_create_issue_propagates_read_timeout_without_retry(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("response lost", request=request)

    _issue_transport(monkeypatch, handler)
    with pytest.raises(httpx.ReadTimeout, match="response lost"):
        github.create_issue("owner/repo", "t", "b", [], user_agent="test-agent")
    assert len(calls) == 1


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_create_issue_total_deadline_cancels_io(monkeypatch, phase):
    calls = []
    cancelled = []
    closed = []
    chunks = []

    class DrippingResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                for _ in range(50):
                    chunks.append(b" ")
                    yield b" "
                    await asyncio.sleep(0.005)
                yield b'{"number": 42}'
            finally:
                cancelled.append("body")

        async def aclose(self):
            closed.append(True)

    async def handler(request):
        calls.append(request)
        if phase == "headers":
            try:
                await asyncio.sleep(0.25)
            finally:
                cancelled.append("headers")
        return httpx.Response(201, stream=DrippingResponse())

    _issue_transport(monkeypatch, handler)
    monkeypatch.setattr(github, "_ISSUE_WRITE_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(httpx.ReadTimeout, match="deadline exceeded"):
        github.create_issue("owner/repo", "t", "b", [], user_agent="test-agent")
    assert len(calls) == 1
    assert cancelled == [phase]
    if phase == "body":
        assert chunks
        assert closed == [True]


def test_list_issue_states_maps_one_bounded_request(monkeypatch):
    monkeypatch.setenv("GITHUB_API_TOKEN", "fake-issue-token")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {"number": 1, "state": "open", "state_reason": None},
                {"number": 2, "state": "open", "state_reason": "reopened"},
                {"number": 3, "state": "closed", "state_reason": "completed"},
                {"number": 4, "state": "closed", "state_reason": None},
                {"number": 5, "state": "closed", "state_reason": "not_planned"},
                {"number": 6, "state": "closed", "state_reason": "duplicate"},
                {"number": 7, "state": "closed", "pull_request": {"url": "x"}},
                {"number": 8, "state": "open", "pull_request": {"url": "x"}},
            ],
        )

    _issue_transport(monkeypatch, handler)

    states = github.list_issue_states(
        "owner/repo", ["grimoire", "feedback"], user_agent="test-agent"
    )

    assert states == {
        1: "open",
        2: "open",
        3: "completed",
        4: "completed",
        5: "not_planned",
        6: "not_planned",
    }
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/repos/owner/repo/issues"
    assert dict(seen[0].url.params) == {
        "labels": "grimoire,feedback",
        "state": "all",
        "sort": "updated",
        "direction": "desc",
        "per_page": "100",
    }
    assert seen[0].content == b""
    assert seen[0].headers["authorization"] == "Bearer fake-issue-token"
    assert seen[0].headers["user-agent"] == "test-agent"
    assert seen[0].extensions["timeout"] == {
        "connect": 2,
        "write": 2,
        "read": 3,
        "pool": 1,
    }


def test_list_issue_states_accepts_an_empty_list(monkeypatch):
    _issue_transport(monkeypatch, lambda request: httpx.Response(200, json=[]))
    assert github.list_issue_states("owner/repo", ["a"], user_agent="t") == {}


@pytest.mark.parametrize(
    "response,error",
    [
        (httpx.Response(403, json={"message": "no"}), httpx.HTTPStatusError),
        (httpx.Response(301, headers={"location": "/x"}), httpx.HTTPStatusError),
        (httpx.Response(200, json={"number": 1, "state": "open"}), ValueError),
        (httpx.Response(200, json=["issue"]), ValueError),
        (httpx.Response(200, json=[{"number": "1", "state": "open"}]), ValueError),
        (httpx.Response(200, json=[{"number": 1, "state": "merged"}]), ValueError),
        (httpx.Response(200, content=b"not json"), ValueError),
        (httpx.Response(200, content=b"x" * 1_000_001), ValueError),
    ],
)
def test_list_issue_states_fails_closed(monkeypatch, response, error):
    calls = []

    def handler(request):
        calls.append(request)
        return response

    _issue_transport(monkeypatch, handler)

    with pytest.raises(error):
        github.list_issue_states("owner/repo", ["a"], user_agent="test-agent")
    assert len(calls) == 1


def test_list_issue_states_rejects_a_malformed_repository(monkeypatch):
    _issue_transport(monkeypatch, lambda request: pytest.fail("no request expected"))
    with pytest.raises(ValueError):
        github.list_issue_states("owner/repo/../x", ["a"], user_agent="test-agent")


def test_list_issue_states_total_deadline_cancels_io(monkeypatch):
    calls = []
    cancelled = []

    async def handler(request):
        calls.append(request)
        try:
            await asyncio.sleep(0.25)
        finally:
            cancelled.append(True)
        return httpx.Response(200, json=[])

    _issue_transport(monkeypatch, handler)
    monkeypatch.setattr(github, "_ISSUE_WRITE_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(httpx.ReadTimeout, match="deadline exceeded"):
        github.list_issue_states("owner/repo", ["a"], user_agent="test-agent")
    assert len(calls) == 1
    assert cancelled == [True]
