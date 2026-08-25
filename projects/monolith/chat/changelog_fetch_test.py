"""Direct unit tests for _fetch_commits_since in chat.changelog."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from chat.changelog import _fetch_commits_since


def _make_client(response_json=None, raise_for_status=None):
    """Build a minimal async httpx.AsyncClient mock."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = response_json or []
    if raise_for_status is not None:
        mock_resp.raise_for_status.side_effect = raise_for_status
    else:
        mock_resp.raise_for_status = MagicMock()

    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=mock_resp)
    return client, mock_resp


class TestFetchCommitsSince:
    @pytest.mark.asyncio
    async def test_returns_parsed_json(self):
        """_fetch_commits_since returns the list returned by resp.json()."""
        commits = [{"sha": "abc123", "commit": {"message": "feat: new thing"}}]
        client, _ = _make_client(response_json=commits)

        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = await _fetch_commits_since(client, "owner/repo", "ghp_tok", since)

        assert result == commits

    @pytest.mark.asyncio
    async def test_calls_github_api_url(self):
        """_fetch_commits_since requests the GitHub /repos/{repo}/commits endpoint."""
        client, _ = _make_client()
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)

        await _fetch_commits_since(client, "myorg/myrepo", "tok", since)

        call_url = client.get.call_args[0][0]
        assert "myorg/myrepo" in call_url
        assert "/commits" in call_url

    @pytest.mark.asyncio
    async def test_sends_sha_main_param(self):
        """_fetch_commits_since passes sha=main in query params."""
        client, _ = _make_client()
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)

        await _fetch_commits_since(client, "owner/repo", "tok", since)

        params = client.get.call_args[1]["params"]
        assert params["sha"] == "main"

    @pytest.mark.asyncio
    async def test_sends_since_param(self):
        """_fetch_commits_since passes since as an ISO-formatted string."""
        client, _ = _make_client()
        since = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)

        await _fetch_commits_since(client, "owner/repo", "tok", since)

        params = client.get.call_args[1]["params"]
        assert params["since"] == since.isoformat()

    @pytest.mark.asyncio
    async def test_sends_authorization_header(self):
        """_fetch_commits_since includes a Bearer/token Authorization header."""
        client, _ = _make_client()
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)

        await _fetch_commits_since(client, "owner/repo", "ghp_secret", since)

        headers = client.get.call_args[1]["headers"]
        assert "Authorization" in headers
        assert "ghp_secret" in headers["Authorization"]

    @pytest.mark.asyncio
    async def test_calls_raise_for_status(self):
        """_fetch_commits_since calls raise_for_status on the response."""
        client, mock_resp = _make_client()
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)

        await _fetch_commits_since(client, "owner/repo", "tok", since)

        mock_resp.raise_for_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_raises_on_http_error(self):
        """_fetch_commits_since propagates HTTPStatusError from raise_for_status."""
        http_err = httpx.HTTPStatusError(
            "404",
            request=MagicMock(),
            response=MagicMock(status_code=404),
        )
        client, _ = _make_client(raise_for_status=http_err)
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)

        with pytest.raises(httpx.HTTPStatusError):
            await _fetch_commits_since(client, "owner/repo", "tok", since)

    @pytest.mark.asyncio
    async def test_empty_list_returned_when_no_commits(self):
        """_fetch_commits_since returns an empty list when the API reports no commits."""
        client, _ = _make_client(response_json=[])
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)

        result = await _fetch_commits_since(client, "owner/repo", "tok", since)

        assert result == []

    @pytest.mark.asyncio
    async def test_sends_per_page_100(self):
        """_fetch_commits_since requests up to 100 commits per page."""
        client, _ = _make_client()
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)

        await _fetch_commits_since(client, "owner/repo", "tok", since)

        params = client.get.call_args[1]["params"]
        assert params["per_page"] == 100


class TestRedirectFollowing:
    """The repo moved orgs, so GitHub answers the old slug with a 301.

    These use a real httpx.AsyncClient over MockTransport rather than the
    AsyncMock above. A mocked client cannot exercise redirect handling at all,
    which is why the org move broke the hourly changelog job in production
    while this suite stayed green.
    """

    @staticmethod
    def _transport(commits):
        def handle(request: httpx.Request) -> httpx.Response:
            if "/repos/jomcgi/homelab/" in str(request.url):
                return httpx.Response(
                    301,
                    headers={
                        "Location": "https://api.github.com/repos/jomcgi-org/homelab/commits"
                    },
                    json={"message": "Moved Permanently"},
                )
            return httpx.Response(200, json=commits)

        return httpx.MockTransport(handle)

    @pytest.mark.asyncio
    async def test_follows_the_org_move_redirect(self):
        """With follow_redirects the 301 resolves to the real commit list."""
        commits = [{"sha": "abc123", "commit": {"message": "feat: new thing"}}]
        async with httpx.AsyncClient(
            transport=self._transport(commits), follow_redirects=True
        ) as client:
            result = await _fetch_commits_since(
                client, "jomcgi/homelab", "token", datetime.now(timezone.utc)
            )
        assert result == commits

    @pytest.mark.asyncio
    async def test_without_following_the_redirect_raises(self):
        """Pin the production failure mode.

        raise_for_status() returns early only on is_success, so a 301 raises
        HTTPStatusError rather than passing a redirect body downstream. That
        exception is what exited the hourly job with code 1.
        """
        async with httpx.AsyncClient(
            transport=self._transport([]), follow_redirects=False
        ) as client:
            with pytest.raises(httpx.HTTPStatusError) as excinfo:
                await _fetch_commits_since(
                    client, "jomcgi/homelab", "token", datetime.now(timezone.utc)
                )
        assert excinfo.value.response.status_code == 301
