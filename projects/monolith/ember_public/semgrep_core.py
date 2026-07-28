"""Core logic for the public semgrep demo: validation, admission, savings.

Mirrors the demo-postgres core in this package (rate bucket, semaphore)
but adds a small bounded waiting queue so the UI can show queueing
instead of an instant busy.
"""

import asyncio
import contextlib
import logging
import os
import time
from datetime import datetime, timezone
from time import monotonic

from sqlmodel import Session

from core.db import get_engine
from ember_public.db import get_savings_engine
from ember_public.models import DemoSgSavings

logger = logging.getLogger(__name__)

# Cold start = what the daemon pays building the warm base at startup: boot
# VM + start engine + load 1,600 rules to ready. Measured on node-4, daemon
# log 2026-07-20: `warm base built key=semgrep took=6.85s`.
COLD_START_MS = 6_850
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


def saved_ms() -> int:
    """Per-scan saving is exactly the skipped cold start: scan time itself
    is paid on both the warm and cold paths, so the credit doesn't depend
    on scan_ms."""
    return COLD_START_MS


# ---------------------------------------------------------------------------
# All-time savings counter: "scan time saved versus a hosted single-file
# scan". Like bazel_core's counter (and unlike demo_pg_savings' polled
# banked-to-banked delta), this has no state machine: every successful scan
# already knows exactly how much it saved (saved_ms(scan_ms)), so accrual is
# a direct add, called once per successful POST /scan response. Storage/
# read-cache shape mirrors demo_pg_savings and bazel_query_savings exactly
# (singleton row, writer/reader engine split, 30s single-flight cache).
# ---------------------------------------------------------------------------


def record_demo_sg_savings_core(session: Session, *, scan_ms: int) -> dict:
    """Credit one scan's outcome into the single all-time row. Sync; to_thread.
    Always writes (no throttle: this is called once per successful scan, not
    on a sub-second poll like demo_pg_savings)."""
    row = session.get(DemoSgSavings, 1)
    if row is None:
        row = DemoSgSavings(id=1, scans=0, actual_ms=0, saved_ms=0)
        session.add(row)

    row.scans += 1
    row.actual_ms += scan_ms
    row.saved_ms += saved_ms()
    session.commit()
    return {"scans": row.scans, "actual_ms": row.actual_ms, "saved_ms": row.saved_ms}


def _record_demo_sg_savings_sync(scan_ms: int) -> dict:
    """Opens its own Session against the writer engine (public_writer on the
    public tier, the default app engine on the private tier); never receives
    a session from the caller's thread."""
    with Session(get_savings_engine()) as session:
        return record_demo_sg_savings_core(session, scan_ms=scan_ms)


async def record_demo_sg_savings(scan_ms: int) -> dict | None:
    """Best-effort: a missing table (pre-migration) must not break scans."""
    try:
        return await asyncio.to_thread(_record_demo_sg_savings_sync, scan_ms)
    except Exception as exc:  # noqa: BLE001 - accrual is best-effort, never fatal
        logger.warning("demo-semgrep savings accrual failed: %s", exc)
        return None


# GET /savings: a 30s in-process cache over a plain SELECT of the singleton
# demo_sg_savings row. Reads always use the DEFAULT reader engine
# (core.db.get_engine, public_reader on the replica): SELECT works fine on the
# replica, and reserving the writer engine for accrual keeps the read path
# off the primary. Missing table (pre-migration) or any error degrades to
# scans/actual_ms/saved_ms: None, never a 5xx.

_SAVINGS_CACHE_TTL_S = 30.0
_savings_cache_lock = asyncio.Lock()
_savings_cache: dict = {
    "at": None,
    "scans": None,
    "actual_ms": None,
    "saved_ms": None,
    "as_of": None,
}


def _read_demo_sg_savings_sync() -> dict | None:
    with Session(get_engine()) as session:
        row = session.get(DemoSgSavings, 1)
        if row is None:
            return None
        return {
            "scans": row.scans,
            "actual_ms": row.actual_ms,
            "saved_ms": row.saved_ms,
        }


async def cached_demo_sg_savings() -> dict:
    """Single-flight, 30s-TTL-cached read of the all-time savings counter.

    Returns {"scans": int | None, "actual_ms": int | None, "saved_ms": int |
    None, "as_of": iso8601}; as_of is when the value was actually read from
    the DB (the cached_at timestamp), not the current time, so a
    stale-but-still-fresh cached response is honest about its age.
    """
    async with _savings_cache_lock:
        now = monotonic()
        cached_at = _savings_cache["at"]
        if cached_at is not None and (now - cached_at) < _SAVINGS_CACHE_TTL_S:
            return {
                "scans": _savings_cache["scans"],
                "actual_ms": _savings_cache["actual_ms"],
                "saved_ms": _savings_cache["saved_ms"],
                "as_of": _savings_cache["as_of"],
            }

        try:
            totals = await asyncio.to_thread(_read_demo_sg_savings_sync)
        except Exception as exc:  # noqa: BLE001 - a read failure is data, not a fault
            logger.warning("demo-semgrep savings read failed: %s", exc)
            totals = None

        as_of = datetime.now(timezone.utc).isoformat()
        _savings_cache["at"] = now
        _savings_cache["scans"] = totals["scans"] if totals else None
        _savings_cache["actual_ms"] = totals["actual_ms"] if totals else None
        _savings_cache["saved_ms"] = totals["saved_ms"] if totals else None
        _savings_cache["as_of"] = as_of
        return {
            "scans": _savings_cache["scans"],
            "actual_ms": _savings_cache["actual_ms"],
            "saved_ms": _savings_cache["saved_ms"],
            "as_of": as_of,
        }
