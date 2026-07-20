"""Core logic for the public semgrep demo: validation, admission, savings.

Mirrors the demo-postgres core in this package (rate bucket, semaphore)
but adds a small bounded waiting queue so the UI can show queueing
instead of an instant busy.
"""

import asyncio
import contextlib
import os
import time

HOSTED_SCAN_MEDIAN_MS = 11_000
MAX_LINES = 200
MAX_CHARS = 8_000
LANGUAGES = {"python": "snippet.py", "javascript": "snippet.js"}

_SCAN_MIN_INTERVAL_S = 3.0
_BUCKET_MAX_AGE_S = 3600.0
_scan_buckets: dict[str, float] = {}


class QueueFullError(Exception):
    """Raised when every slot and every waiting position is taken."""


def validate_snippet(language: str, content: str) -> str | None:
    if language not in LANGUAGES:
        return "language must be python or javascript"
    if len(content) > MAX_CHARS:
        return f"snippet is limited to {MAX_CHARS} characters"
    if content.count("\n") + 1 > MAX_LINES:
        return f"snippet is limited to {MAX_LINES} lines"
    if not content.strip():
        return "snippet is empty"
    return None


def snippet_path(language: str) -> str:
    return LANGUAGES[language]


def check_and_record_scan(session_tag: str) -> bool:
    """One scan per session per _SCAN_MIN_INTERVAL_S. Prunes stale tags."""
    now = time.monotonic()
    for tag, ts in list(_scan_buckets.items()):
        if now - ts > _BUCKET_MAX_AGE_S:
            del _scan_buckets[tag]
    last = _scan_buckets.get(session_tag)
    if last is not None and now - last < _SCAN_MIN_INTERVAL_S:
        return False
    _scan_buckets[session_tag] = now
    return True


class _BoundedQueue:
    """Semaphore with a hard cap on waiters, so admission is bounded."""

    def __init__(self, slots: int, max_waiters: int) -> None:
        self._sem = asyncio.Semaphore(slots)
        self._max_waiters = max_waiters
        self._waiters = 0

    @property
    def waiting(self) -> int:
        return self._waiters

    @contextlib.asynccontextmanager
    async def slot(self):
        if self._sem.locked() and self._waiters >= self._max_waiters:
            raise QueueFullError
        self._waiters += 1
        try:
            await self._sem.acquire()
        finally:
            self._waiters -= 1
        try:
            yield
        finally:
            self._sem.release()


def _make_queue(slots: int, max_waiters: int) -> _BoundedQueue:
    return _BoundedQueue(slots=slots, max_waiters=max_waiters)


QUEUE = _make_queue(
    slots=int(os.environ.get("EMBER_SEMGREP_MAX_CONCURRENT", "3")),
    max_waiters=int(os.environ.get("EMBER_SEMGREP_MAX_WAITERS", "8")),
)


def saved_ms(scan_ms: int) -> int:
    return max(0, HOSTED_SCAN_MEDIAN_MS - scan_ms)
