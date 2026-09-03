"""Tests for Cloudflare Access token management."""

from pathlib import Path
from unittest.mock import patch

import pytest

from tools.cli.auth import (
    _read_token,
    clear_cf_token,
    get_cf_token,
    read_cached_cf_token,
)


def test_read_cached_cf_token_never_starts_login(tmp_path):
    with (
        patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path),
        patch("tools.cli.auth.subprocess.run") as run,
    ):
        assert read_cached_cf_token() is None
    run.assert_not_called()


class TestGetCfToken:
    def test_returns_token_from_existing_file(self, tmp_path):
        token_file = tmp_path / "private.jomcgi.dev-token"
        token_file.write_text("my-cf-token")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            assert get_cf_token() == "my-cf-token"

    def test_raises_when_no_token_and_cloudflared_missing(self, tmp_path):
        with (
            patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path),
            patch("tools.cli.auth.shutil.which", return_value=None),
        ):
            with pytest.raises(SystemExit):
                get_cf_token()

    def test_runs_cloudflared_login_when_no_token(self, tmp_path):
        token_file = tmp_path / "private.jomcgi.dev-token"
        call_count = 0

        def fake_login(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            token_file.write_text("fresh-token")

        with (
            patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path),
            patch(
                "tools.cli.auth.shutil.which", return_value="/usr/local/bin/cloudflared"
            ),
            patch("tools.cli.auth.subprocess.run", side_effect=fake_login),
        ):
            result = get_cf_token()
            assert result == "fresh-token"
            assert call_count == 1

    def test_subprocess_run_called_with_timeout_300(self, tmp_path):
        """subprocess.run must be invoked with timeout=300 during cloudflared login."""
        token_file = tmp_path / "private.jomcgi.dev-token"

        def fake_login(*args, **kwargs):
            token_file.write_text("tok")

        with (
            patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path),
            patch(
                "tools.cli.auth.shutil.which", return_value="/usr/local/bin/cloudflared"
            ),
            patch("tools.cli.auth.subprocess.run", side_effect=fake_login) as mock_run,
        ):
            get_cf_token()
            _, kwargs = mock_run.call_args
            assert kwargs.get("timeout") == 300

    def test_custom_hostname_reads_matching_token(self, tmp_path):
        """Custom hostname resolves the file containing that hostname in its name."""
        token_file = tmp_path / "custom.example.com-token"
        token_file.write_text("custom-host-token")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            result = get_cf_token("custom.example.com")
        assert result == "custom-host-token"

    def test_custom_hostname_does_not_match_default_token(self, tmp_path):
        """A token file for the default hostname is not returned for a different hostname."""
        # Only the default hostname token exists.
        default_token = tmp_path / "private.jomcgi.dev-token"
        default_token.write_text("default-token")
        with (
            patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path),
            patch("tools.cli.auth.shutil.which", return_value=None),
        ):
            # No matching file for "other.example.com" → triggers SystemExit (no cloudflared).
            with pytest.raises(SystemExit):
                get_cf_token("other.example.com")

    def test_strips_leading_and_trailing_whitespace_from_token(self, tmp_path):
        """Token files with surrounding whitespace are stripped before returning."""
        token_file = tmp_path / "private.jomcgi.dev-token"
        token_file.write_text("  \n  actual-token  \n  ")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            result = get_cf_token()
        assert result == "actual-token"

    def test_strips_newline_only_whitespace(self, tmp_path):
        """Token files ending with a trailing newline (common in editors) are stripped."""
        token_file = tmp_path / "private.jomcgi.dev-token"
        token_file.write_text("newline-token\n")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            result = get_cf_token()
        assert result == "newline-token"

    def test_permission_error_reading_token_file_propagates(self, tmp_path):
        """PermissionError when reading the token file propagates to the caller."""
        token_file = tmp_path / "private.jomcgi.dev-token"
        token_file.write_text("secret")

        def mock_read_text(self, *args, **kwargs):
            raise PermissionError("Permission denied: token file")

        with (
            patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path),
            patch.object(Path, "read_text", mock_read_text),
        ):
            with pytest.raises(PermissionError):
                get_cf_token()

    def test_most_recent_token_file_wins(self, tmp_path):
        """When multiple token files match, the most recently modified one is used."""
        import time

        old_file = tmp_path / "private.jomcgi.dev-old"
        old_file.write_text("old-token")
        time.sleep(0.01)  # ensure mtime difference
        new_file = tmp_path / "private.jomcgi.dev-new"
        new_file.write_text("new-token")

        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            result = get_cf_token()
        assert result == "new-token"


