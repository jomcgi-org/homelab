"""Cross-replica record of EmberVM session-create outcomes.

Session creates run on whichever replica claimed the turn, and a failing create
is retried in-process (the capacity ladder in transport.py) without writing
anything durable until the ladder gives up nineteen minutes later. The
``embervm_capacity`` health component needs "creates have been failing
continuously for more than 15 minutes" sooner than that and from the leader,
so every replica records each create attempt's outcome in two
``platform_probe`` latch rows:

- ``embervm.session_create``: the latest outcome. ``ok`` and ``detail`` are
  the latest attempt, ``checked_at`` its time, ``last_ok_at`` the latest
  successful create (the usual latch semantics).
- ``embervm.session_create.failing_since``: written only when the latest
  outcome flips from success (or nothing) to failure, so its ``checked_at`` is
  when the current failure streak began.

Only outcomes that say something about EmberVM's ability to place a session
are recorded: a success, a 429 capacity denial, a 5xx, a timeout, a transport
error or a malformed success body. Other 4xx answers (a restore denial, an
unknown workload) are about the caller's request and are not recorded.

Recording is best effort and off the create path: it is scheduled as a
background thread write, never awaited by the caller, and never raises. It is
gated on ``EMBERVM_CREATE_OUTCOME_LATCH_ENABLED`` so tests and binaries without
the platform_probe table do not try to write it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LATEST = "embervm.session_create"
FAILING_SINCE = "embervm.session_create.failing_since"
ENABLED_ENV = "EMBERVM_CREATE_OUTCOME_LATCH_ENABLED"
_DETAIL_MAX = 300

_pending: set[asyncio.Task] = set()


def enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "").strip().lower() in {"1", "true", "yes"}


def _record_sync(ok: bool, detail: str, now: datetime | None = None) -> None:
    from core.db import get_engine
    from core.platform_probe import PlatformProbe
    from sqlmodel import Session

    now = now or datetime.now(timezone.utc)
    detail = detail[:_DETAIL_MAX]
    with Session(get_engine()) as session:
        row = session.get(PlatformProbe, LATEST, with_for_update=True)
        if row is None:
            streak_started = not ok
            row = PlatformProbe(
                name=LATEST,
                ok=ok,
                detail=detail,
                checked_at=now,
                last_ok_at=now if ok else None,
            )
        else:
            streak_started = (not ok) and row.ok
            row.ok = ok
            row.detail = detail
            row.checked_at = now
            if ok:
                row.last_ok_at = now
        session.add(row)
        if streak_started:
            streak = session.get(PlatformProbe, FAILING_SINCE)
            if streak is None:
                streak = PlatformProbe(
                    name=FAILING_SINCE, ok=False, detail=detail, checked_at=now
                )
            else:
                streak.ok = False
                streak.detail = detail
                streak.checked_at = now
            session.add(streak)
        session.commit()


def _done(task: asyncio.Task) -> None:
    _pending.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("session create outcome not recorded: %s", exc)


def record(ok: bool, detail: str) -> None:
    """Schedule one outcome write. Never raises and never blocks the caller."""
    if not enabled():
        return
    try:  # nosemgrep: no-broad-except-swallow - telemetry must not fail a create
        loop = asyncio.get_running_loop()
        task = loop.create_task(asyncio.to_thread(_record_sync, ok, detail))
    except Exception as exc:  # noqa: BLE001
        logger.warning("session create outcome not scheduled: %s", exc)
        return
    _pending.add(task)
    task.add_done_callback(_done)
