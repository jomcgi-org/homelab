"""Unit tests for core.log — _HealthzFilter and configure_logging()."""

import logging
from unittest.mock import MagicMock, patch

from core.log import _HealthzFilter, configure_logging


# ---------------------------------------------------------------------------
# _HealthzFilter.filter()
# ---------------------------------------------------------------------------


def test_healthz_filter_suppresses_healthz_message():
    """_HealthzFilter.filter() returns False when message contains '/healthz'."""
    f = _HealthzFilter()
    record = MagicMock(spec=logging.LogRecord)
    record.getMessage.return_value = "GET /healthz HTTP/1.1 200"
    assert f.filter(record) is False


def test_healthz_filter_passes_non_healthz_message():
    """_HealthzFilter.filter() returns True when message does not contain '/healthz'."""
    f = _HealthzFilter()
    record = MagicMock(spec=logging.LogRecord)
    record.getMessage.return_value = "GET /api/todo/daily HTTP/1.1 200"
    assert f.filter(record) is True


def test_healthz_filter_passes_empty_message():
    """_HealthzFilter.filter() returns True for an empty log message."""
    f = _HealthzFilter()
    record = MagicMock(spec=logging.LogRecord)
    record.getMessage.return_value = ""
    assert f.filter(record) is True


def test_healthz_filter_suppresses_healthz_with_query_string():
    """_HealthzFilter.filter() returns False when message contains '/healthz?query=1'."""
    f = _HealthzFilter()
    record = MagicMock(spec=logging.LogRecord)
    record.getMessage.return_value = "GET /healthz?query=1 HTTP/1.1 200"
    assert f.filter(record) is False


def test_healthz_filter_suppresses_exact_healthz_path():
    """_HealthzFilter.filter() returns False when message is exactly '/healthz'."""
    f = _HealthzFilter()
    record = MagicMock(spec=logging.LogRecord)
    record.getMessage.return_value = "/healthz"
    assert f.filter(record) is False


def test_healthz_filter_suppresses_healthz_as_substring():
    """_HealthzFilter.filter() returns False when '/healthz' appears anywhere in message."""
    f = _HealthzFilter()
    record = MagicMock(spec=logging.LogRecord)
    record.getMessage.return_value = "probing /healthz endpoint via k8s"
    assert f.filter(record) is False


# ---------------------------------------------------------------------------
# configure_logging()
# ---------------------------------------------------------------------------


def setup_function():
    """Clear uvicorn.access filters before each test to prevent filter accumulation."""
    logging.getLogger("uvicorn.access").filters.clear()


def test_configure_logging_sets_root_level_debug():
    """configure_logging(logging.DEBUG) passes DEBUG level to basicConfig."""
    with patch("logging.basicConfig") as mock_basicconfig:
        configure_logging(logging.DEBUG)
    kwargs = mock_basicconfig.call_args[1]
    assert kwargs.get("level") == logging.DEBUG


def test_configure_logging_sets_root_level_info():
    """configure_logging(logging.INFO) passes INFO level to basicConfig."""
    with patch("logging.basicConfig") as mock_basicconfig:
        configure_logging(logging.INFO)
    kwargs = mock_basicconfig.call_args[1]
    assert kwargs.get("level") == logging.INFO


def test_configure_logging_sets_root_level_warning():
    """configure_logging(logging.WARNING) passes WARNING level to basicConfig."""
    with patch("logging.basicConfig") as mock_basicconfig:
        configure_logging(logging.WARNING)
    kwargs = mock_basicconfig.call_args[1]
    assert kwargs.get("level") == logging.WARNING


def test_configure_logging_defaults_to_info():
    """configure_logging() without arguments passes INFO level to basicConfig."""
    with patch("logging.basicConfig") as mock_basicconfig:
        configure_logging()
    kwargs = mock_basicconfig.call_args[1]
    assert kwargs.get("level") == logging.INFO


def test_configure_logging_calls_basicconfig_with_level():
    """configure_logging() calls logging.basicConfig exactly once."""
    with patch("logging.basicConfig") as mock_basicconfig:
        configure_logging(logging.DEBUG)
    mock_basicconfig.assert_called_once()


def test_configure_logging_discord_gateway_set_to_warning():
    """configure_logging() sets discord.gateway logger to WARNING."""
    with patch("logging.basicConfig"):
        configure_logging()
    assert logging.getLogger("discord.gateway").level == logging.WARNING


def test_configure_logging_discord_client_set_to_error():
    """configure_logging() sets discord.client logger to ERROR."""
    with patch("logging.basicConfig"):
        configure_logging()
    assert logging.getLogger("discord.client").level == logging.ERROR


def test_configure_logging_httpx_set_to_warning():
    """configure_logging() sets httpx logger to WARNING."""
    with patch("logging.basicConfig"):
        configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING


def test_configure_logging_httpcore_set_to_warning():
    """configure_logging() sets httpcore logger to WARNING."""
    with patch("logging.basicConfig"):
        configure_logging()
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_configure_logging_adds_healthz_filter_to_uvicorn_access():
    """configure_logging() attaches a _HealthzFilter to the uvicorn.access logger."""
    with patch("logging.basicConfig"):
        configure_logging()
    uvicorn_access = logging.getLogger("uvicorn.access")
    filter_types = [type(f) for f in uvicorn_access.filters]
    assert _HealthzFilter in filter_types, (
        "Expected _HealthzFilter to be attached to uvicorn.access logger"
    )
