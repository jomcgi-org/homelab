"""Hikes HTTP API. SSR-only: never added to httproute-public.yaml.

Two read endpoints back the /app/hikes walk planner:

- ``GET /api/hikes/walks``, the whole walk corpus as a LIGHT list (stats,
  coordinates, and the set of viable UK-local days), for the map + filters.
  Deliberately omits the hour-by-hour ``windows`` and the prose ``summary``:
  shipping ~49 hourly tuples for all ~1600 walks was ~3 MB, and the map and
  day filter only need "which days is this walk viable".
- ``GET /api/hikes/walks/{uuid}``, the per-walk detail (``summary`` +
  hourly ``windows``) the selected-walk card fetches on demand.

Reached only from SvelteKit SSR (``http://localhost:8000`` in the same pod);
the /app/hikes page is the public surface and the CDN fans out to viewers.
Conditional GETs short-circuit with a 304 via ETag.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session, select

from app.db import get_session
from hikes.models import Walk

logger = logging.getLogger("hikes")

router = APIRouter(prefix="/api/hikes", tags=["hikes"])

# Walks are in Scotland, so viable-day bucketing uses the UK civil calendar (the
# frontend day strip does the same in the browser). tzdata is present in the
# image (home.schedule already relies on zoneinfo server-side).
_UK_TZ = ZoneInfo("Europe/London")

# Forecasts refresh every 2 h, so 30 min edge freshness with a 1 h SWR window is
# plenty; max-age=0 makes the browser revalidate rather than hold a stale copy
# (see the note in cache-headers.js). Mirrors HIKES_WALKS_CACHE_CONTROL in
# frontend/src/lib/cache-headers.js, keep in sync.
_WALKS_CACHE_CONTROL = "public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400"


def _viable_days(windows: list | None) -> list[str]:
    """Distinct UK-local calendar days (YYYY-MM-DD) that carry a viable window.

    Window tuples are [ts_seconds, ...]; ts is unix UTC. We emit ABSOLUTE dates
    (not "next 7 days") so the value is independent of when it was computed and
    stays correct in a CDN cache across midnight; the client intersects them
    with its own rolling 7-day strip.
    """
    days: set[str] = set()
    for window in windows or []:
        try:
            ts = window[0]
        except (TypeError, IndexError, KeyError):
            continue
        local = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_UK_TZ)
        days.add(local.date().isoformat())
    return sorted(days)


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to tz-aware UTC.

    Postgres returns tz-aware values; SQLite (used in tests) can return
    naive ones even though we always write tz-aware UTC. Treat naive
    datetimes as UTC so downstream formatters and ETag stamps are stable
    across both backends.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 string in UTC, or None. Keeps the JSON consistent across backends."""
    coerced = _as_utc(value)
    return coerced.isoformat() if coerced is not None else None


def _walks_etag(walk_count: int, max_updated: datetime | None) -> str:
    """Stable ETag for the walks payload.

    Combines max(windows_updated_at) with the row count so a walk appearing
    or disappearing invalidates the cache even when no surviving row's
    timestamp moves.
    """
    stamp = max_updated.isoformat() if max_updated is not None else "null"
    return f'"{stamp}-{walk_count}"'


@router.get("/walks")
def get_walks(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """The whole walk corpus for the /app/hikes planner. SSR-only, CDN-cached."""
    rows = session.exec(select(Walk).order_by(Walk.name)).all()

    walks = []
    max_updated: datetime | None = None
    for walk in rows:
        updated = _as_utc(walk.windows_updated_at)
        if updated is not None and (max_updated is None or updated > max_updated):
            max_updated = updated
        walks.append(
            {
                "uuid": walk.uuid,
                "name": walk.name,
                "url": walk.url,
                "distance_km": walk.distance_km,
                "ascent_m": walk.ascent_m,
                "duration_h": walk.duration_h,
                "latitude": walk.latitude,
                "longitude": walk.longitude,
                # Light: the days this walk is viable, not the hourly windows.
                # The card fetches windows + summary from /walks/{uuid}.
                "viable_days": _viable_days(walk.windows),
            }
        )

    etag = _walks_etag(len(walks), max_updated)
    headers = {"Cache-Control": _WALKS_CACHE_CONTROL, "ETag": etag}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return {
        "count": len(walks),
        "generated_at": _iso(max_updated),
        "walks": walks,
    }


@router.get("/walks/{uuid}")
def get_walk_detail(
    uuid: str,
    response: Response,
    session: Session = Depends(get_session),
):
    """Per-walk detail (summary + hourly windows) for the selected-walk card.

    Split out of the list so the corpus stays light; fetched on demand when a
    marker is clicked. SSR-only, CDN-cached the same as the list.
    """
    walk = session.get(Walk, uuid)
    if walk is None:
        raise HTTPException(status_code=404, detail="walk not found")

    response.headers["Cache-Control"] = _WALKS_CACHE_CONTROL
    return {
        "uuid": walk.uuid,
        "summary": walk.summary,
        "windows": walk.windows,
    }
