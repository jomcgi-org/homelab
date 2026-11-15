"""Execute an async operation with caller-owned retry policy and logging."""

from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def async_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    is_retryable: Callable[[Exception], bool],
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    sleep_budget: float,
    on_retry: Callable[[int, float, Exception], None],
    sleep: Callable[[float], Awaitable[None]],
) -> T:
    """Preserve prospective sleep accounting, including the final attempt's sleep.

    The callback receives the one-based attempt, delay and exception only after
    the proposed cumulative sleep passes the budget check. Request resources
    belong to operation and must be released before it returns or raises.
    """
    elapsed = 0.0
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await operation()
        except Exception as exc:
            last_exc = exc
            if not is_retryable(exc):
                raise

            delay = min(base_delay * (2**attempt), max_delay)
            elapsed += delay
            if elapsed > sleep_budget:
                break

            on_retry(attempt + 1, delay, exc)
            await sleep(delay)

    raise last_exc  # type: ignore[misc]
