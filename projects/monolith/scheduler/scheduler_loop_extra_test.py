"""Extra tests for run_scheduler_loop() — poll_interval propagation and iteration counts.

scheduler_loop_test.py covers the exception-handling path for dispatch_due_jobs.
These tests cover the complementary cases:

- poll_interval is forwarded verbatim to asyncio.sleep on every iteration
- The default poll_interval of 30 seconds is used when none is specified
- The loop executes multiple successful iterations without termination
- asyncio.sleep is called once per loop iteration (after each dispatch call)
"""

import pytest
from unittest.mock import AsyncMock, patch

from scheduler.api import run_scheduler_loop


class TestRunSchedulerLoopPollInterval:
    @pytest.mark.asyncio
    async def test_sleep_called_with_custom_poll_interval(self):
        """asyncio.sleep is called with the provided poll_interval value."""
        tick_count = 0

        async def tick_side_effect(*_args, **_kwargs):
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 2:
                raise KeyboardInterrupt

        with (
            patch("scheduler.api.dispatch_due_jobs", side_effect=tick_side_effect),
            patch("scheduler.api.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            with pytest.raises(KeyboardInterrupt):
                await run_scheduler_loop(poll_interval=42)

        # sleep was called at least once and always with our custom interval
        assert mock_sleep.call_count >= 1
        for call_args in mock_sleep.call_args_list:
            assert call_args.args[0] == 42, (
                f"Expected sleep(42) but got sleep({call_args.args[0]})"
            )

    @pytest.mark.asyncio
    async def test_default_poll_interval_is_30_seconds(self):
        """When poll_interval is not specified, asyncio.sleep uses 30 seconds."""
        tick_count = 0

        async def tick_side_effect(*_args, **_kwargs):
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 2:
                raise KeyboardInterrupt

        with (
            patch("scheduler.api.dispatch_due_jobs", side_effect=tick_side_effect),
            patch("scheduler.api.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            with pytest.raises(KeyboardInterrupt):
                await run_scheduler_loop()  # no poll_interval arg

        assert mock_sleep.call_count >= 1
        for call_args in mock_sleep.call_args_list:
            assert call_args.args[0] == 30, (
                f"Expected default sleep(30) but got sleep({call_args.args[0]})"
            )

    @pytest.mark.asyncio
    async def test_poll_interval_zero_is_respected(self):
        """poll_interval=0 is a valid value and is forwarded to asyncio.sleep."""
        tick_count = 0

        async def tick_side_effect(*_args, **_kwargs):
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 2:
                raise KeyboardInterrupt

        with (
            patch("scheduler.api.dispatch_due_jobs", side_effect=tick_side_effect),
            patch("scheduler.api.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            with pytest.raises(KeyboardInterrupt):
                await run_scheduler_loop(poll_interval=0)

        assert mock_sleep.call_count >= 1
        for call_args in mock_sleep.call_args_list:
            assert call_args.args[0] == 0


class TestRunSchedulerLoopIterations:
    @pytest.mark.asyncio
    async def test_runs_multiple_successful_iterations(self):
        """The loop executes multiple dispatch_due_jobs() calls successfully."""
        tick_count = 0

        async def tick_side_effect(*_args, **_kwargs):
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 5:
                raise KeyboardInterrupt

        with (
            patch("scheduler.api.dispatch_due_jobs", side_effect=tick_side_effect),
            patch("scheduler.api.asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(KeyboardInterrupt):
                await run_scheduler_loop(poll_interval=0)

        assert tick_count == 5

    @pytest.mark.asyncio
    async def test_sleep_called_once_per_successful_iteration(self):
        """asyncio.sleep is called after every tick, including those that succeed."""
        tick_count = 0
        sleep_count = 0

        async def tick_side_effect(*_args, **_kwargs):
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 4:
                raise KeyboardInterrupt

        async def sleep_side_effect(_):
            nonlocal sleep_count
            sleep_count += 1

        with (
            patch("scheduler.api.dispatch_due_jobs", side_effect=tick_side_effect),
            patch("scheduler.api.asyncio.sleep", side_effect=sleep_side_effect),
        ):
            with pytest.raises(KeyboardInterrupt):
                await run_scheduler_loop(poll_interval=0)

        # tick_count=4 raises KeyboardInterrupt before sleep is called;
        # sleep should have been called for the first 3 ticks.
        assert sleep_count == tick_count - 1

    @pytest.mark.asyncio
    async def test_sleep_called_after_failed_ticks_too(self):
        """asyncio.sleep is called even when dispatch_due_jobs() raises a caught exception."""
        tick_count = 0
        sleep_count = 0

        async def tick_side_effect(*_args, **_kwargs):
            nonlocal tick_count
            tick_count += 1
            if tick_count <= 2:
                raise RuntimeError("simulated tick failure")
            if tick_count >= 4:
                raise KeyboardInterrupt

        async def sleep_side_effect(_):
            nonlocal sleep_count
            sleep_count += 1

        with (
            patch("scheduler.api.dispatch_due_jobs", side_effect=tick_side_effect),
            patch("scheduler.api.asyncio.sleep", side_effect=sleep_side_effect),
        ):
            with pytest.raises(KeyboardInterrupt):
                await run_scheduler_loop(poll_interval=0)

        # ticks 1, 2 raised RuntimeError (caught), tick 3 succeeded, tick 4 raised KeyboardInterrupt
        # sleep called after ticks 1, 2, 3 → 3 times
        assert sleep_count == tick_count - 1

    @pytest.mark.asyncio
    async def test_mixed_failure_and_success_continues_with_correct_interval(self):
        """The loop uses the same poll_interval after both successful and failed ticks."""
        tick_count = 0
        sleep_args_seen = []

        async def tick_side_effect(*_args, **_kwargs):
            nonlocal tick_count
            tick_count += 1
            if tick_count == 1:
                raise RuntimeError("first tick fails")
            if tick_count == 3:
                raise KeyboardInterrupt

        async def sleep_side_effect(interval):
            sleep_args_seen.append(interval)

        with (
            patch("scheduler.api.dispatch_due_jobs", side_effect=tick_side_effect),
            patch("scheduler.api.asyncio.sleep", side_effect=sleep_side_effect),
        ):
            with pytest.raises(KeyboardInterrupt):
                await run_scheduler_loop(poll_interval=15)

        # All sleep calls must use the same poll_interval
        assert all(v == 15 for v in sleep_args_seen), (
            f"Expected all sleep calls with 15 but got {sleep_args_seen}"
        )
