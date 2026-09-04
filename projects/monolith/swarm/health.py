"""Advisory health for the serial Luna routine-job drainer.

This advisory is read by the private /api/health endpoint; alert wiring is
tracked in #5328.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlmodel import Session

from agent.config import DrainerSettings, load_drainer_settings


def _result(lag_seconds: float, threshold_seconds: int) -> dict:
    lag_seconds = max(0.0, lag_seconds)
    stalled = lag_seconds > threshold_seconds
    if stalled:
        reason = (
            f"oldest claimable drainer job is {lag_seconds:.0f}s overdue, "
            f"exceeds threshold {threshold_seconds} seconds"
        )
        status = "stalled"
    elif lag_seconds > 0:
        reason = f"oldest claimable drainer job is {lag_seconds:.0f}s overdue"
        status = "ok"
    else:
        reason = "no due claimable drainer jobs"
        status = "ok"
    return {
        "ok": not stalled,
        "stalled": stalled,
        "status": status,
        "lag_seconds": lag_seconds,
        "threshold_seconds": threshold_seconds,
        "reason": reason,
        "detail": reason,
    }


def _drainer_health_core(
    session: Session, job_kinds: tuple[str, ...] | list[str], threshold_seconds: int
) -> dict:
    """Compute claim lag using the routine-job claimability predicate."""
    sql = text(
        """
        SELECT EXTRACT(EPOCH FROM (now() - MIN(next_run_at))) AS lag_seconds
          FROM claude_agent.routine_jobs
         WHERE routine_kind = ANY(:kinds)
           AND next_run_at IS NOT NULL
           AND next_run_at <= now()
           AND (
                locked_by IS NULL
                OR locked_at + (ttl_secs || ' seconds')::interval < now()
           )
        """
    )
    lag_seconds = session.execute(sql, {"kinds": list(job_kinds)}).scalar_one_or_none()
    return _result(float(lag_seconds or 0.0), threshold_seconds)


def _read_drainer_health(settings: DrainerSettings) -> dict:
    from core.db import get_engine

    with Session(get_engine()) as session:
        # kg-drain jobs are deferred by design when the daily extraction cap
        # is reached, which would read as a stall here. The kg advisory owns
        # that queue's age, so this lag covers the general lane only.
        general_kinds = [kind for kind in settings.job_kinds if kind != "kg-drain"]
        return _drainer_health_core(
            session, general_kinds, settings.stall_threshold_seconds
        )


async def drainer_health() -> dict:
    settings = load_drainer_settings()
    if not settings.enabled:
        reason = "drainer disabled"
        return {
            "ok": True,
            "stalled": False,
            "status": "disabled",
            "lag_seconds": 0.0,
            "threshold_seconds": settings.stall_threshold_seconds,
            "reason": reason,
            "detail": reason,
        }
    return await asyncio.to_thread(_read_drainer_health, settings)
