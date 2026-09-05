"""Unit tests for the leader-election state machine (core.leadership).

The lease query is Postgres-specific (ON CONFLICT ... WHERE, make_interval), so
it is mocked here; these tests exercise the transition logic - acquire/resign
callbacks, no double-firing, fail-safe to follower, and lease release on
shutdown - not the SQL itself.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

import core.leadership as leadership


def _seq(values):
    """A fake _acquire_or_renew that yields `values` then stays a follower."""
    it = iter(values)

    def f(*_args):
        try:
            return next(it)
        except StopIteration:
            return False

    return f


@pytest.mark.asyncio
async def test_acquire_then_resign_fire_once(monkeypatch):
    # leader, leader (stay), follower -> acquire once, resign once.
    monkeypatch.setattr(leadership, "_acquire_or_renew", _seq([True, True, False]))
    monkeypatch.setattr(leadership, "RENEW_INTERVAL", 0)
    on_acquire = mock.AsyncMock()
    on_resign = mock.AsyncMock()
    elector = leadership.LeaderElector()

    task = asyncio.create_task(elector.run(on_acquire, on_resign))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    on_acquire.assert_awaited_once()
    on_resign.assert_awaited_once()
    assert elector.is_leader is False


@pytest.mark.asyncio
async def test_db_error_is_follower(monkeypatch):
    def boom(*_args):
        raise RuntimeError("db down")

    monkeypatch.setattr(leadership, "_acquire_or_renew", boom)
    monkeypatch.setattr(leadership, "RENEW_INTERVAL", 0)
    on_acquire = mock.AsyncMock()
    on_resign = mock.AsyncMock()
    elector = leadership.LeaderElector()

    task = asyncio.create_task(elector.run(on_acquire, on_resign))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Fail-safe: never became leader, so singletons never started.
    on_acquire.assert_not_awaited()
    assert elector.is_leader is False


@pytest.mark.asyncio
async def test_acquire_hook_failure_resigns_and_reacquires(monkeypatch):
    monkeypatch.setattr(leadership, "_acquire_or_renew", lambda *_a: True)
    monkeypatch.setattr(leadership, "RENEW_INTERVAL", 0)
    monkeypatch.setattr(leadership, "ACQUIRE_BACKOFF_INITIAL", 0)
    released = mock.Mock()
    monkeypatch.setattr(leadership, "_release", released)
    reacquired = asyncio.Event()
    acquire_calls = 0

    async def on_acquire():
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls == 1:
            raise RuntimeError("singleton startup failed")
        reacquired.set()

    on_resign = mock.AsyncMock()
    elector = leadership.LeaderElector()
    task = asyncio.create_task(elector.run(on_acquire, on_resign))

    await asyncio.wait_for(reacquired.wait(), timeout=0.5)

    assert not task.done()
    assert elector.is_leader is True
    assert elector.consecutive_acquire_failures == 0
    on_resign.assert_awaited_once()
    released.assert_called_once()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_repeated_acquire_hook_failures_trip_health_flag(monkeypatch):
    monkeypatch.setattr(leadership, "_acquire_or_renew", lambda *_a: True)
    monkeypatch.setattr(leadership, "_release", mock.Mock())
    monkeypatch.setattr(leadership, "RENEW_INTERVAL", 0)
    monkeypatch.setattr(leadership, "ACQUIRE_BACKOFF_INITIAL", 0)
    next_attempt_started = asyncio.Event()
    acquire_calls = 0

    async def on_acquire():
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls <= leadership.MAX_CONSECUTIVE_ACQUIRE_FAILURES:
            raise RuntimeError("singleton startup failed")
        next_attempt_started.set()
        await asyncio.Event().wait()

    elector = leadership.LeaderElector()
    task = asyncio.create_task(elector.run(on_acquire, mock.AsyncMock()))

    await asyncio.wait_for(next_attempt_started.wait(), timeout=0.5)

    assert not task.done()
    assert elector.acquire_failures_exceeded is True
    assert (
        elector.consecutive_acquire_failures
        == leadership.MAX_CONSECUTIVE_ACQUIRE_FAILURES
    )

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_acquire_hook_failure_backoff_grows(monkeypatch):
    class StopElection(Exception):
        pass

    monkeypatch.setattr(leadership, "_acquire_or_renew", lambda *_a: True)
    monkeypatch.setattr(leadership, "_release", mock.Mock())
    monkeypatch.setattr(leadership.random, "uniform", lambda *_a: 1.0)
    sleep_intervals: list[float] = []

    async def fake_sleep(interval: float) -> None:
        sleep_intervals.append(interval)
        if len(sleep_intervals) == 3:
            raise StopElection

    async def failing_acquire() -> None:
        raise RuntimeError("singleton startup failed")

    monkeypatch.setattr(leadership.asyncio, "sleep", fake_sleep)
    elector = leadership.LeaderElector()

    with pytest.raises(StopElection):
        await elector.run(failing_acquire, mock.AsyncMock())

    assert sleep_intervals == [
        leadership.ACQUIRE_BACKOFF_INITIAL,
        leadership.ACQUIRE_BACKOFF_INITIAL * 2,
        leadership.ACQUIRE_BACKOFF_INITIAL * 4,
    ]
    assert sleep_intervals[2] > sleep_intervals[0]


@pytest.mark.asyncio
async def test_observing_follower_resets_acquire_failures(monkeypatch):
    monkeypatch.setattr(leadership, "_acquire_or_renew", _seq([True, False, False]))
    monkeypatch.setattr(leadership, "_release", mock.Mock())
    monkeypatch.setattr(leadership, "RENEW_INTERVAL", 0)
    monkeypatch.setattr(leadership, "ACQUIRE_BACKOFF_INITIAL", 0)
    elector = leadership.LeaderElector()

    async def failing_acquire() -> None:
        raise RuntimeError("singleton startup failed")

    task = asyncio.create_task(elector.run(failing_acquire, mock.AsyncMock()))
    await asyncio.sleep(0.03)

    assert elector.is_leader is False
    assert elector.consecutive_acquire_failures == 0

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_resign_failure_does_not_end_election(monkeypatch):
    monkeypatch.setattr(leadership, "_acquire_or_renew", _seq([True, False]))
    monkeypatch.setattr(leadership, "RENEW_INTERVAL", 0)
    resign_attempted = asyncio.Event()

    async def failing_resign() -> None:
        resign_attempted.set()
        raise RuntimeError("singleton shutdown failed")

    elector = leadership.LeaderElector()
    task = asyncio.create_task(elector.run(mock.AsyncMock(), failing_resign))
    await asyncio.wait_for(resign_attempted.wait(), timeout=0.5)
    await asyncio.sleep(0)

    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_releases_lease_on_cancel_when_leader(monkeypatch):
    monkeypatch.setattr(leadership, "_acquire_or_renew", lambda *_a: True)
    monkeypatch.setattr(leadership, "RENEW_INTERVAL", 0)
    released = mock.Mock()
    monkeypatch.setattr(leadership, "_release", released)
    elector = leadership.LeaderElector()

    task = asyncio.create_task(elector.run(mock.AsyncMock(), mock.AsyncMock()))
    await asyncio.sleep(0.03)  # let it become leader
    assert elector.is_leader is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    released.assert_called_once()
