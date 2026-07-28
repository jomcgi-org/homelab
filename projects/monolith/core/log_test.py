"""Tests for core.log -- _HealthzFilter and configure_logging()."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from core.log import _HealthzFilter, configure_logging


class TestHealthzFilter:
    def test_suppresses_healthz_records(self):
        """_HealthzFilter.filter() returns False for records containing '/healthz'."""
        f = _HealthzFilter()
        record = MagicMock(spec=logging.LogRecord)
        record.getMessage.return_value = "GET /healthz HTTP/1.1 200"
        assert f.filter(record) is False

    def test_passes_non_healthz_records(self):
        """_HealthzFilter.filter() returns True for records not containing '/healthz'."""
        f = _HealthzFilter()
        record = MagicMock(spec=logging.LogRecord)
        record.getMessage.return_value = "GET /api/todo/daily HTTP/1.1 200"
        assert f.filter(record) is True

    def test_passes_empty_message(self):
        """_HealthzFilter.filter() returns True for empty log messages."""
        f = _HealthzFilter()
        record = MagicMock(spec=logging.LogRecord)
        record.getMessage.return_value = ""
        assert f.filter(record) is True

    def test_suppresses_when_healthz_in_path_only(self):
        """_HealthzFilter.filter() suppresses any message containing '/healthz' substring."""
        f = _HealthzFilter()
        record = MagicMock(spec=logging.LogRecord)
        record.getMessage.return_value = "some prefix /healthz suffix"
        assert f.filter(record) is False

    def test_suppresses_healthz_in_any_context(self):
        """_HealthzFilter.filter() suppresses any message that contains '/healthz'."""
        f = _HealthzFilter()
        record = MagicMock(spec=logging.LogRecord)
        # Note: '/healthz' substring present — should be suppressed
        record.getMessage.return_value = "debug: probing /healthz endpoint"
        assert f.filter(record) is False


class TestConfigureLogging:
    def setup_method(self):
        """Clear uvicorn.access filters before each test to prevent _HealthzFilter accumulation.

        configure_logging() calls addFilter() without deduplication, so repeated calls
        across tests would stack multiple _HealthzFilter instances on the global logger.
        Resetting here keeps each test isolated regardless of execution order.
        """
        logging.getLogger("uvicorn.access").filters.clear()

    def test_sets_root_logger_level(self):
        """configure_logging() sets the root logger to the specified level."""
        configure_logging(logging.DEBUG)
        assert logging.getLogger().level == logging.DEBUG
        # reset
        configure_logging(logging.INFO)

    def test_root_level_defaults_to_info(self):
        """configure_logging() defaults the root logger to INFO."""
        configure_logging()
        assert logging.getLogger().level == logging.INFO

    def test_discord_gateway_set_to_warning(self):
        """configure_logging() sets discord.gateway to WARNING."""
        configure_logging()
        assert logging.getLogger("discord.gateway").level == logging.WARNING

    def test_discord_client_set_to_error(self):
        """configure_logging() sets discord.client to ERROR."""
        configure_logging()
        assert logging.getLogger("discord.client").level == logging.ERROR

    def test_httpx_set_to_warning(self):
        """configure_logging() sets httpx to WARNING."""
        configure_logging()
        assert logging.getLogger("httpx").level == logging.WARNING

    def test_httpcore_set_to_warning(self):
        """configure_logging() sets httpcore to WARNING."""
        configure_logging()
        assert logging.getLogger("httpcore").level == logging.WARNING

    def test_healthz_filter_attached_to_uvicorn_access(self):
        """configure_logging() adds a _HealthzFilter to uvicorn.access logger."""
        configure_logging()
        uvicorn_access = logging.getLogger("uvicorn.access")
        filter_types = [type(f) for f in uvicorn_access.filters]
        assert _HealthzFilter in filter_types, (
            "Expected _HealthzFilter to be attached to uvicorn.access logger"
        )
