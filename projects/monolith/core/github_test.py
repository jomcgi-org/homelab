"""Tests for shared GitHub repository configuration and client."""

import importlib
import json
from datetime import datetime, timezone

import httpx

from core.github import fetch_merged_pull_requests


def _read_github_repo() -> str:
    import core.github as github

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
        variables = json.loads(request.content)["variables"]
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
