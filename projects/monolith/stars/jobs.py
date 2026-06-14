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
from datetime import datetime, timezone

from sqlmodel import Session, delete, select

from shared.forecast_freshness import top_of_hour
from stars.forecast import fetch_all
from stars.models import Site, SiteHour, SiteMonthStat
from stars.scoring import is_clear_dark_hour

logger = logging.getLogger("monolith.stars.jobs")


def _load_sites() -> list[dict]:
    """Load the site list from stars.sites in a fresh session (off the loop).

    Returns the minimal shape the forecast fetch needs (id/lat/lon/altitude_m).
    """
    from app.db import get_engine

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
    in place: the hourly prune is the sole remover of elapsed hours and banks
    them into stars.site_month_stats exactly once before deleting (ADR 008). A
    wholesale delete here would drop those rows before the prune could bank
    them. Sites absent from ``scored`` (failed fetch) are untouched: stale beats
    empty.
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
    from app.db import get_engine

    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        written = _write_sites(session, scored, now)
        session.commit()
    return written


def _prune_elapsed(session: Session) -> int:
    """Bank then delete rows whose clock hour has elapsed. Returns rows deleted.

    The cutoff is the top of the current UTC clock hour: rows whose hour_time is
    strictly before it have elapsed. cutoff is tz-aware UTC, so the comparison
    stays tz-aware against the TIMESTAMPTZ column.

    Exactly-once banking: this is the SOLE remover of elapsed hours, so each
    elapsing hour is accumulated into stars.site_month_stats once, then deleted.
    Every elapsing row is a dark hour (forecast keeps only sun < -12), so each
    increments dark_hours; the subset that is also clear (cloud < 10%) increments
    clear_dark_hours. The aggregation is done in Python (grouping by the hour's
    month-of-year) rather than a Postgres ``extract()`` / ``ON CONFLICT`` upsert
    so the same core is portable to the SQLite create_all test fixtures. The
    elapsing set is small (one clock hour's worth), so the loop is cheap.
    """
    cutoff = top_of_hour()
    elapsing = session.exec(select(SiteHour).where(SiteHour.hour_time < cutoff)).all()

    # Accumulate the per-(site, month-of-year) clear-dark counts in memory.
    buckets: dict[tuple[str, int], dict[str, int]] = {}
    for row in elapsing:
        key = (row.site_id, row.hour_time.month)
        agg = buckets.setdefault(key, {"dark": 0, "clear": 0})
        agg["dark"] += 1
        if is_clear_dark_hour(row.sun_elevation_deg, row.cloud_area_fraction):
            agg["clear"] += 1

    # Bank each bucket: increment the existing month row in place, or add a new
    # one. New rows are collected and add_all'd once (no session.add in a loop).
    new_rows: list[SiteMonthStat] = []
    for (site_id, month), agg in buckets.items():
        stat = session.get(SiteMonthStat, (site_id, month))
        if stat is None:
            new_rows.append(
                SiteMonthStat(
                    site_id=site_id,
                    month=month,
                    dark_hours=agg["dark"],
                    clear_dark_hours=agg["clear"],
                )
            )
        else:
            stat.dark_hours += agg["dark"]
            stat.clear_dark_hours += agg["clear"]
    if new_rows:
        session.add_all(new_rows)

    # synchronize_session=False: the rows were just loaded to bank them, so an
    # in-Python evaluation of the predicate would compare the aware cutoff
    # against SQLite's naive datetimes (in tests) and raise. Issue the SQL delete.
    result = session.execute(
        delete(SiteHour)
        .where(SiteHour.hour_time < cutoff)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


def _run_prune() -> int:
    """Prune elapsed hours in a fresh session (runs off the event loop)."""
    from app.db import get_engine

    with Session(get_engine()) as session:
        deleted = _prune_elapsed(session)
        session.commit()
    return deleted


async def refresh_handler(session: Session) -> datetime | None:
    """Refresh per-site dark-hour forecasts from met.no.

    The site list is loaded from stars.sites in a worker thread; the network
    fetch + scoring runs in the async handler; the synchronous write is
    delegated to a worker thread with its own session. Sites whose fetch failed
    keep their previous rows (stale beats empty); a total fetch failure writes
    nothing.
    """
    sites = await asyncio.to_thread(_load_sites)
    if not sites:
        logger.warning("stars.refresh: no sites in stars.sites, nothing to fetch")
        return None
    scored = await fetch_all(sites)
    if not scored:
        logger.warning("stars.refresh: empty fetch, keeping existing rows")
        return None
    written = await asyncio.to_thread(_persist_sites, scored)
    logger.info(
        "stars.refresh ok: %d hours across %d/%d sites",
        written,
        len(scored),
        len(sites),
    )
    return None


async def prune_hours_handler(session: Session) -> datetime | None:
    """Drop elapsed hours (housekeeping; the read endpoint also filters)."""
    deleted = await asyncio.to_thread(_run_prune)
    logger.info("stars.prune_hours: deleted %d elapsed rows", deleted)
    return None
