"""Unit tests for the CLI HTTP layer: client(), timeout, and non-404 errors.

These tests do NOT use the FastAPI integration stack — they exercise the CLI
layer in isolation using mocked httpx responses. This keeps them fast and
free of monolith dependencies.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from tools.cli import api
from tools.cli.main import app


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# api.client() helper
# ---------------------------------------------------------------------------


class TestClientHelper:
    def test_uses_cf_token_as_cookie(self):
        """client() injects the CF token as the CF_Authorization cookie."""
        with patch("tools.cli.api.get_cf_token", return_value="test-token"):
            client = api.client()
            try:
                assert client.cookies.get("CF_Authorization") == "test-token"
            finally:
                client.close()

    def test_sets_correct_base_url(self):
        """client() sets the base URL to the private homelab domain."""
        with patch("tools.cli.api.get_cf_token", return_value="test-token"):
            client = api.client()
            try:
                assert "private.jomcgi.dev" in str(client.base_url)
            finally:
                client.close()

    def test_sets_30_second_timeout(self):
        """client() configures a 30-second read timeout."""
        with patch("tools.cli.api.get_cf_token", return_value="test-token"):
            client = api.client()
            try:
                assert client.timeout.read == 30.0
            finally:
                client.close()

    def test_different_tokens_produce_different_cookies(self):
        """Each unique token returned by get_cf_token() appears in the cookie jar."""
        with patch("tools.cli.api.get_cf_token", return_value="unique-token-xyz"):
            client = api.client()
            try:
                assert client.cookies.get("CF_Authorization") == "unique-token-xyz"
            finally:
                client.close()

    def test_follow_redirects_is_false(self):
        """client() disables automatic redirect following so 3xx responses are surfaced."""
        with patch("tools.cli.api.get_cf_token", return_value="test-token"):
            client = api.client()
            try:
                assert client.follow_redirects is False
            finally:
                client.close()


# ---------------------------------------------------------------------------
# Helpers: mock api.client() as a context manager
#
# api.request() uses:  `with client() as http:`
# so patching client with a plain function that returns a @contextmanager
# correctly intercepts both the call and the `with` block.
# ---------------------------------------------------------------------------


def _make_timeout_client() -> object:
    """Return a client() replacement that raises TimeoutException on every request."""
    mock_client = MagicMock()
    mock_client.get.side_effect = httpx.TimeoutException("timed out")
    mock_client.post.side_effect = httpx.TimeoutException("timed out")

    @contextmanager
    def _ctx():
        yield mock_client

    def _factory():
        return _ctx()

    return _factory


def _make_error_client(status_code: int) -> object:
    """Return a client() replacement that returns an HTTP error response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=MagicMock(),
        response=mock_resp,
    )
    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp
    mock_client.post.return_value = mock_resp

    @contextmanager
    def _ctx():
        yield mock_client

    def _factory():
        return _ctx()

    return _factory


# ---------------------------------------------------------------------------
# Timeout propagation
# ---------------------------------------------------------------------------


