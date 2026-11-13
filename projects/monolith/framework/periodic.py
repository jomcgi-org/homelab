"""Serial periodic cycles with caller-owned configuration and error reporting."""

import asyncio
from collections.abc import Awaitable, Callable


async def run_periodic(
    cycle: Callable[[], Awaitable[object]],
    interval: float,
    report_failure: Callable[[], object],
) -> None:
    """Run immediately, then delay after each completed or failed cycle."""
    while True:
        try:  # nosemgrep: no-broad-except-swallow - caller reports failures before retry
            await cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            report_failure()
        await asyncio.sleep(interval)
