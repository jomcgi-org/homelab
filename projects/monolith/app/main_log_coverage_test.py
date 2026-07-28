"""Extra coverage tests for core.log and app.main._wait_for_sidecar.

Covers branches not addressed by existing log_test.py / log_config_test.py:

1. configure_logging() sets uvicorn logger to WARNING (line 33 of log.py).
2. configure_logging() sets uvicorn.error logger to WARNING (line 34 of log.py).
3. configure_logging() passes stream=sys.stdout to basicConfig.
4. configure_logging() passes the expected format string to basicConfig.
5. configure_logging() passes force=True to basicConfig.
6. _wait_for_sidecar(): status 499 (< 500) → returns immediately (boundary).
7. _wait_for_sidecar(): status 500 (>= 500) → retries, then status 200 → returns.
8. _wait_for_sidecar(): asyncio.sleep is called between retries.
9. _wait_for_sidecar(): multiple consecutive httpx.HTTPError exceptions before success.
"""

from __future__ import annotations

import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# Ensure no valid static directory is set before importing main symbols.
os.environ.pop("STATIC_DIR", None)

from core.log import configure_logging  # noqa: E402
from app.main import _wait_for_sidecar  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_uvicorn_filters():
    """Clear uvicorn.access filters so configure_logging tests don't stack them."""
    logging.getLogger("uvicorn.access").filters.clear()


