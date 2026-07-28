"""Scheduled refresh job for the campsites domain (/app/campsites).

refresh_handler runs hourly (Argo CronWorkflow campsites-refresh). It:
  1. loads the static campground catalog (campsites/catalog.json) and upserts
     campsites.campgrounds,
  2. fetches BC Parks GoingToCamp availability for the 14-day window, using a
     curl_cffi browser-TLS-impersonating session (the Azure WAF 403s httpx),
  3. fetches the Open-Meteo 14-day forecast per park over plain httpx,
  4. upserts campsites.availability and campsites.weather and prunes past-window
     rows.

The network phase runs in the async handler; every synchronous DB pass is
delegated to a worker thread (asyncio.to_thread) with its own Session so it
never blocks the scheduler event loop, mirroring worldcup.refresh_handler and
dr_jobs.scrape_nhs_handler.

A partial failure is tolerated: if availability comes back empty (WAF block) but
weather succeeds, or vice versa, whatever succeeded is committed and the failure
is logged. The tables are never wiped on an empty result: stale data beats a
blank page. This module is excluded from the public binary.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from datetime import datetime as dt
from datetime import timezone

import httpx
from curl_cffi.requests import AsyncSession

from campsites import client, weather
from campsites.models import Availability, Campground, Weather

logger = logging.getLogger("monolith.campsites")

# curl_cffi browser profile that defeats the Azure WAF JA3 fingerprint check.
_IMPERSONATE = "chrome"

# Client-level ceiling for the Open-Meteo phase.
_WEATHER_TIMEOUT_SECS = 25.0


# ---------------------------------------------------------------------------
# Sync DB helpers (each opens its own Session and commits, off the event loop)
# ---------------------------------------------------------------------------


def _load_and_upsert_catalog() -> list[client.CampgroundRow]:
    """Load the static catalog and upsert campsites.campgrounds; return the rows.

    Idempotent: merge() inserts new parks and updates existing ones keyed on
    resource_location_id. Never deletes: a park dropped from the catalog simply
    stops being refreshed. Runs off the event loop via asyncio.to_thread.
    """
    from sqlmodel import Session

    from core.db import get_engine

    rows = client.load_catalog()
    now = dt.now(timezone.utc)
    with Session(get_engine()) as session:
        for r in rows:
            session.merge(
                Campground(
                    resource_location_id=r.resource_location_id,
                    park_map_id=r.park_map_id,
                    name=r.name,
                    region=r.region,
                    latitude=r.latitude,
                    longitude=r.longitude,
                    iana_tz=r.iana_tz,
                    description=r.description,
                    booking_url=r.booking_url,
                    updated_at=now,
                )
            )
        session.commit()
    return rows


def _upsert_availability(
    avail: dict[int, list[client.DayAvail]],
    prune_floor: datetime.date,
) -> int:
    """Upsert per-park-per-date availability rows, then prune past-window rows.

    Uses session.merge (upsert on the (resource_location_id, date) PK), so no
    session.add-in-loop savepoint concern. Rows with date < prune_floor are
    pruned in the same transaction to keep the rolling window small. prune_floor
    is UTC-today minus one day so a park-local boundary date is never pruned
    during BC evenings (UTC 00:00-07:00) when UTC-today is one day ahead.
    Returns the number of rows written. A no-op (empty input) writes nothing but
    still prunes elapsed rows.
    """
    from sqlmodel import Session, delete

    from core.db import get_engine

    now = dt.now(timezone.utc)
    written = 0
    with Session(get_engine()) as session:
        for rid, days in avail.items():
            for day in days:
                session.merge(
                    Availability(
                        resource_location_id=rid,
                        date=day.date,
                        has_availability=day.has_availability,
                        loops_open=day.loops_open,
                        scraped_at=now,
                    )
                )
                written += 1
        session.execute(delete(Availability).where(Availability.date < prune_floor))
        session.commit()
    return written


def _upsert_weather(
    wx: dict[int, list[weather.WxDay]],
    prune_floor: datetime.date,
) -> int:
    """Upsert per-park-per-date forecast rows, then prune past-window rows.

    Same shape as _upsert_availability: merge() upserts on the composite PK and
    a single delete prunes elapsed dates. precip_prob is stored as an int (the
    Weather column is INTEGER); Open-Meteo returns whole-percent values.
    prune_floor matches the availability floor so both tables stay aligned.
    """
    from sqlmodel import Session, delete

    from core.db import get_engine

    now = dt.now(timezone.utc)
    written = 0
    with Session(get_engine()) as session:
        for rid, days in wx.items():
            for day in days:
                prob = None if day.precip_prob is None else int(day.precip_prob)
                session.merge(
                    Weather(
                        resource_location_id=rid,
                        date=day.date,
                        cloud_cover=day.cloud_cover,
                        precip_sum=day.precip_sum,
                        precip_prob=prob,
                        temp_max=day.temp_max,
                        wind_max=day.wind_max,
                        sunny_score=day.sunny_score,
                        is_good=day.is_good,
                        fetched_at=now,
                    )
                )
                written += 1
        session.execute(delete(Weather).where(Weather.date < prune_floor))
        session.commit()
    return written


# ---------------------------------------------------------------------------
# Async handler
# ---------------------------------------------------------------------------


async def refresh_handler(session) -> datetime.datetime | None:
    """Hourly: refresh availability + weather for all BC Parks campgrounds.

    The scheduler passes a Session but every DB write uses its own session
    inside a worker thread. Returning None lets the scheduler compute the next
    run from the configured interval.
    """
    # Prune floor is UTC-today minus one day so a park whose local date is still
    # "yesterday" (BC evenings, UTC 00:00-07:00) is never pruned mid-run.
    # Availability is anchored per-park to park-local today inside
    # client.fetch_all_availability; weather already uses each park's iana_tz.
    prune_floor = dt.now(timezone.utc).date() - datetime.timedelta(days=1)

    # 1. Static catalog: read the committed JSON and upsert the campgrounds.
    cats = await asyncio.to_thread(_load_and_upsert_catalog)
    logger.info("campsites refresh: loaded %d campgrounds from catalog", len(cats))

    # 2. Availability (curl_cffi impersonation defeats the Azure WAF JA3 check).
    avail: dict[int, list[client.DayAvail]] = {}
    try:
        async with AsyncSession(impersonate=_IMPERSONATE) as gc:
            avail = await client.fetch_all_availability(gc, cats)
    except Exception:
        logger.error("campsites refresh: availability phase failed", exc_info=True)

    # Commit availability NOW, before touching weather. Availability (BC Parks)
    # and weather (Open-Meteo) are independent sources joined per-date by the
    # snapshot API from separate tables, so there is no reason to couple their
    # commits. Committing here means a later weather-phase failure, or the pod
    # being killed at activeDeadlineSeconds while weather grinds through a slow
    # Open-Meteo, can no longer discard a good availability fetch. This is its
    # own committed transaction (to_thread + fresh session), so a SIGKILL after
    # it does not roll it back. _upsert_availability prunes elapsed rows even on
    # an empty dict, and merge never deletes in-window rows, so an empty avail
    # (WAF block) keeps existing data (stale beats blank).
    avail_written = await asyncio.to_thread(_upsert_availability, avail, prune_floor)

    # 3. Weather (Open-Meteo is not WAF-protected; plain httpx). fetch_all_weather
    # bails early after a run of consecutive failures (e.g. an Open-Meteo outage)
    # rather than timing out park-by-park to the deadline.
    wx: dict[int, list[weather.WxDay]] = {}
    try:
        coords = [
            (c.resource_location_id, c.latitude, c.longitude, c.iana_tz) for c in cats
        ]
        async with httpx.AsyncClient(timeout=_WEATHER_TIMEOUT_SECS) as om:
            wx = await weather.fetch_all_weather(om, coords)
    except Exception:
        logger.error("campsites refresh: weather phase failed", exc_info=True)

    # Commit weather independently. Its own prune runs even on an empty dict, so
    # an Open-Meteo outage keeps the existing (stale) forecast rather than a gap.
    wx_written = await asyncio.to_thread(_upsert_weather, wx, prune_floor)
    logger.info(
        "campsites refresh: wrote %d availability rows (%d parks), "
        "%d weather rows (%d parks)",
        avail_written,
        len(avail),
        wx_written,
        len(wx),
    )
    return None
