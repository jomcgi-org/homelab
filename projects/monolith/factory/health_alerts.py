"""Leader-only Discord alerts on operational health transitions.

Replaces the Honeycomb triggers the free plan cannot hold (it allows one, spent
on the public /health composite). Every ``HEALTH_ALERTS_INTERVAL_S`` the leader
evaluates the ``factory.ops_health`` components and posts one Discord message
through ``shared.notify`` when a component flips unhealthy or recovers, plus a
reminder every ``HEALTH_ALERTS_RENOTIFY_S`` while it stays unhealthy.

Dedupe state lives in Postgres (``platform_probe`` rows named
``health_alert.<component>``), not in memory. Leadership moves on every
rollout, and in-memory state would re-announce every standing failure and drop
every recovery that happened across a handover. In those rows ``ok`` is the
last state announced (or silently baselined, for a healthy first sight) and
``checked_at`` is when it was announced. The row is written only after the
message is enqueued, so a failed enqueue retries on the next cycle rather than
losing the alert.

A component reporting ``status: "unknown"`` (its data could not be read) never
causes a transition: an unreadable source is neither a failure nor a recovery.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from factory import ops_health

logger = logging.getLogger(__name__)

ENABLED_ENV = "HEALTH_ALERTS_ENABLED"
INTERVAL_ENV = "HEALTH_ALERTS_INTERVAL_S"
RENOTIFY_ENV = "HEALTH_ALERTS_RENOTIFY_S"
CHANNEL_ENV = "HEALTH_ALERTS_CHANNEL_ID"
DEFAULT_INTERVAL_S = 60.0
DEFAULT_RENOTIFY_S = 6 * 3600.0
ROW_PREFIX = "health_alert."
_DETAIL_MAX = 1500


def _enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "").strip().lower() in {"1", "true", "yes"}


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        value = float(raw) if raw else default
    except ValueError:
        logger.warning("health alerts: %s=%r is not a number", name, raw)
        return default
    return value if value > 0 else default


def _read_sync(names: list[str]) -> dict[str, tuple[bool, datetime]]:
    from core.db import get_engine
    from core.platform_probe import PlatformProbe
    from sqlmodel import Session, select

    keys = [ROW_PREFIX + name for name in names]
    with Session(get_engine()) as session:
        rows = session.exec(
            select(PlatformProbe).where(PlatformProbe.name.in_(keys))
        ).all()
        return {
            row.name.removeprefix(ROW_PREFIX): (row.ok, row.checked_at) for row in rows
        }


def _write_sync(name: str, ok: bool, detail: str, now: datetime) -> None:
    from core.db import get_engine
    from core.platform_probe import PlatformProbe
    from sqlmodel import Session

    with Session(get_engine()) as session:
        row = session.get(PlatformProbe, ROW_PREFIX + name)
        if row is None:
            row = PlatformProbe(
                name=ROW_PREFIX + name, ok=ok, detail="", checked_at=now
            )
        row.ok = ok
        row.detail = detail[:_DETAIL_MAX]
        row.checked_at = now
        if ok:
            row.last_ok_at = now
        session.add(row)
        session.commit()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _hours(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.0f}h" if hours >= 1 else f"{delta.total_seconds() / 60:.0f}m"


def decide(
    previous: tuple[bool, datetime] | None,
    result: dict,
    now: datetime,
    renotify: timedelta,
) -> tuple[str | None, bool]:
    """(message kind or None, whether to write the row) for one component.

    Kinds: "unhealthy" (flip to failing, or failing on first sight),
    "recovered", "still" (renotify). A healthy first sight is baselined
    silently.
    """
    if result.get("status") == "unknown":
        return None, False
    ok = bool(result.get("ok"))
    if previous is None:
        return (None if ok else "unhealthy"), True
    was_ok, announced_at = previous
    if was_ok and not ok:
        return "unhealthy", True
    if not was_ok and ok:
        return "recovered", True
    if not ok and now - _utc(announced_at) >= renotify:
        return "still", True
    return None, False


def message(
    kind: str, name: str, detail: str, since: datetime | None, now: datetime
) -> str:
    detail = detail or "no detail"
    if kind == "unhealthy":
        return f"Health alert: {name} is unhealthy. {detail}"
    if kind == "recovered":
        return f"Health recovered: {name} is healthy again. {detail}"
    ago = f" (last notice {_hours(now - _utc(since))} ago)" if since else ""
    return f"Health alert: {name} is still unhealthy{ago}. {detail}"


async def _one(name, check, previous, now, renotify, channel) -> str | None:
    from shared.notify import notify

    result = await check()
    stamp = now or datetime.now(timezone.utc)
    kind, write = decide(previous, result, stamp, renotify)
    detail = str(result.get("detail") or "")
    text = None
    if kind is not None:
        since = previous[1] if previous is not None else None
        text = message(kind, name, detail, since, stamp)
        await notify(
            text, level="info" if kind == "recovered" else "warn", channel=channel
        )
    if write:
        await asyncio.to_thread(
            _write_sync, name, bool(result.get("ok")), detail, stamp
        )
    return text


async def run_cycle(checks=None, *, now: datetime | None = None) -> list[str]:
    """Evaluate every component once; return the messages enqueued."""
    checks = checks or ops_health.CHECKS
    renotify = timedelta(seconds=_float_env(RENOTIFY_ENV, DEFAULT_RENOTIFY_S))
    channel = os.environ.get(CHANNEL_ENV, "").strip() or None
    previous = await asyncio.to_thread(_read_sync, list(checks))
    sent = []
    for name, check in checks.items():
        try:  # nosemgrep: no-broad-except-swallow - one component must not block the rest
            text = await _one(name, check, previous.get(name), now, renotify, channel)
        except Exception:
            logger.exception("health alert for %s failed; retrying next cycle", name)
            continue
        if text is not None:
            sent.append(text)
    return sent


async def _loop() -> None:
    interval = _float_env(INTERVAL_ENV, DEFAULT_INTERVAL_S)
    while True:
        try:  # nosemgrep: no-broad-except-swallow - a dead loop is worse, logged here
            await run_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("health alert cycle failed")
        await asyncio.sleep(interval)


def start_health_alert_loop() -> list[asyncio.Task]:
    if not _enabled():
        return []
    from framework import log_task_exception

    task = asyncio.create_task(_loop(), name="factory-health-alerts")
    task.add_done_callback(log_task_exception)
    return [task]
