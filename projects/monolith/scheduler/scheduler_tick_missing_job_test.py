"""Tests for the _run_claimed_job branch where session.get(ScheduledJob, job_name) returns None.

This can happen legitimately: dispatch_due_jobs claims a batch of jobs in one
session, then spawns each handler with its own session. Between the claim
commit and the handler re-fetching the row, another process could delete it
(e.g. purge_stale_jobs in a second pod).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session

from scheduler.api import _registry, _run_claimed_job


@pytest.fixture(autouse=True)
def _clear_registry():
    """Ensure a clean handler registry for each test."""
    _registry.clear()
    yield
    _registry.clear()


class TestRunClaimedJobMissingRow:
    @pytest.mark.asyncio
    async def test_returns_early_when_job_row_not_found(self):
        """When session.get returns None, _run_claimed_job returns early
        without calling any of _complete_job, _fail_job, or _release_lock."""
        handler = AsyncMock()
        _registry["ghost-job"] = handler

        mock_session = MagicMock(spec=Session)
        mock_session.get.return_value = None  # row disappeared between claim and run

        with (
            patch("scheduler.api.get_engine"),
            patch("scheduler.api.Session") as mock_session_cls,
            patch("scheduler.api._complete_job") as mock_complete,
            patch("scheduler.api._fail_job") as mock_fail,
            patch("scheduler.api._release_lock") as mock_release,
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=mock_session
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await _run_claimed_job("ghost-job")

        mock_complete.assert_not_called()
        mock_fail.assert_not_called()
        mock_release.assert_not_called()
        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_raise_when_job_row_missing(self):
        """_run_claimed_job with a missing job row must not propagate any exception."""
        mock_session = MagicMock(spec=Session)
        mock_session.get.return_value = None

        with (
            patch("scheduler.api.get_engine"),
            patch("scheduler.api.Session") as mock_session_cls,
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(
                return_value=mock_session
            )
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            # Should complete silently
            await _run_claimed_job("no-such-job")
