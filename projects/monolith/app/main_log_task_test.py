"""Tests for app.main._log_task_exception() -- background task error logging."""

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

# Ensure no valid STATIC_DIR is set before importing main
os.environ.pop("STATIC_DIR", None)

from app.main import _log_task_exception  # noqa: E402


class TestLogTaskException:
    def test_logs_error_when_task_has_exception(self):
        """_log_task_exception logs an error when the task raised an exception."""
        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("something went wrong")
        task.get_name.return_value = "my-task"

        with patch("framework.core.logger") as mock_logger:
            _log_task_exception(task)

        mock_logger.error.assert_called_once()
        call_args = mock_logger.error.call_args
        # The task name should appear in the log message
        assert "my-task" in call_args[0][1]
        # exc_info should include the exception
        assert call_args[1].get("exc_info") == task.exception.return_value

    def test_does_not_log_when_task_is_cancelled(self):
        """_log_task_exception does not log when the task was cancelled."""
        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = True
        task.get_name.return_value = "cancelled-task"

        with patch("framework.core.logger") as mock_logger:
            _log_task_exception(task)

        mock_logger.error.assert_not_called()

    def test_does_not_log_when_task_succeeded(self):
        """_log_task_exception does not log when the task completed without exception."""
        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = False
        task.exception.return_value = None

        with patch("framework.core.logger") as mock_logger:
            _log_task_exception(task)

        mock_logger.error.assert_not_called()

    def test_does_not_log_when_cancelled_even_if_exception_set(self):
        """Cancelled status takes precedence -- no log even if exception() is non-None."""
        task = MagicMock(spec=asyncio.Task)
        task.cancelled.return_value = True
        task.exception.return_value = RuntimeError("irrelevant")
        task.get_name.return_value = "task"

        with patch("framework.core.logger") as mock_logger:
            _log_task_exception(task)

        mock_logger.error.assert_not_called()
