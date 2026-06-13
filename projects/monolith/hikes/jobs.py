"""Scheduled job handlers for the hikes domain.

scrape_walks_handler runs the weekly WalkHighlands scrape and
refresh_forecasts_handler the 6-hourly met.no window refresh; in both, the
network phase completes fully before any session writes (mirroring how
ships.ingest separates network work from Session usage).
"""

import logging
from datetime import datetime, timezone

import httpx
from sqlmodel import Session, select

from hikes import models
from hikes.forecast import fetch_all_windows
from hikes.walkhighlands import fetch_all_walks

logger = logging.getLogger("hikes")


async def scrape_walks_handler(session: Session) -> datetime | None:
    """Weekly WalkHighlands scrape: full network phase first, then one upsert pass.

    Upserts only the scraped columns plus scraped_at; windows and
    windows_updated_at belong to the forecast job and are never touched here.
    If the scrape produced nothing, keep the existing corpus and write nothing
    (a transient WalkHighlands outage must not wipe the table).
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        walks, stats = await fetch_all_walks(client)

    if not walks:
        logger.error(
            "hikes scrape: zero walks scraped, keeping existing corpus (stats: %s)",
            stats,
        )
        return None

    now = datetime.now(timezone.utc)
    new_rows: list[models.Walk] = []
    updated = 0
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
    logger.info(
        "hikes scrape: upserted %d walks (%d new, %d updated)",
        len(walks),
        len(new_rows),
        updated,
    )
    return None


async def refresh_forecasts_handler(session: Session) -> datetime | None:
    """6-hourly met.no refresh: recompute viable hiking windows per walk.

    Loads the coordinate corpus, runs the whole network phase, then updates
    windows and windows_updated_at in one transaction. Walks whose fetch
    failed are absent from the result dict and keep their previous windows
    (stale beats empty).
    """
    coords = session.exec(
        select(models.Walk.uuid, models.Walk.latitude, models.Walk.longitude)
    ).all()
    if not coords:
        logger.info("hikes forecast: no walks in corpus, nothing to refresh")
        return None

    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient() as client:
        windows_by_uuid = await fetch_all_windows(client, coords, now)

    total_windows = 0
    for walk_uuid, windows in windows_by_uuid.items():
        walk = session.get(models.Walk, walk_uuid)
        if walk is None:
            continue
        walk.windows = windows
        walk.windows_updated_at = now
        total_windows += len(windows)
        # Tracked-row mutation: session.get rows flush on commit without
        # a session.add in the loop.
    session.commit()

    logger.info(
        "hikes forecast: %d walks, %d forecasts fetched, %d viable windows",
        len(coords),
        len(windows_by_uuid),
        total_windows,
    )
    return None
