"""Ships traffic-density heatmap rollup.

Recomputes ``ships.heat_cells`` from ``ships.positions`` (the 7-day partitioned
window): one row per occupied ~500m grid cell holding the count of DISTINCT
vessels that MOVED through it.

Two deliberate choices make the map show *traffic* rather than *dwell*:

  - "Moved" = the vessel's speed-over-ground exceeded ``MIN_SPEED_KN`` at some
    point in the window. This keeps anchored-but-real traffic (a cargo ship
    waiting in the bay arrives and departs, so it moved) while dropping
    permanently-fixed small craft that never move.
  - ``count(distinct mmsi)`` (not raw fix count) so a single vessel reporting
    every couple of minutes for days counts once per cell, not thousands of
    times. An anchorage then reads as "how many different ships used it".

Validated against live data before building: ~9k occupied cells, counts 1-22,
hot cells landing on real ferry lanes and harbour approaches.

This module is split like ``retention.py``:

  - Pure helpers (cell steps, the rollup SQL builder) are unit-tested.
  - A thin handler runs the live ``DELETE`` + ``INSERT`` off the event loop. The
    aggregation is Postgres-only and is exercised in prod / CI-against-Postgres,
    never in the SQLite unit tests.
"""

import asyncio
import logging
import os
from datetime import date, datetime, timedelta

from sqlmodel import Session, text

logger = logging.getLogger("monolith.ships")

# ~500m square cells at Salish Sea latitude (~48N). A degree of longitude is
# shorter than a degree of latitude there, so the lon step is larger to keep
# cells roughly square. The serving layer reconstructs each cell polygon as
# [lat_bin*LAT_STEP, lon_bin*LON_STEP] .. [+LAT_STEP, +LON_STEP].
LAT_STEP = float(os.environ.get("SHIPS_HEAT_LAT_STEP", "0.005"))
LON_STEP = float(os.environ.get("SHIPS_HEAT_LON_STEP", "0.0075"))

# A vessel counts as real traffic (not a fixed object) if its speed exceeds this
# at any point in the window. Env-tunable so the cut can be adjusted live.
MIN_SPEED_KN = float(os.environ.get("SHIPS_HEAT_MIN_SPEED_KN", "1.0"))

# Trailing window. Matches positions retention (SHIPS_RETENTION_DAYS).
WINDOW_DAYS = int(os.environ.get("SHIPS_HEAT_WINDOW_DAYS", "7"))


def rollup_insert_sql(
    lat_step: float, lon_step: float, min_speed: float, window_days: int
) -> str:
    """Build the INSERT...SELECT that fills heat_cells from positions.

    All interpolated values are floats/ints derived from module constants (env),
    never user input, so the f-string cannot carry SQL injection. (Flagged to
    preempt the semgrep raw-SQL rule, mirrors retention.py.)
    """
    return (
        "INSERT INTO ships.heat_cells (lat_bin, lon_bin, count) "
        "WITH movers AS ("
        "  SELECT mmsi FROM ships.positions"
        f"  WHERE recorded_at >= now() - interval '{window_days} days'"
        f"  GROUP BY mmsi HAVING max(speed) >= {min_speed}"
        ") "
        f"SELECT floor(p.lat / {lat_step})::int, floor(p.lon / {lon_step})::int, "
        "       count(distinct p.mmsi) "
        "FROM ships.positions p JOIN movers USING (mmsi) "
        f"WHERE p.recorded_at >= now() - interval '{window_days} days' "
        "  AND p.lat BETWEEN -90 AND 90 AND p.lon BETWEEN -180 AND 180 "
        "  AND NOT (p.lat = 0 AND p.lon = 0) "
        "GROUP BY 1, 2"
    )


def bank_day_sql(day: date, lat_step: float, lon_step: float, min_speed: float) -> str:
    """Build the INSERT...SELECT that banks ONE day's per-cell distinct-mover
    counts into ships.heat_cells_historical, additively.

    Reads the day's rows from the PARENT partitioned table by recorded_at range
    (Postgres prunes to the one partition). After that partition is dropped this
    SELECT returns zero rows, so re-running adds nothing: the bank is idempotent
    and is therefore safe to run in the same transaction as the DROP and to
    re-attempt on later maintenance passes.

    day, steps and min_speed are derived from a date / module env constants,
    never user input, so the f-string cannot carry SQL injection. (Flagged to
    preempt the semgrep raw-SQL rule, mirrors rollup_insert_sql.)
    """
    lo = day.isoformat()
    hi = (day + timedelta(days=1)).isoformat()
    return (
        "INSERT INTO ships.heat_cells_historical (lat_bin, lon_bin, count) "
        "WITH movers AS ("
        "  SELECT mmsi FROM ships.positions"
        f"  WHERE recorded_at >= '{lo}' AND recorded_at < '{hi}'"
        f"  GROUP BY mmsi HAVING max(speed) >= {min_speed}"
        ") "
        f"SELECT floor(p.lat / {lat_step})::int, floor(p.lon / {lon_step})::int, "
        "       count(distinct p.mmsi) "
        "FROM ships.positions p JOIN movers USING (mmsi) "
        f"WHERE p.recorded_at >= '{lo}' AND p.recorded_at < '{hi}' "
        "  AND p.lat BETWEEN -90 AND 90 AND p.lon BETWEEN -180 AND 180 "
        "  AND NOT (p.lat = 0 AND p.lon = 0) "
        "GROUP BY 1, 2 "
        "ON CONFLICT (lat_bin, lon_bin) DO UPDATE "
        "SET count = ships.heat_cells_historical.count + EXCLUDED.count, "
        "    updated_at = now()"
    )


def _run_heat_rollup() -> None:
    """Synchronous full-replace rollup; own session, run off the event loop.

    DELETE + INSERT in one transaction so readers of heat_cells never see an
    empty table mid-rebuild (MVCC keeps the old rows visible until commit).
    """
    from core.db import get_engine

    insert_sql = rollup_insert_sql(LAT_STEP, LON_STEP, MIN_SPEED_KN, WINDOW_DAYS)
    with Session(get_engine()) as session:
        try:
            session.execute(text("DELETE FROM ships.heat_cells"))
            session.execute(text(insert_sql))
            session.commit()
            logger.info(
                "ships heat rollup ok (window=%dd, min_speed=%.1f)",
                WINDOW_DAYS,
                MIN_SPEED_KN,
            )
        except Exception:
            logger.exception("ships heat rollup failed")
            session.rollback()


async def heat_rollup_handler(session: Session) -> datetime | None:
    """Recompute the traffic-density heatmap rollup.

    Delegates the synchronous aggregation to a worker thread so it never blocks
    the event loop. The scheduler passes a session, but the work uses its own
    session inside the thread (mirrors partition_maintenance_handler).
    """
    await asyncio.to_thread(_run_heat_rollup)
    return None