def _make_mock_async_client(responses):
    """Return (mock_cm, mock_client) for patching httpx.AsyncClient."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=responses)
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm, mock_client


def _resp(status_code: int) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    return r


# ---------------------------------------------------------------------------
# configure_logging() — uvicorn / uvicorn.error logger levels
# ---------------------------------------------------------------------------


class TestConfigureLoggingUvicornLevels:
    def setup_method(self):
        _clear_uvicorn_filters()

    def test_uvicorn_logger_set_to_warning(self):
        """configure_logging() sets the uvicorn logger to WARNING."""
        configure_logging()
        assert logging.getLogger("uvicorn").level == logging.WARNING, (
            "Expected uvicorn logger to be set to WARNING by configure_logging()"
        )

    def test_uvicorn_error_logger_set_to_warning(self):
        """configure_logging() sets the uvicorn.error logger to WARNING."""
        configure_logging()
        assert logging.getLogger("uvicorn.error").level == logging.WARNING, (
            "Expected uvicorn.error logger to be set to WARNING by configure_logging()"
        )

    def test_uvicorn_level_unaffected_by_custom_root_level(self):
        """uvicorn is set to WARNING regardless of the root level argument."""
        configure_logging(logging.DEBUG)
        assert logging.getLogger("uvicorn").level == logging.WARNING

    def test_uvicorn_error_level_unaffected_by_custom_root_level(self):
        """uvicorn.error is set to WARNING regardless of the root level argument."""
        configure_logging(logging.DEBUG)
        assert logging.getLogger("uvicorn.error").level == logging.WARNING


# ---------------------------------------------------------------------------
# configure_logging() — basicConfig format and stream assertions
# ---------------------------------------------------------------------------


class TestConfigureLoggingBasicConfigArgs:
    def setup_method(self):
        _clear_uvicorn_filters()

    def test_basicconfig_receives_stdout_stream(self):
        """configure_logging() passes stream=sys.stdout to logging.basicConfig."""
        with patch("logging.basicConfig") as mock_basicconfig:
            configure_logging()
        kwargs = mock_basicconfig.call_args[1]
        assert kwargs.get("stream") is sys.stdout, (
            "Expected stream=sys.stdout to be passed to logging.basicConfig"
        )

    def test_basicconfig_receives_expected_format_string(self):
        """configure_logging() passes the structured format string to basicConfig."""
        with patch("logging.basicConfig") as mock_basicconfig:
            configure_logging()
        kwargs = mock_basicconfig.call_args[1]
        fmt = kwargs.get("format", "")
        assert "%(levelname)s" in fmt, "Format string should include %(levelname)s"
        assert "%(name)s" in fmt, "Format string should include %(name)s"
        assert "%(message)s" in fmt, "Format string should include %(message)s"

    def test_basicconfig_receives_force_true(self):
        """configure_logging() passes force=True to logging.basicConfig."""
        with patch("logging.basicConfig") as mock_basicconfig:
            configure_logging()
        kwargs = mock_basicconfig.call_args[1]
        assert kwargs.get("force") is True, (
            "Expected force=True to be passed to logging.basicConfig"
        )


# ---------------------------------------------------------------------------
# _wait_for_sidecar() — status-code boundary conditions
# ---------------------------------------------------------------------------


class TestWaitForSidecarStatusBoundary:
    @pytest.mark.asyncio
    async def test_status_499_returns_immediately(self):
        """A 499 response (< 500) causes _wait_for_sidecar to return without retrying."""
        mock_cm, mock_client = _make_mock_async_client([_resp(499)])

        with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
            with patch("httpx.AsyncClient", return_value=mock_cm):
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    await _wait_for_sidecar()

        # Should have been called exactly once (499 < 500 → return)
        assert mock_client.get.call_count == 1
        # sleep should NOT have been called since we returned on the first attempt
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_status_200_returns_immediately(self):
        """A 200 response (< 500) causes _wait_for_sidecar to return without retrying."""
        mock_cm, mock_client = _make_mock_async_client([_resp(200)])

        with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
            with patch("httpx.AsyncClient", return_value=mock_cm):
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    await _wait_for_sidecar()

        assert mock_client.get.call_count == 1
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_status_500_retries_and_then_succeeds(self):
        """A 500 response (>= 500) causes a retry; succeeds on the next 200."""
        mock_cm, mock_client = _make_mock_async_client([_resp(500), _resp(200)])

        with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
            with patch("httpx.AsyncClient", return_value=mock_cm):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await _wait_for_sidecar()

        assert mock_client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_status_503_retries(self):
        """A 503 response (>= 500) is treated identically to 500 — causes retry."""
        mock_cm, mock_client = _make_mock_async_client([_resp(503), _resp(200)])

        with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
            with patch("httpx.AsyncClient", return_value=mock_cm):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await _wait_for_sidecar()

        assert mock_client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_sleep_called_after_5xx_response(self):
        """asyncio.sleep(2) is called after each 5xx response before the next retry."""
        mock_cm, mock_client = _make_mock_async_client([_resp(500), _resp(200)])

        with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
            with patch("httpx.AsyncClient", return_value=mock_cm):
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    await _wait_for_sidecar()

        mock_sleep.assert_called_once_with(2)

    @pytest.mark.asyncio
    async def test_sleep_called_after_http_error(self):
        """asyncio.sleep(2) is called after an httpx.HTTPError before retrying."""
        import httpx

        mock_cm, mock_client = _make_mock_async_client(
            [httpx.HTTPError("connection refused"), _resp(200)]
        )

        with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
            with patch("httpx.AsyncClient", return_value=mock_cm):
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    await _wait_for_sidecar()

        mock_sleep.assert_called_once_with(2)
        assert mock_client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_multiple_http_errors_then_success(self):
        """Multiple consecutive httpx.HTTPErrors are retried until a success response."""
        import httpx

        errors = [httpx.HTTPError("err1"), httpx.HTTPError("err2"), _resp(200)]
        mock_cm, mock_client = _make_mock_async_client(errors)

        with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
            with patch("httpx.AsyncClient", return_value=mock_cm):
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    await _wait_for_sidecar()

        assert mock_client.get.call_count == 3
        # sleep called after each of the two errors
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(2)

    @pytest.mark.asyncio
    async def test_status_404_returns_immediately(self):
        """A 404 (< 500) is treated as 'ready' — returns without retrying."""
        mock_cm, mock_client = _make_mock_async_client([_resp(404)])

        with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
            with patch("httpx.AsyncClient", return_value=mock_cm):
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    await _wait_for_sidecar()

        assert mock_client.get.call_count == 1
        mock_sleep.assert_not_called()
