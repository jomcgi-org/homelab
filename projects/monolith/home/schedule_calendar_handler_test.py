"""Tests for the calendar handler invoked by the one-shot Argo job."""

import pytest
from unittest.mock import AsyncMock, patch

from home.schedule import calendar_poll_handler


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