class TestTimeoutHandling:
    def test_search_timeout_exits_nonzero(self, runner):
        """Network timeout during `knowledge search` results in a non-zero exit."""
        with patch("tools.cli.api.client", _make_timeout_client()):
            result = runner.invoke(app, ["knowledge", "search", "query"])
        assert result.exit_code != 0

    def test_note_timeout_exits_nonzero(self, runner):
        """Network timeout during `knowledge note` results in a non-zero exit."""
        with patch("tools.cli.api.client", _make_timeout_client()):
            result = runner.invoke(app, ["knowledge", "note", "n1"])
        assert result.exit_code != 0

    def test_dead_letters_timeout_exits_nonzero(self, runner):
        """Network timeout during `knowledge dead-letters` results in a non-zero exit."""
        with patch("tools.cli.api.client", _make_timeout_client()):
            result = runner.invoke(app, ["knowledge", "dead-letters"])
        assert result.exit_code != 0

    def test_replay_timeout_exits_nonzero(self, runner):
        """Network timeout during `knowledge replay` results in a non-zero exit."""
        with patch("tools.cli.api.client", _make_timeout_client()):
            result = runner.invoke(app, ["knowledge", "replay", "1"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# replay() non-404 error handling
#
# The replay() command handles 404 explicitly (prints message, Exit(1)).
# All other HTTP error codes must propagate via raise_for_status() → non-zero exit.
# ---------------------------------------------------------------------------


class TestReplayNon404ErrorHandling:
    def test_replay_500_exits_nonzero(self, runner):
        """500 Internal Server Error from replay propagates as a non-zero exit."""
        with patch("tools.cli.api.client", _make_error_client(500)):
            result = runner.invoke(app, ["knowledge", "replay", "1"])
        assert result.exit_code != 0

    def test_replay_403_exits_nonzero(self, runner):
        """403 Forbidden from replay propagates as a non-zero exit."""
        with patch("tools.cli.api.client", _make_error_client(403)):
            result = runner.invoke(app, ["knowledge", "replay", "1"])
        assert result.exit_code != 0

    def test_replay_503_exits_nonzero(self, runner):
        """503 Service Unavailable from replay propagates as a non-zero exit."""
        with patch("tools.cli.api.client", _make_error_client(503)):
            result = runner.invoke(app, ["knowledge", "replay", "1"])
        assert result.exit_code != 0

    def test_replay_404_prints_friendly_message(self, runner):
        """404 from replay prints a human-readable 'not found' message to stderr."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        # raise_for_status should NOT be called for 404 — the code checks status directly.
        mock_resp.raise_for_status.side_effect = AssertionError(
            "raise_for_status should not be called for 404"
        )
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp

        @contextmanager
        def _ctx():
            yield mock_client

        def _factory():
            return _ctx()

        with patch("tools.cli.api.client", _factory):
            result = runner.invoke(app, ["knowledge", "replay", "42"])

        assert result.exit_code == 1
        assert "42" in result.output
        assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# api.request() wrapper
#
# request() calls client() as a context manager.  On a non-redirect response
# it returns immediately.  On a 3xx (is_redirect=True) it calls clear_cf_token()
# and retries with a fresh client.  All kwargs are forwarded to the underlying
# httpx method.
# ---------------------------------------------------------------------------


def _make_single_response_client(is_redirect: bool = False) -> object:
    """Return a client() factory whose HTTP methods return one fixed response."""
    mock_resp = MagicMock()
    mock_resp.is_redirect = is_redirect
    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp
    mock_client.post.return_value = mock_resp

    @contextmanager
    def _ctx():
        yield mock_client

    def _factory():
        return _ctx()

    return _factory, mock_client, mock_resp


def _make_redirect_then_ok_client():
    """Return a client() factory: first call returns 3xx, second returns 200."""
    responses = [
        MagicMock(is_redirect=True),
        MagicMock(is_redirect=False),
    ]
    call_idx = [0]

    def _factory():
        resp = responses[call_idx[0]]
        call_idx[0] = min(call_idx[0] + 1, len(responses) - 1)
        mock_client = MagicMock()
        mock_client.get.return_value = resp
        mock_client.post.return_value = resp

        @contextmanager
        def _ctx():
            yield mock_client

        return _ctx()

    return _factory, responses


class TestRequest:
    """Tests for request(): the HTTP wrapper with auto re-auth on 3xx."""

    def test_non_redirect_response_returned_as_is(self):
        """A 2xx response is returned directly without calling clear_cf_token."""
        factory, mock_client, mock_resp = _make_single_response_client(
            is_redirect=False
        )
        with (
            patch("tools.cli.api.client", factory),
            patch("tools.cli.api.clear_cf_token") as mock_clear,
        ):
            result = api.request("get", "/api/knowledge/search")

        assert result is mock_resp
        mock_clear.assert_not_called()

    def test_redirect_triggers_clear_cf_token(self):
        """A 3xx response causes clear_cf_token() to be called before retrying."""
        factory, responses = _make_redirect_then_ok_client()
        with (
            patch("tools.cli.api.client", factory),
            patch("tools.cli.api.clear_cf_token") as mock_clear,
        ):
            api.request("get", "/api/knowledge/search")

        mock_clear.assert_called_once()

    def test_redirect_returns_retry_response(self):
        """After a 3xx, the response from the retry request is returned."""
        factory, responses = _make_redirect_then_ok_client()
        with (
            patch("tools.cli.api.client", factory),
            patch("tools.cli.api.clear_cf_token"),
        ):
            result = api.request("get", "/api/knowledge/search")

        # The second (non-redirect) response should be returned
        assert result is responses[1]

    def test_kwargs_forwarded_to_client_method(self):
        """Extra kwargs (e.g. params) are passed through to the underlying httpx method."""
        factory, mock_client, mock_resp = _make_single_response_client(
            is_redirect=False
        )
        params = {"q": "test query", "limit": 5}
        with (
            patch("tools.cli.api.client", factory),
            patch("tools.cli.api.clear_cf_token"),
        ):
            api.request("get", "/api/knowledge/search", params=params)

        mock_client.get.assert_called_once_with("/api/knowledge/search", params=params)

    def test_post_kwargs_forwarded_to_client_method(self):
        """kwargs are forwarded correctly for POST requests too."""
        factory, mock_client, mock_resp = _make_single_response_client(
            is_redirect=False
        )
        json_body = {"key": "value"}
        with (
            patch("tools.cli.api.client", factory),
            patch("tools.cli.api.clear_cf_token"),
        ):
            api.request("post", "/api/knowledge/dead-letter/1/replay", json=json_body)

        mock_client.post.assert_called_once_with(
            "/api/knowledge/dead-letter/1/replay", json=json_body
        )
