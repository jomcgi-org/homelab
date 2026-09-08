"""Tests for shared GitHub repository configuration and client."""

import importlib
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from core import github
from core.github import fetch_merged_pull_requests


def _read_github_repo() -> str:
    from core import github

    return importlib.reload(github).GITHUB_REPO


def test_github_repo_defaults_to_current_slug(monkeypatch):
    monkeypatch.delenv("GITHUB_REPO", raising=False)

    assert _read_github_repo() == "jomcgi-org/homelab"


def test_github_repo_environment_override(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "example/alternate-repo")

    assert _read_github_repo() == "example/alternate-repo"


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
