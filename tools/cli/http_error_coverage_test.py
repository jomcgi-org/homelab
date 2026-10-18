"""Tests for HTTP error response paths and cloudflared subprocess failure.

Covers three gaps identified in the coverage analysis:
1. get_cf_token() when cloudflared binary exists but subprocess.run() fails
2. HTTP 5xx server errors for search, note, and dead_letters commands
3. HTTP 403 forbidden responses for search, note, and dead_letters commands
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from tools.cli.auth import get_cf_token
from tools.cli.main import app


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Helper: build a _client() replacement that returns an HTTP error response
#
# Mirrors the pattern in knowledge_unit_test.py so tests are consistent.
# ---------------------------------------------------------------------------


def _make_error_client(status_code: int):
    """Return a _client replacement that raises HTTPStatusError on raise_for_status."""
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
# get_cf_token: cloudflared in PATH but subprocess.run() fails
# ---------------------------------------------------------------------------


class TestGetCfTokenCloudflaredFails:
    def test_subprocess_called_process_error_propagates(self, tmp_path):
        """CalledProcessError from subprocess.run() propagates to the caller.

        When cloudflared is found but exits non-zero (check=True), subprocess.run()
        raises CalledProcessError. The function must not swallow this exception.
        """
        with (
            patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path),
            patch(
                "tools.cli.auth.shutil.which",
                return_value="/usr/local/bin/cloudflared",
            ),
            patch(
                "tools.cli.auth.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    returncode=1,
                    cmd=[
                        "cloudflared",
                        "access",
                        "login",
                        "https://private.jomcgi.dev",
                    ],
                ),
            ),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                get_cf_token()

    def test_subprocess_os_error_propagates(self, tmp_path):
        """OSError from subprocess.run() propagates to the caller.

        If the binary cannot be executed (e.g. permission denied after which()
        found it), the OSError must surface unmodified.
        """
        with (
            patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path),
            patch(
                "tools.cli.auth.shutil.which",
                return_value="/usr/local/bin/cloudflared",
            ),
            patch(
                "tools.cli.auth.subprocess.run",
                side_effect=OSError("Permission denied"),
            ),
        ):
            with pytest.raises(OSError):
                get_cf_token()

    def test_cloudflared_fails_no_token_written_raises_system_exit(self, tmp_path):
        """If cloudflared runs but writes no token file, SystemExit is raised.

        Simulates a non-zero exit from cloudflared that still returns normally
        (e.g. user cancels the browser flow but the process exits 0). After the
        login attempt, _read_token() returns None and get_cf_token() must raise.
        """
        call_count = 0

        def _silent_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Does NOT write a token file — simulates a cancelled login.

        with (
            patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path),
            patch(
                "tools.cli.auth.shutil.which",
                return_value="/usr/local/bin/cloudflared",
            ),
            patch("tools.cli.auth.subprocess.run", side_effect=_silent_run),
        ):
            with pytest.raises(SystemExit):
                get_cf_token()

        assert call_count == 1


# ---------------------------------------------------------------------------
# HTTP 5xx server errors — search, note, dead_letters
# ---------------------------------------------------------------------------


class TestSearchHttp5xxErrors:
    def test_search_500_exits_nonzero(self, runner):
        """500 Internal Server Error during search results in a non-zero exit."""
        with patch("tools.cli.knowledge_cmd._client", _make_error_client(500)):
            result = runner.invoke(app, ["knowledge", "search", "query"])
        assert result.exit_code != 0

    def test_search_502_exits_nonzero(self, runner):
        """502 Bad Gateway during search results in a non-zero exit."""
        with patch("tools.cli.knowledge_cmd._client", _make_error_client(502)):
            result = runner.invoke(app, ["knowledge", "search", "query"])
        assert result.exit_code != 0

    def test_search_503_exits_nonzero(self, runner):
        """503 Service Unavailable during search results in a non-zero exit."""
        with patch("tools.cli.knowledge_cmd._client", _make_error_client(503)):
            result = runner.invoke(app, ["knowledge", "search", "query"])
        assert result.exit_code != 0


class TestTasksAddErrors:
    def test_uncertain_post_is_not_retried(self, runner):
        requests = []

        def handle(request):
            requests.append(request)
            raise httpx.ReadTimeout("Response lost", request=request)

        with patch(
            "tools.cli.tasks_cmd._client",
            return_value=httpx.Client(
                base_url="https://tasks.test", transport=httpx.MockTransport(handle)
            ),
        ):
            result = runner.invoke(app, ["tasks", "add", "A task"])

        assert result.exit_code != 0
        assert isinstance(result.exception, httpx.ReadTimeout)
        assert result.output == ""
        assert len(requests) == 1

    @pytest.mark.parametrize("prefix", [["tasks"], ["knowledge", "tasks"]])
    @pytest.mark.parametrize("status_code", [403, 422, 500, 503])
    def test_failure_has_no_id_or_success_output(self, runner, prefix, status_code):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                status_code,
                json={"detail": "failed", "note_id": "must-not-print"},
            )

        with patch(
            "tools.cli.tasks_cmd._client",
            return_value=httpx.Client(
                base_url="https://tasks.test", transport=httpx.MockTransport(handle)
            ),
        ):
            result = runner.invoke(app, [*prefix, "add", "A task"])

        assert result.exit_code != 0
        assert isinstance(result.exception, httpx.HTTPStatusError)
        assert result.output == ""
        assert len(requests) == 1
        assert requests[0].method == "POST"
        assert requests[0].url.path == "/api/knowledge/tasks"


class TestNoteHttp5xxErrors:
    def test_note_500_exits_nonzero(self, runner):
        """500 Internal Server Error during note fetch results in a non-zero exit."""
        with patch("tools.cli.knowledge_cmd._client", _make_error_client(500)):
            result = runner.invoke(app, ["knowledge", "note", "n1"])
        assert result.exit_code != 0

    def test_note_502_exits_nonzero(self, runner):
        """502 Bad Gateway during note fetch results in a non-zero exit."""
        with patch("tools.cli.knowledge_cmd._client", _make_error_client(502)):
            result = runner.invoke(app, ["knowledge", "note", "n1"])
        assert result.exit_code != 0

    def test_note_503_exits_nonzero(self, runner):
        """503 Service Unavailable during note fetch results in a non-zero exit."""
        with patch("tools.cli.knowledge_cmd._client", _make_error_client(503)):
            result = runner.invoke(app, ["knowledge", "note", "n1"])
        assert result.exit_code != 0


class TestDeadLettersHttp5xxErrors:
    def test_dead_letters_500_exits_nonzero(self, runner):
        """500 Internal Server Error during dead-letters results in a non-zero exit."""
        with patch("tools.cli.knowledge_cmd._client", _make_error_client(500)):
            result = runner.invoke(app, ["knowledge", "dead-letters"])
        assert result.exit_code != 0

    def test_dead_letters_502_exits_nonzero(self, runner):
        """502 Bad Gateway during dead-letters results in a non-zero exit."""
        with patch("tools.cli.knowledge_cmd._client", _make_error_client(502)):
            result = runner.invoke(app, ["knowledge", "dead-letters"])
        assert result.exit_code != 0

    def test_dead_letters_503_exits_nonzero(self, runner):
        """503 Service Unavailable during dead-letters results in a non-zero exit."""
        with patch("tools.cli.knowledge_cmd._client", _make_error_client(503)):
            result = runner.invoke(app, ["knowledge", "dead-letters"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# HTTP 403 forbidden — search, note, dead_letters
# ---------------------------------------------------------------------------


class TestSearchHttp403:
    def test_search_403_exits_nonzero(self, runner):
        """403 Forbidden during search results in a non-zero exit."""
        with patch("tools.cli.knowledge_cmd._client", _make_error_client(403)):
            result = runner.invoke(app, ["knowledge", "search", "query"])
        assert result.exit_code != 0


class TestNoteHttp403:
    def test_note_403_exits_nonzero(self, runner):
        """403 Forbidden during note fetch results in a non-zero exit."""
        with patch("tools.cli.knowledge_cmd._client", _make_error_client(403)):
            result = runner.invoke(app, ["knowledge", "note", "n1"])
        assert result.exit_code != 0


class TestDeadLettersHttp403:
    def test_dead_letters_403_exits_nonzero(self, runner):
        """403 Forbidden during dead-letters results in a non-zero exit."""
        with patch("tools.cli.knowledge_cmd._client", _make_error_client(403)):
            result = runner.invoke(app, ["knowledge", "dead-letters"])
        assert result.exit_code != 0