class TestReadToken:
    """Direct unit tests for the _read_token() private helper.

    _read_token() is tested indirectly through get_cf_token() in TestGetCfToken,
    but direct tests isolate its two short-circuit branches and make the
    contract explicit without the noise of the cloudflared-login machinery.
    """

    def test_returns_none_when_token_dir_does_not_exist(self, tmp_path):
        """Returns None immediately when CF_TOKEN_DIR is not a directory."""
        nonexistent = tmp_path / "no-such-dir"
        with patch("tools.cli.auth.CF_TOKEN_DIR", nonexistent):
            result = _read_token("private.jomcgi.dev")
        assert result is None

    def test_returns_none_when_token_dir_is_a_file_not_a_directory(self, tmp_path):
        """Returns None when CF_TOKEN_DIR path exists but is a file, not a directory."""
        file_path = tmp_path / "not-a-dir"
        file_path.write_text("oops")
        with patch("tools.cli.auth.CF_TOKEN_DIR", file_path):
            result = _read_token("private.jomcgi.dev")
        assert result is None

    def test_returns_none_when_no_matching_file_in_dir(self, tmp_path):
        """Returns None when CF_TOKEN_DIR exists but contains no file matching hostname."""
        # Directory exists but has no files matching the hostname glob.
        (tmp_path / "other.host.com-token").write_text("unrelated")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            result = _read_token("private.jomcgi.dev")
        assert result is None

    def test_returns_token_content_when_matching_file_found(self, tmp_path):
        """Returns the stripped content of the matching token file."""
        token_file = tmp_path / "private.jomcgi.dev-token"
        token_file.write_text("direct-token\n")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            result = _read_token("private.jomcgi.dev")
        assert result == "direct-token"

    def test_returns_most_recent_file_when_multiple_match(self, tmp_path):
        """Returns the most recently modified file when multiple files match hostname."""
        import time

        old_file = tmp_path / "private.jomcgi.dev-old"
        old_file.write_text("stale-token")
        time.sleep(0.01)
        new_file = tmp_path / "private.jomcgi.dev-new"
        new_file.write_text("fresh-token")

        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            result = _read_token("private.jomcgi.dev")
        assert result == "fresh-token"

    def test_glob_pattern_matches_hostname_substring(self, tmp_path):
        """Token file is matched by glob when hostname appears anywhere in its name."""
        # The glob pattern is f"*{hostname}*" so the hostname can be a substring.
        token_file = tmp_path / "app.private.jomcgi.dev.access"
        token_file.write_text("substring-token")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            result = _read_token("private.jomcgi.dev")
        assert result == "substring-token"

    def test_custom_hostname_not_matched_by_unrelated_files(self, tmp_path):
        """Token files for different hostnames are not returned."""
        (tmp_path / "other.example.com-token").write_text("wrong-token")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            result = _read_token("my.hostname.com")
        assert result is None


class TestClearCfToken:
    """Tests for clear_cf_token() — removes cached CF token files for a hostname."""

    def test_deletes_matching_token_file(self, tmp_path):
        """Matching token file is removed from CF_TOKEN_DIR."""
        token_file = tmp_path / "private.jomcgi.dev-token"
        token_file.write_text("stale-token")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            clear_cf_token()
        assert not token_file.exists()

    def test_deletes_multiple_matching_files(self, tmp_path):
        """All files matching the hostname glob are deleted."""
        file_a = tmp_path / "private.jomcgi.dev-old"
        file_b = tmp_path / "private.jomcgi.dev-new"
        file_a.write_text("old-token")
        file_b.write_text("new-token")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            clear_cf_token()
        assert not file_a.exists()
        assert not file_b.exists()

    def test_no_op_when_cf_token_dir_does_not_exist(self, tmp_path):
        """Does not raise when CF_TOKEN_DIR doesn't exist."""
        nonexistent = tmp_path / "no-such-dir"
        with patch("tools.cli.auth.CF_TOKEN_DIR", nonexistent):
            clear_cf_token()  # must not raise

    def test_no_op_when_no_files_match_hostname(self, tmp_path):
        """No files are deleted when none match the hostname."""
        unrelated = tmp_path / "other.example.com-token"
        unrelated.write_text("unrelated-token")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            clear_cf_token("private.jomcgi.dev")
        assert unrelated.exists()

    def test_does_not_delete_unrelated_files(self, tmp_path):
        """Only files containing the hostname are deleted; others remain."""
        match = tmp_path / "private.jomcgi.dev-token"
        no_match = tmp_path / "other.example.com-token"
        match.write_text("my-token")
        no_match.write_text("other-token")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            clear_cf_token("private.jomcgi.dev")
        assert not match.exists()
        assert no_match.exists()

    def test_custom_hostname_deletes_only_matching_files(self, tmp_path):
        """Custom hostname targets only files containing that hostname."""
        target = tmp_path / "custom.example.com-access"
        other = tmp_path / "private.jomcgi.dev-token"
        target.write_text("custom-token")
        other.write_text("default-token")
        with patch("tools.cli.auth.CF_TOKEN_DIR", tmp_path):
            clear_cf_token("custom.example.com")
        assert not target.exists()
        assert other.exists()
