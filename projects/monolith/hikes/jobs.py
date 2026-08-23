"""Scheduled job handlers for the hikes domain.

scrape_walks_handler runs the weekly WalkHighlands scrape and
refresh_forecasts_handler the 6-hourly met.no window refresh. In both, the
network phase runs in the async handler and the synchronous Session I/O is
delegated to a worker thread (asyncio.to_thread) so it never blocks the event
loop, mirroring ships.retention / ships.ingest. The scheduler passes a
session, but the DB work uses its own session inside the thread.
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlmodel import Session, delete, select

from hikes import models
from hikes.forecast import fetch_all_windows
from hikes.walkhighlands import Walk as ScrapedWalk
from hikes.walkhighlands import fetch_all_walks
from shared.forecast_freshness import top_of_hour

logger = logging.getLogger("monolith.hikes")

# Generous client-level ceiling; per-request timeouts in the fetch helpers are
# tighter. An explicit timeout keeps the client from hanging the loop forever.
SCRAPE_TIMEOUT_SECS = 30.0
FORECAST_TIMEOUT_SECS = 30.0


def _upsert_walks(session: Session, walks: list[ScrapedWalk]) -> tuple[int, int]:
    """Upsert scraped walks into ``session`` (no commit). Returns (new, updated).

    Dedupes the batch by uuid first: two walk pages can share a trailhead
    coordinate, and the uuid is uuid5 of "lat,lon", so the scraped list may carry
    duplicate uuids. Without deduping, two not-yet-persisted rows with the same
    uuid both take the insert path and the batch INSERT trips ``walks_pkey``,
    rolling back the entire upsert (which is exactly how the corpus stayed frozen
    once scraping started succeeding again). Last occurrence wins.

    Upserts only the scraped columns plus scraped_at; the typed walk_hours rows
    belong to the forecast job and are never touched here. Takes an explicit
    session so the SQLite create_all fixtures can drive it.
    """
    now = datetime.now(timezone.utc)
    deduped = list({walk.uuid: walk for walk in walks}.values())
    new_rows: list[models.Walk] = []
    updated = 0
    for walk in deduped:
        existing = session.get(models.Walk, walk.uuid)
        if existing is None:
            new_rows.append(
                models.Walk(
                    uuid=walk.uuid,
                    name=walk.name,
                    url=walk.url,
                    distance_km=walk.distance_km,
                    ascent_m=walk.ascent_m,
                    duration_h=walk.duration_h,
                    summary=walk.summary,
                    latitude=walk.latitude,
                    longitude=walk.longitude,
                    scraped_at=now,
                )
            )
            continue
        existing.name = walk.name
        existing.url = walk.url
        existing.distance_km = walk.distance_km
        existing.ascent_m = walk.ascent_m
        existing.duration_h = walk.duration_h
        existing.summary = walk.summary
        existing.latitude = walk.latitude
        existing.longitude = walk.longitude
        existing.scraped_at = now
        updated += 1
        # Rows fetched via session.get are already tracked; attribute mutations
        # flush on commit without a session.add in the loop.

    if new_rows:
        session.add_all(new_rows)
    return len(new_rows), updated


def _persist_walks(walks: list[ScrapedWalk]) -> tuple[int, int]:
    """Open a fresh session and upsert the scraped walks. Runs off the event
    loop via asyncio.to_thread (so it must own its session, never the
    scheduler's). Delegates the testable upsert to ``_upsert_walks``.
    """
    from core.db import get_engine

    with Session(get_engine()) as session:
        result = _upsert_walks(session, walks)
        session.commit()
    return result


async def scrape_walks_handler(session: Session) -> datetime | None:
    """Weekly WalkHighlands scrape: full network phase first, then one upsert pass.

    If the scrape produced nothing, keep the existing corpus and write nothing
    (a transient WalkHighlands outage must not wipe the table).
    """
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=SCRAPE_TIMEOUT_SECS
    ) as client:
        walks, stats = await fetch_all_walks(client)

    if not walks:
        logger.error(
            "hikes scrape: zero walks scraped, keeping existing corpus (stats: %s)",
            stats,
        )
        return None

    new_count, updated = await asyncio.to_thread(_persist_walks, walks)
    logger.info(
        "hikes scrape: upserted %d walks (%d new, %d updated)",
        len(walks),
        new_count,
        updated,
    )
    return None


def _load_coords() -> list[tuple[str, float, float]]:
    """Load (uuid, lat, lon) for every walk in a fresh session."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        return list(
            session.exec(
                select(models.Walk.uuid, models.Walk.latitude, models.Walk.longitude)
            ).all()
        )


