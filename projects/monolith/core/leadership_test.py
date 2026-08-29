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


@pytest.mark.asyncio
async def test_acquire_failure_does_not_kill_election(monkeypatch):
    # The first startup fails, then a later election attempt succeeds. The
    # failed attempt must clean up and release its lease before retrying.
    monkeypatch.setattr(leadership, "_acquire_or_renew", _seq([True, True, True]))
    monkeypatch.setattr(leadership, "RENEW_INTERVAL", 0)
    released = mock.Mock()
    monkeypatch.setattr(leadership, "_release", released)
    on_acquire = mock.AsyncMock(side_effect=[RuntimeError("hook failed"), None])
    on_resign = mock.AsyncMock()
    elector = leadership.LeaderElector()

    task = asyncio.create_task(elector.run(on_acquire, on_resign))
    await asyncio.sleep(0.03)
    assert elector.is_leader is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert on_acquire.await_count == 2
    on_resign.assert_awaited_once()
    assert released.call_count == 2
