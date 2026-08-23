"""Ships partition maintenance and retention.

``ships.positions`` is range-partitioned by ``recorded_at`` (one partition per
day). Retention is enforced by DROPPING whole daily partitions rather than
DELETE-ing rows, so there is zero vacuum churn on the shared Postgres.

This module is split into:

  - Pure helpers (``partition_name``, ``partition_bounds``,
    ``partitions_to_create``, ``partitions_to_drop``) and pure DDL builders
    (``create_partition_sql``, ``drop_partition_sql``). These are platform
    independent and unit-tested in ``retention_test.py``.
  - A thin ``partition_maintenance_handler`` that executes the DDL. Partition
    DDL is Postgres-only and cannot run on SQLite, so the live DDL is exercised
    in prod / CI-against-Postgres, never in the SQLite unit tests.
"""

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone

from sqlmodel import Session, text

from ships.heat import LAT_STEP, LON_STEP, MIN_SPEED_KN, bank_day_sql

logger = logging.getLogger("monolith.ships")

# Keep position data for this many days. A partition is only dropped once its
# WHOLE day is older than this window.
RETENTION_DAYS = int(os.environ.get("SHIPS_RETENTION_DAYS", "7"))

# Create partitions for today plus this many days ahead, so inserts always have
# a home even if the job is delayed by up to PARTITION_AHEAD_DAYS.
PARTITION_AHEAD_DAYS = int(os.environ.get("SHIPS_PARTITION_AHEAD_DAYS", "2"))

# How many days past the retention boundary to scan for droppable partitions.
# This lets the job clean a window of old partitions even if it missed several
# daily runs, without scanning all of history.
DROP_SCAN_DAYS = 30


def partition_name(day: date) -> str:
    """Stable, collision-free partition name for a given day."""
    return f"positions_{day:%Y%m%d}"


def partition_bounds(day: date) -> tuple[str, str]:
    """Range bounds for a daily partition: FROM inclusive, TO exclusive.

    Returns ISO date strings ``(day, day + 1)``.
    """
    return (day.isoformat(), (day + timedelta(days=1)).isoformat())


def partitions_to_create(today: date, ahead_days: int) -> list[date]:
    """Days that should have partitions: today through today + ahead_days."""
    return [today + timedelta(days=i) for i in range(ahead_days + 1)]


def partitions_to_drop(today: date, retention_days: int, scan_days: int) -> list[date]:
    """Days whose partitions are safe to drop.

    A partition for ``day`` holds rows with ``recorded_at`` in
    ``[day, day + 1)``. We keep any data recorded within the last
    ``retention_days`` days, i.e. with ``recorded_at >= today - retention_days``.

    The partition for ``day`` is safe to drop only when its ENTIRE range is
    older than that cutoff, i.e. its exclusive upper bound ``day + 1`` is at or
    before the cutoff::

        day + 1 <= today - retention_days
        => day <= today - retention_days - 1

    So the newest droppable day is ``today - (retention_days + 1)``. The
    partition for ``today - retention_days`` is the boundary partition: it still
    contains ``recorded_at == today - retention_days``, which IS within
    retention, so it is NEVER dropped.

    We scan back ``scan_days`` further so a window of old partitions is cleaned
    even if the job missed runs.
    """
    oldest = today - timedelta(days=retention_days + scan_days)
    newest_droppable = today - timedelta(days=retention_days + 1)
    return [
        oldest + timedelta(days=i) for i in range((newest_droppable - oldest).days + 1)
    ]


def create_partition_sql(day: date) -> str:
    """Idempotent CREATE for a single daily partition.

    The day is derived internally from a ``date`` value, never from user input,
    so the f-string interpolation here cannot carry a SQL injection. (Flagged to
    preempt the semgrep raw-SQL rule.)
    """
    name = partition_name(day)
    lo, hi = partition_bounds(day)
    return (
        f"CREATE TABLE IF NOT EXISTS ships.{name} "
        f"PARTITION OF ships.positions FOR VALUES FROM ('{lo}') TO ('{hi}')"
    )


def drop_partition_sql(day: date) -> str:
    """Idempotent DROP for a single daily partition.

    The day is derived internally from a ``date`` value, never from user input,
    so the f-string interpolation here cannot carry a SQL injection. (Flagged to
    preempt the semgrep raw-SQL rule.)
    """
    return f"DROP TABLE IF EXISTS ships.{partition_name(day)}"


def _run_partition_maintenance() -> None:
    """Synchronous partition DDL, run off the event loop via asyncio.to_thread."""
    from core.db import get_engine

    today = datetime.now(timezone.utc).date()
    with Session(get_engine()) as session:
        try:
            for day in partitions_to_create(today, PARTITION_AHEAD_DAYS):
                session.execute(text(create_partition_sql(day)))
            dropped = partitions_to_drop(today, RETENTION_DAYS, DROP_SCAN_DAYS)
            for day in dropped:
                # Bank the day's per-cell counts into the all-time accumulator
                # BEFORE dropping the partition, in this same transaction. The
                # bank reads the day's slice from the parent table by range, so a
                # retry after the drop reads zero rows (idempotent).
                session.execute(
                    text(bank_day_sql(day, LAT_STEP, LON_STEP, MIN_SPEED_KN))
                )
                session.execute(text(drop_partition_sql(day)))
            session.commit()
            logger.info(
                "ships partition maintenance ok (retention=%dd, banked+dropped %d days)",
                RETENTION_DAYS,
                len(dropped),
            )
        except Exception:
            logger.exception("ships partition maintenance failed")
            session.rollback()


async def partition_maintenance_handler(session: Session) -> datetime | None:
    """Create daily partitions ahead and drop partitions past retention.

    Delegates the synchronous DDL to a worker thread so it never blocks the
    event loop (mirrors the ingest flush pattern). The scheduler passes a
    session, but the DDL uses its own session inside the thread.
    """
    await asyncio.to_thread(_run_partition_maintenance)
    return None