def _write_walk_hours(
    session: Session, windows_by_uuid: dict[str, list], now: datetime
) -> int:
    """Wholesale-replace walk_hours rows for every fetched walk. Returns rows written.

    One bulk delete of the fetched walks' existing hours, then one add_all of
    the new typed rows, so there is no per-iteration session.add. Walks absent
    from ``windows_by_uuid`` (failed fetch) are untouched: stale beats empty.
    A walk present with an empty window list correctly ends up with no hours
    (its fetch succeeded but yielded nothing viable).

    Each window tuple is [ts_unix, temp_c, precip_mm, wind_kmh, cloud_pct] as
    emitted by hikes.forecast.compute_windows; the unix timestamp becomes the
    tz-aware UTC hour_time.
    """
    walk_uuids = list(windows_by_uuid.keys())
    session.execute(
        delete(models.WalkHour).where(models.WalkHour.walk_uuid.in_(walk_uuids))
    )
    new_rows = [
        models.WalkHour(
            walk_uuid=walk_uuid,
            hour_time=datetime.fromtimestamp(window[0], tz=timezone.utc),
            temp_c=window[1],
            precip_mm=window[2],
            wind_kmh=window[3],
            cloud_pct=window[4],
            fetched_at=now,
        )
        for walk_uuid, windows in windows_by_uuid.items()
        for window in windows
    ]
    session.add_all(new_rows)
    return len(new_rows)


def _persist_windows(windows_by_uuid: dict[str, list], now: datetime) -> int:
    """Write recomputed hours in a fresh session. Returns total hour count."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        written = _write_walk_hours(session, windows_by_uuid, now)
        session.commit()
    return written


def _prune_elapsed(session: Session) -> int:
    """Delete walk_hours whose clock hour has elapsed. Returns rows deleted.

    The cutoff is the top of the current UTC clock hour: rows whose hour_time is
    strictly before it have elapsed. cutoff is tz-aware UTC, so the comparison
    stays tz-aware against the TIMESTAMPTZ column.
    """
    cutoff = top_of_hour()
    result = session.execute(
        delete(models.WalkHour).where(models.WalkHour.hour_time < cutoff)
    )
    return result.rowcount or 0


def _run_prune() -> int:
    """Prune elapsed hours in a fresh session (runs off the event loop).

    Commits only when rows were actually deleted, so a quiet hour is a no-op
    transaction; fetched_at is never touched (the prune only deletes).
    """
    from core.db import get_engine

    with Session(get_engine()) as session:
        deleted = _prune_elapsed(session)
        if deleted:
            session.commit()
    return deleted


async def refresh_forecasts_handler(session: Session) -> datetime | None:
    """6-hourly met.no refresh: recompute viable hiking windows per walk.

    Loads the coordinate corpus, runs the whole network phase, then
    wholesale-replaces each fetched walk's typed walk_hours rows in one
    transaction. Walks whose fetch or window computation failed are absent from
    the result dict and keep their previous hours (stale beats empty).
    """
    coords = await asyncio.to_thread(_load_coords)
    if not coords:
        logger.info("hikes forecast: no walks in corpus, nothing to refresh")
        return None

    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=FORECAST_TIMEOUT_SECS) as client:
        windows_by_uuid = await fetch_all_windows(client, coords, now)

    total_hours = await asyncio.to_thread(_persist_windows, windows_by_uuid, now)
    logger.info(
        "hikes forecast: %d walks, %d forecasts fetched, %d viable hours",
        len(coords),
        len(windows_by_uuid),
        total_hours,
    )
    return None


async def prune_windows_handler(session: Session) -> datetime | None:
    """Drop elapsed hours hourly (housekeeping; the read endpoints also filter)."""
    deleted = await asyncio.to_thread(_run_prune)
    logger.info("hikes prune_windows: deleted %d elapsed rows", deleted)
    return None
