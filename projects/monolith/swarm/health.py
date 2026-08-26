"""Advisory health for the serial qwen routine-job drainer."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlmodel import Session

from agent.config import DrainerSettings, load_drainer_settings


def _result(lag_seconds: float, threshold_seconds: int) -> dict:
    lag_seconds = max(0.0, lag_seconds)
    stalled = lag_seconds > threshold_seconds
    if stalled:
        reason = f"claimable drainer job is {lag_seconds:.0f}s overdue"
        status = "stalled"
    elif lag_seconds:
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
    session: Session, job_kind: str, threshold_seconds: int
) -> dict:
    """Compute claim lag using the routine-job claimability predicate."""
    if session.get_bind().dialect.name == "sqlite":
        sql = text(
            """
            SELECT (julianday(CURRENT_TIMESTAMP) - julianday(MIN(next_run_at)))
                   * 86400.0 AS lag_seconds
              FROM routine_jobs
             WHERE routine_kind = :kind
               AND next_run_at IS NOT NULL
               AND next_run_at <= CURRENT_TIMESTAMP
               AND (
                    locked_by IS NULL
                    OR datetime(locked_at, '+' || ttl_secs || ' seconds')
                       < CURRENT_TIMESTAMP
               )
            """
        )
    else:
        sql = text(
            """
            SELECT EXTRACT(EPOCH FROM (now() - MIN(next_run_at))) AS lag_seconds
              FROM claude_agent.routine_jobs
             WHERE routine_kind = :kind
               AND next_run_at IS NOT NULL
               AND next_run_at <= now()
               AND (
                    locked_by IS NULL
                    OR locked_at + (ttl_secs || ' seconds')::interval < now()
               )
            """
        )
    lag_seconds = session.execute(sql, {"kind": job_kind}).scalar_one_or_none()
    return _result(float(lag_seconds or 0.0), threshold_seconds)


def _read_drainer_health(settings: DrainerSettings) -> dict:
    from core.db import get_engine

    with Session(get_engine()) as session:
        return _drainer_health_core(
            session, settings.job_kind, settings.stall_threshold_seconds
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
