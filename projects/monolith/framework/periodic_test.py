"""Deterministic cadence, reporting and cancellation contracts."""

import asyncio
import logging
import sys

import pytest

from framework import periodic


@pytest.mark.parametrize("fail_first", [False, True])
def test_immediate_serial_cycles_delay_after_success_and_failure(
    monkeypatch, caplog, fail_first
):
    async def scenario():
        entered = asyncio.Queue()
        release = asyncio.Queue()
        delays = asyncio.Queue()
        wake = asyncio.Queue()
        trace = []
        active = 0
        calls = 0
        failure = RuntimeError("cycle failed")
        reported = []
        logger = logging.getLogger(__name__)

        async def cycle():
            nonlocal active, calls
            active += 1
            calls += 1
            assert active == 1
            trace.append("cycle")
            entered.put_nowait(calls)
            try:
                await release.get()
                if calls == 1 and fail_first:
                    raise failure
                return ["ignored cycle result"]
            finally:
                active -= 1

        async def sleep(interval):
            assert active == 0
            trace.append("sleep")
            delays.put_nowait(interval)
            await wake.get()

        def report_failure():
            reported.append(sys.exception())
            logger.exception("original cycle message")
            return "ignored reporting result"

        monkeypatch.setattr(periodic.asyncio, "sleep", sleep)
        task = asyncio.create_task(periodic.run_periodic(cycle, 17.25, report_failure))
        try:
            # No sleep or timer is released before the first cycle starts.
            assert await asyncio.wait_for(entered.get(), 1) == 1
            assert trace == ["cycle"]
            release.put_nowait(None)
            assert await asyncio.wait_for(delays.get(), 1) == 17.25
            assert calls == 1
            wake.put_nowait(None)
            assert await asyncio.wait_for(entered.get(), 1) == 2
            release.put_nowait(None)
            assert await asyncio.wait_for(delays.get(), 1) == 17.25
            assert calls == 2
            assert trace == ["cycle", "sleep", "cycle", "sleep"]
            assert reported == ([failure] if fail_first else [])
            records = [r for r in caplog.records if r.name == __name__]
            if fail_first:
                assert len(records) == 1
                assert records[0].message == "original cycle message"
                assert records[0].exc_info[1] is failure
                assert records[0].exc_info[2] is not None
            else:
                assert records == []
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["cycle", "sleep"])
def test_cancellation_propagates_without_reporting_or_another_cycle(monkeypatch, phase):
    async def scenario():
        blocked = asyncio.Event()
        calls = []
        delays = []
        reported = []
        finished = []

        async def block():
            blocked.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(phase)

        async def cycle():
            calls.append(1)
            if phase == "cycle":
                await block()

        async def sleep(interval):
            delays.append(interval)
            await block()

        monkeypatch.setattr(periodic.asyncio, "sleep", sleep)
        task = asyncio.create_task(
            periodic.run_periodic(cycle, 9.5, lambda: reported.append(1))
        )
        try:
            await asyncio.wait_for(blocked.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert task.cancelled()
            assert calls == [1]
            assert delays == ([] if phase == "cycle" else [9.5])
            assert reported == []
            assert finished == [phase]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
