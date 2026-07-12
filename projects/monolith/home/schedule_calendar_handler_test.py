"""Tests for calendar_poll_handler() and on_startup_jobs() in home domain.

calendar_poll_handler() is a scheduler handler that delegates to
poll_calendar() and always returns None (no next_run_at override).

on_startup_jobs() wires calendar_poll_handler into the distributed scheduler with
a 15-minute interval and a 120-second TTL.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from home.schedule import calendar_poll_handler
from home import on_startup_jobs


# ---------------------------------------------------------------------------
# calendar_poll_handler
# ---------------------------------------------------------------------------


class TestCalendarPollHandler:
    @pytest.mark.asyncio
    async def test_calls_poll_calendar(self):
        """calendar_poll_handler() must invoke poll_calendar() exactly once."""
        with patch("home.schedule.poll_calendar", new_callable=AsyncMock) as mock_poll:
            await calendar_poll_handler()
        mock_poll.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_returns_none(self):
        """calendar_poll_handler() always returns None (no next_run_at override)."""
        with patch("home.schedule.poll_calendar", new_callable=AsyncMock):
            result = await calendar_poll_handler()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_calendar_called_without_arguments(self):
        """poll_calendar is called with no arguments (all defaults)."""
        with patch("home.schedule.poll_calendar", new_callable=AsyncMock) as mock_poll:
            await calendar_poll_handler()
        # Verify positional and keyword arguments are empty
        args, kwargs = mock_poll.call_args
        assert args == ()
        assert kwargs == {}

    @pytest.mark.asyncio
    async def test_return_value_is_always_none_regardless_of_poll_result(self):
        """Return value is None even if poll_calendar() returns a non-None value."""
        with patch("home.schedule.poll_calendar", new_callable=AsyncMock) as mock_poll:
            mock_poll.return_value = "unexpected"
            result = await calendar_poll_handler()
        assert result is None

    @pytest.mark.asyncio
    async def test_awaits_poll_calendar(self):
        """calendar_poll_handler awaits poll_calendar (it is a coroutine)."""
        call_order = []

        async def tracked_poll():
            call_order.append("poll_calendar")

        with patch("home.schedule.poll_calendar", side_effect=tracked_poll):
            await calendar_poll_handler()

        assert call_order == ["poll_calendar"]


# ---------------------------------------------------------------------------
# on_startup_jobs
# ---------------------------------------------------------------------------


def _calls_by_name(mock_register):
    """Map each register_job(...) call to its job name kwarg.

    on_startup_jobs registers more than one job, so tests select the call they
    mean by name instead of relying on call_args (which is only the last call).
    """
    return {call.kwargs["name"]: call for call in mock_register.call_args_list}


class TestOnStartupJobs:
    def test_registers_calendar_and_cluster_snapshot_jobs(self):
        """on_startup_jobs() registers both the calendar poll and the cluster
        health snapshot refresh."""
        mock_session = MagicMock()
        with patch("scheduler.api.register_job") as mock_register:
            on_startup_jobs(mock_session)
        assert mock_register.call_count == 2
        names = {call.kwargs["name"] for call in mock_register.call_args_list}
        assert names == {"home.calendar_poll", "home.cluster_snapshot_refresh"}

    def test_passes_session_to_every_register_job(self):
        """on_startup_jobs() forwards its session as the first positional arg to
        every registration."""
        mock_session = MagicMock()
        with patch("scheduler.api.register_job") as mock_register:
            on_startup_jobs(mock_session)
        for call in mock_register.call_args_list:
            assert call.args[0] is mock_session

    def test_calendar_job_interval_and_ttl(self):
        """Calendar poll runs every 900 seconds (15 minutes) with a 120s TTL."""
        mock_session = MagicMock()
        with patch("scheduler.api.register_job") as mock_register:
            on_startup_jobs(mock_session)
        cal = _calls_by_name(mock_register)["home.calendar_poll"]
        assert cal.kwargs["interval_secs"] == 900
        assert cal.kwargs["ttl_secs"] == 120

    def test_cluster_snapshot_job_interval_and_ttl(self):
        """The cluster snapshot refresh runs every 60 seconds with a 120s TTL."""
        mock_session = MagicMock()
        with patch("scheduler.api.register_job") as mock_register:
            on_startup_jobs(mock_session)
        snap = _calls_by_name(mock_register)["home.cluster_snapshot_refresh"]
        assert snap.kwargs["interval_secs"] == 60
        assert snap.kwargs["ttl_secs"] == 120

    @pytest.mark.asyncio
    async def test_calendar_handler_delegates_to_calendar_poll_handler(self):
        """The calendar job's handler wraps calendar_poll_handler."""
        mock_session = MagicMock()
        with patch("scheduler.api.register_job") as mock_register:
            on_startup_jobs(mock_session)
        cal = _calls_by_name(mock_register)["home.calendar_poll"]
        with patch("home.schedule.poll_calendar", new_callable=AsyncMock):
            result = await cal.kwargs["handler"](mock_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_cluster_snapshot_handler_delegates_to_refresh(self):
        """The snapshot job's handler wraps refresh_cluster_snapshot."""
        mock_session = MagicMock()
        with patch("scheduler.api.register_job") as mock_register:
            on_startup_jobs(mock_session)
        snap = _calls_by_name(mock_register)["home.cluster_snapshot_refresh"]
        with patch(
            "home.cluster_snapshot.refresh_cluster_snapshot", new_callable=AsyncMock
        ) as mock_refresh:
            result = await snap.kwargs["handler"](mock_session)
        mock_refresh.assert_awaited_once()
        assert result is None
