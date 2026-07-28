"""Campsites HTTP API. SSR-only, read-only public surface.

One endpoint backs the /app/campsites page:

- ``GET /api/campsites/snapshot`` returns all BC Parks campgrounds with
  per-day availability + weather merged over the next 14 days, plus
  server-computed park-level metrics (best_score, good_days) so the
  SvelteKit page needs no math. CDN-cached with ETag/304 support.
"""

from __future__ import annotations

import logging
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session, select

from core.db import get_session
from campsites.models import Availability, Campground, Weather

logger = logging.getLogger("monolith.campsites")

router = APIRouter(prefix="/api/campsites", tags=["campsites"])

# Data refreshes hourly (campsites-refresh CronWorkflow), but 60s edge freshness
# lets a deploy become visible within ~1 min without a CDN purge: the versioned
# page ETag busts on deploy and the short s-maxage makes the CDN revalidate
# promptly, while the (build x data)-derived ETag keeps the extra revalidations
# cheap 304s. Mirrors CAMPSITES_SNAPSHOT_CACHE_CONTROL in
# frontend/src/lib/cache-headers.js, keep in sync.
_SNAPSHOT_CACHE_CONTROL = (
    "public, max-age=0, s-maxage=60, stale-while-revalidate=3600, stale-if-error=86400"
)

_LOOKAHEAD_DAYS = 14


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to tz-aware UTC.

    Postgres returns tz-aware values; SQLite (tests) can return naive ones even
    though we always write tz-aware UTC. Treat naive as UTC so ETag stamps stay
    stable across both backends.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 string in UTC, or None."""
    coerced = _as_utc(value)
    return coerced.isoformat() if coerced is not None else None


def _snapshot_etag(generated_at: str | None, count: int) -> str:
    """Stable ETag: combines the freshest data timestamp with the park count.

    A park being added or removed invalidates the cache even when no surviving
    row's timestamp changes.
    """
    stamp = generated_at or "null"
    return f'"{stamp}-{count}"'


@router.get("/snapshot")
def get_snapshot(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """All campgrounds with 14-day availability x weather. SSR-only, CDN-cached.

    Returns a single payload with every park, per-day merged data, and
    server-computed park-level scores so the client needs no math. Returns 503
    when no campground rows exist (data not yet populated). Conditional GETs
    with If-None-Match short-circuit with 304.
    """
    campgrounds = session.exec(select(Campground)).all()
    if not campgrounds:
        raise HTTPException(status_code=503, detail="campsites data unavailable")

    today = datetime.now(timezone.utc).date()
    cutoff = today + timedelta(days=_LOOKAHEAD_DAYS - 1)
    # Window lower bound is one day behind UTC-today so a park whose local date
    # is still "yesterday" (BC evenings, UTC 00:00-07:00) is not hidden from
    # the response. The job stores park-local dates, so this keeps the two grids
    # aligned. Upper bound is unchanged: the job stores at most ~14 days ahead.
    window_start = today - timedelta(days=1)

    avail_rows = session.exec(
        select(Availability)
        .where(Availability.date >= window_start)
        .where(Availability.date <= cutoff)
    ).all()
    weather_rows = session.exec(
        select(Weather)
        .where(Weather.date >= window_start)
        .where(Weather.date <= cutoff)
    ).all()

    # Index by (resource_location_id, date) for O(1) merge.
    avail_by_park: dict[int, dict[date_type, Availability]] = {}
    for row in avail_rows:
        avail_by_park.setdefault(row.resource_location_id, {})[row.date] = row

    weather_by_park: dict[int, dict[date_type, Weather]] = {}
    for row in weather_rows:
        weather_by_park.setdefault(row.resource_location_id, {})[row.date] = row

    # generated_at = max(scraped_at, fetched_at) across all data rows.
    generated_at_dt: datetime | None = None

    def _bump(dt: datetime | None) -> None:
        nonlocal generated_at_dt
        coerced = _as_utc(dt)
        if coerced is not None and (
            generated_at_dt is None or coerced > generated_at_dt
        ):
            generated_at_dt = coerced

    for row in avail_rows:
        _bump(row.scraped_at)
    for row in weather_rows:
        _bump(row.fetched_at)

    parks: list[dict[str, Any]] = []
    for cg in campgrounds:
        avail_map = avail_by_park.get(cg.resource_location_id, {})
        weather_map = weather_by_park.get(cg.resource_location_id, {})
        all_dates = sorted(avail_map.keys() | weather_map.keys())

        days: list[dict[str, Any]] = []
        for d in all_dates:
            av = avail_map.get(d)
            wx = weather_map.get(d)
            available = av.has_availability if av is not None else False
            days.append(
                {
                    "date": d.isoformat(),
                    "available": available,
                    "sunny_score": wx.sunny_score if wx is not None else 0,
                    "is_good": wx.is_good if wx is not None else False,
                    "cloud": wx.cloud_cover if wx is not None else None,
                    "precip": wx.precip_sum if wx is not None else None,
                    "temp_max": wx.temp_max if wx is not None else None,
                }
            )

        available_scores = [day["sunny_score"] for day in days if day["available"]]
        best_score = max(available_scores) if available_scores else 0
        good_days = sum(1 for day in days if day["available"] and day["is_good"])

        parks.append(
            {
                "id": cg.resource_location_id,
                "name": cg.name,
                "region": cg.region,
                "lat": cg.latitude,
                "lon": cg.longitude,
                "booking_url": cg.booking_url,
                "best_score": best_score,
                "good_days": good_days,
                "days": days,
            }
        )

    parks.sort(key=lambda p: (-p["best_score"], p["name"]))

    generated_at = _iso(generated_at_dt)
    etag = _snapshot_etag(generated_at, len(parks))
    headers = {"Cache-Control": _SNAPSHOT_CACHE_CONTROL, "ETag": etag}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value

    return {
        "generated_at": generated_at,
        "count": len(parks),
        "parks": parks,
    }
