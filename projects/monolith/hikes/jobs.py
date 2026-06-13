"""Scheduled job handlers for the hikes domain.

scrape_walks_handler runs the weekly WalkHighlands scrape; the network phase
completes fully before any session use (mirroring how ships.ingest separates
network work from Session usage). refresh_forecasts_handler is a stub until
the met.no forecast job lands.
"""

import logging
from datetime import datetime, timezone

import httpx
from sqlmodel import Session

from hikes import models
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
    """Stub: the met.no forecast windows job lands in a follow-up task."""
    logger.info("hikes.refresh_forecasts: not implemented yet")
    return None
