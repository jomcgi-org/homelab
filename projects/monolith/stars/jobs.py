"""Stars scheduled job handlers (refresh + prune).

refresh_handler runs the met.no fetch + astronomy scoring for every site in the
stars.sites table (sourced from the light-pollution grid, ADR 006) and
wholesale-replaces site_hours rows. prune_hours_handler drops hours once their
clock hour has elapsed. In both, the network phase runs in the async handler and
the synchronous Session I/O is delegated to a worker thread (asyncio.to_thread)
so it never blocks the event loop, mirroring hikes.jobs and ships.retention. The
scheduler passes a session, but the DB work uses its own fresh session inside
the thread.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterator
from datetime import datetime, timezone

from sqlmodel import Session, delete, select

from shared.forecast_freshness import top_of_hour
from stars.forecast import fetch_all
from stars.models import Site, SiteHour

logger = logging.getLogger("monolith.stars.jobs")

# The refresh fetches, scores, and persists sites in batches of this size rather
# than accumulating the whole ~14k-site grid at once. The old whole-grid path
# (one asyncio.gather over every site, then one add_all/commit) held the entire
# scored result set plus its ORM object graph in memory simultaneously and OOMed
# the 512Mi job pod. Batching bounds peak memory to O(batch size), independent of
# the total site count, and commits each batch as it completes. The met.no rate
# limiter (~15 req/s) is unchanged and dominates wall time, so batching does not
# slow the run. Env-overridable so the batch size can be tuned without a redeploy.
REFRESH_BATCH_SIZE = int(os.environ.get("STARS_REFRESH_BATCH_SIZE", "500"))


def _load_sites() -> list[dict]:
    """Load the site list from stars.sites in a fresh session (off the loop).

    Returns the minimal shape the forecast fetch needs (id/lat/lon/altitude_m).
    """
    from core.db import get_engine

    with Session(get_engine()) as session:
        rows = session.exec(select(Site)).all()
        return [
            {
                "id": row.id,
                "lat": row.lat,
                "lon": row.lon,
                "altitude_m": row.altitude_m,
            }
            for row in rows
        ]


def _write_sites(session: Session, scored: dict[str, list[dict]], now: datetime) -> int:
    """Replace each fetched site's FUTURE hours. Returns rows written.

    Deletes only hours at or after the current clock hour for the fetched sites,
    then add_all of the new (future) scored hours, so there is no per-iteration
    session.add. Elapsed rows (hour_time < top_of_hour) are intentionally left
    in place for the hourly prune to delete, rather than wholesale-cleared here.
    Sites absent from ``scored`` (failed fetch) are untouched: stale beats empty.
    """
    site_ids = list(scored.keys())
    cutoff = top_of_hour(now)
    # synchronize_session=False: issue the DELETE as SQL rather than evaluating
    # the predicate in Python, which would compare the aware cutoff against
    # SQLite's naive datetimes in tests and raise.
    session.execute(
        delete(SiteHour)
        .where(SiteHour.site_id.in_(site_ids), SiteHour.hour_time >= cutoff)
        .execution_options(synchronize_session=False)
    )
    new_rows = [
        SiteHour(
            site_id=site_id,
            hour_time=datetime.fromisoformat(h["time"].replace("Z", "+00:00")),
            cloud_area_fraction=h["cloud_area_fraction"],
            air_temperature=h["air_temperature"],
            dew_spread=h["dew_spread"],
            sun_elevation_deg=h["sun_elevation_deg"],
            symbol=h["symbol"],
            fetched_at=now,
        )
        for site_id, hours in scored.items()
        for h in hours
    ]
    session.add_all(new_rows)
    return len(new_rows)


def _persist_sites(scored: dict[str, list[dict]]) -> int:
    """Write scored sites in a fresh session (runs off the event loop)."""
    from core.db import get_engine

    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        written = _write_sites(session, scored, now)
        session.commit()
    return written


def _prune_elapsed(session: Session) -> int:
    """Delete rows whose clock hour has elapsed. Returns rows deleted.

    The cutoff is the top of the current UTC clock hour: rows whose hour_time is
    strictly before it have elapsed. cutoff is tz-aware UTC, so the comparison
    stays tz-aware against the TIMESTAMPTZ column.

    This is pure housekeeping. The historical layer no longer comes from a live
    accumulator banked here: it comes entirely from the ERA5/CERRA climatology
    (stars.site_month_climatology, ADR 009), so the prune just drops elapsed
    forecast hours.
    """
    cutoff = top_of_hour()
    # synchronize_session=False: issue the DELETE as SQL rather than evaluating
    # the predicate in Python, which would compare the aware cutoff against
    # SQLite's naive datetimes (in tests) and raise.
    result = session.execute(
        delete(SiteHour)
        .where(SiteHour.hour_time < cutoff)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


def _run_prune() -> int:
    """Prune elapsed hours in a fresh session (runs off the event loop)."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        deleted = _prune_elapsed(session)
        session.commit()
    return deleted


def _chunks(items: list, size: int) -> Iterator[list]:
    """Yield successive ``size``-length slices of ``items`` (the last may be short)."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def refresh_handler(session: Session) -> datetime | None:
    """Refresh per-site dark-hour forecasts from met.no, in bounded batches.

    The site list is loaded from stars.sites in a worker thread, then processed
    in REFRESH_BATCH_SIZE-site batches: each batch's network fetch + scoring runs
    in the async handler and its synchronous write is delegated to a worker
    thread with its own fresh session. Batching keeps peak memory O(batch size)
    instead of accumulating the whole ~14k-site grid at once (which OOMed the
    pod), and commits each batch as it completes so a mid-run failure still lands
    the batches that finished. Sites whose fetch failed keep their previous rows
    (stale beats empty); a run where every batch fetched nothing writes nothing.
    """
    sites = await asyncio.to_thread(_load_sites)
    if not sites:
        logger.warning("stars.refresh: no sites in stars.sites, nothing to fetch")
        return None
    total_written = 0
    total_scored = 0
    for batch in _chunks(sites, REFRESH_BATCH_SIZE):
        scored = await fetch_all(batch)
        if not scored:
            continue
        total_scored += len(scored)
        total_written += await asyncio.to_thread(_persist_sites, scored)
    if total_written == 0:
        logger.warning("stars.refresh: empty fetch, keeping existing rows")
        return None
    logger.info(
        "stars.refresh ok: %d hours across %d/%d sites",
        total_written,
        total_scored,
        len(sites),
    )
    return None


async def prune_hours_handler(session: Session) -> datetime | None:
    """Drop elapsed hours (housekeeping; the read endpoint also filters)."""
    deleted = await asyncio.to_thread(_run_prune)
    logger.info("stars.prune_hours: deleted %d elapsed rows", deleted)
    return None
