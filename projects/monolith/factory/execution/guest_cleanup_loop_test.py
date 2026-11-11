"""Cover guest_cleanup._loop cursor isolation and sweep failure handling."""

import asyncio
import logging
import types

import pytest

from factory import completed_probe_cleanup
from factory.execution import guest_cleanup


class _StopLoop(BaseException):
    pass


def _install(monkeypatch, guest_script, probe_script, stop_after):
    guest_args = []
    probe_args = []
    sleep_args = []
    guest_iter = iter(guest_script)
    probe_iter = iter(probe_script)

    async def fake_guest(after_id):
        guest_args.append(after_id)
        outcome = next(guest_iter)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def fake_probe(after_id):
        probe_args.append(after_id)
        outcome = next(probe_iter)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def fake_sleep(delay):
        sleep_args.append(delay)
        if len(sleep_args) >= stop_after:
            raise _StopLoop()

    monkeypatch.setattr(guest_cleanup, "sweep_once", fake_guest)
    monkeypatch.setattr(completed_probe_cleanup, "sweep_once", fake_probe)
    monkeypatch.setattr(guest_cleanup.asyncio, "sleep", fake_sleep)
    return guest_args, probe_args, sleep_args


def test_loop_cursor_isolation(monkeypatch):
    guest_args, probe_args, sleep_args = _install(monkeypatch, [16, 0, 0], [7, 0, 0], 3)
    with pytest.raises(_StopLoop):
        asyncio.run(guest_cleanup._loop())
    assert guest_args == [0, 16, 0]
    assert probe_args == [0, 7, 0]
    assert sleep_args == [guest_cleanup.INTERVAL_SECONDS] * 3


def _lock_busy_exc():
    exc = RuntimeError("pool busy")
    exc.orig = types.SimpleNamespace(sqlstate="55P03")
    return exc


@pytest.mark.parametrize(
    ("make_exc", "expected_message"),
    [
        (lambda: RuntimeError("boom"), "Guest cleanup sweep failed"),
        (_lock_busy_exc, "Guest cleanup sweep deferred: pool busy"),
    ],
)
def test_loop_guest_sweep_raises(monkeypatch, caplog, make_exc, expected_message):
    guest_args, probe_args, sleep_args = _install(
        monkeypatch, [make_exc(), 0], [0, 0], 2
    )
    with caplog.at_level(logging.INFO):
        with pytest.raises(_StopLoop):
            asyncio.run(guest_cleanup._loop())
    # Probe sweep still runs in the failing pass, and both run next pass.
    assert guest_args == [0, 0]
    assert probe_args == [0, 0]
    assert sleep_args == [guest_cleanup.INTERVAL_SECONDS] * 2
    assert expected_message in caplog.text


def test_loop_probe_sweep_raises(monkeypatch, caplog):
    guest_args, probe_args, sleep_args = _install(
        monkeypatch, [4, 0], [RuntimeError("probe boom"), 0], 2
    )
    with caplog.at_level(logging.INFO):
        with pytest.raises(_StopLoop):
            asyncio.run(guest_cleanup._loop())
    # Both sweeps still run next pass; probe cursor retained after the raise.
    assert guest_args == [0, 4]
    assert probe_args == [0, 0]
    assert sleep_args == [guest_cleanup.INTERVAL_SECONDS] * 2
    assert "Completed quota probe cleanup sweep failed" in caplog.text
