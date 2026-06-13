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
from sqlmodel import Session, select

from hikes import models
from hikes.forecast import fetch_all_windows
from hikes.walkhighlands import Walk as ScrapedWalk
from hikes.walkhighlands import fetch_all_walks

logger = logging.getLogger("hikes")

# Generous client-level ceiling; per-request timeouts in the fetch helpers are
# tighter. An explicit timeout keeps the client from hanging the loop forever.
SCRAPE_TIMEOUT_SECS = 30.0
FORECAST_TIMEOUT_SECS = 30.0


def _persist_walks(walks: list[ScrapedWalk]) -> tuple[int, int]:
    """Upsert scraped walks in a fresh session. Returns (new, updated).

    Upserts only the scraped columns plus scraped_at; windows and
    windows_updated_at belong to the forecast job and are never touched here.
    Runs off the event loop via asyncio.to_thread.
    """
    from app.db import get_engine

    now = datetime.now(timezone.utc)
    new_rows: list[models.Walk] = []
    updated = 0
    with Session(get_engine()) as session:
        for walk in walks:
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
            # Rows fetched via session.get are already tracked; attribute
            # mutations flush on commit without a session.add in the loop.

        if new_rows:
            session.add_all(new_rows)
        session.commit()
    return len(new_rows), updated


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
    from app.db import get_engine

    with Session(get_engine()) as session:
        return list(
            session.exec(
                select(models.Walk.uuid, models.Walk.latitude, models.Walk.longitude)
            ).all()
        )


def _persist_windows(
    windows_by_uuid: dict[str, list], now: datetime
) -> int:
    """Write recomputed windows in a fresh session. Returns total window count.

    Walks absent from windows_by_uuid keep their previous windows (the caller
    only includes walks whose forecast fetch and computation both succeeded).
    """
    from app.db import get_engine

    total_windows = 0
    with Session(get_engine()) as session:
        for walk_uuid, windows in windows_by_uuid.items():
            walk = session.get(models.Walk, walk_uuid)
            if walk is None:
                continue
            walk.windows = windows
            walk.windows_updated_at = now
            total_windows += len(windows)
        session.commit()
    return total_windows


async def refresh_forecasts_handler(session: Session) -> datetime | None:
    """6-hourly met.no refresh: recompute viable hiking windows per walk.

    Loads the coordinate corpus, runs the whole network phase, then updates
    windows and windows_updated_at in one transaction. Walks whose fetch or
    window computation failed are absent from the result dict and keep their
    previous windows (stale beats empty).
    """
    coords = await asyncio.to_thread(_load_coords)
    if not coords:
        logger.info("hikes forecast: no walks in corpus, nothing to refresh")
        return None

    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=FORECAST_TIMEOUT_SECS) as client:
        windows_by_uuid = await fetch_all_windows(client, coords, now)

    total_windows = await asyncio.to_thread(_persist_windows, windows_by_uuid, now)
    logger.info(
        "hikes forecast: %d walks, %d forecasts fetched, %d viable windows",
        len(coords),
        len(windows_by_uuid),
        total_windows,
    )
    return None
