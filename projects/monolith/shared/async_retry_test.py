"""Retry execution uses prospective sleep accounting and preserves exceptions."""

import asyncio

import pytest

from shared.async_retry import async_retry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failures,budget,base,cap,expected_attempts,delays",
    [
        (2, 300, 2, 30, 3, [2, 4]),
        (12, 300, 2, 30, 12, [2, 4, 8, 16] + [30] * 8),
        (12, 0, 2, 30, 1, []),
        (12, 3, 2, 30, 2, [2]),
        (12, 6, 2, 30, 3, [2, 4]),
        (4, 300, 3, 5, 5, [3, 5, 5, 5]),
    ],
)
async def test_execution(failures, budget, base, cap, expected_attempts, delays):
    errors = [RuntimeError(str(i)) for i in range(failures)]
    events = []
    attempts = 0
    result = object()

    async def operation():
        nonlocal attempts
        attempts += 1
        events.append(("operation", attempts))
        if attempts <= failures:
            raise errors[attempts - 1]
        return result

    def log(attempt, delay, exc):
        events.append(("log", attempt, delay, exc))

    async def sleep(delay):
        events.append(("sleep", delay))

    call = async_retry(
        operation,
        is_retryable=lambda exc: isinstance(exc, RuntimeError),
        max_attempts=12,
        base_delay=base,
        max_delay=cap,
        sleep_budget=budget,
        on_retry=log,
        sleep=sleep,
    )
    if failures < expected_attempts:
        assert await call is result
    else:
        with pytest.raises(RuntimeError) as caught:
            await call
        assert caught.value is errors[expected_attempts - 1]
    assert attempts == expected_attempts
    assert [e[1] for e in events if e[0] == "sleep"] == delays
    logs = [e for e in events if e[0] == "log"]
    assert logs == [("log", i + 1, delay, errors[i]) for i, delay in enumerate(delays)]
    for event_index, event in enumerate(events):
        if event[0] == "sleep":
            assert events[event_index - 1][0] == "log"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["permanent", "operation", "sleep"])
async def test_permanent_and_cancellation(failure_at):
    error = (
        ValueError("permanent")
        if failure_at == "permanent"
        else asyncio.CancelledError("cancelled")
    )
    attempts = []
    sleeps = []
    predicates = []
    logs = []

    async def operation():
        attempts.append(1)
        if failure_at == "sleep":
            raise RuntimeError("retryable")
        raise error

    def predicate(exc):
        predicates.append(exc)
        return isinstance(exc, RuntimeError)

    async def sleep(delay):
        sleeps.append(delay)
        raise error

    with pytest.raises(type(error)) as caught:
        await async_retry(
            operation,
            is_retryable=predicate,
            max_attempts=12,
            base_delay=2,
            max_delay=30,
            sleep_budget=300,
            on_retry=lambda *args: logs.append(args),
            sleep=sleep,
        )
    assert caught.value is error
    assert attempts == [1]
    assert sleeps == ([2] if failure_at == "sleep" else [])
    assert len(logs) == (1 if failure_at == "sleep" else 0)
    assert len(predicates) == (0 if failure_at == "operation" else 1)
