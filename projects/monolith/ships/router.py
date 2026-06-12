"""Ships HTTP API. SSR-only: never added to httproute-public.yaml.

Two read endpoints back the /app/ships live map:

- ``GET /api/ships/snapshot``, every vessel's current position joined with
  its metadata, for the initial map render.
- ``GET /api/ships/track/{mmsi}``, one vessel's position history, fetched on
  marker click to draw its route.

Both are CDN-cached and reached only from SvelteKit SSR (``http://localhost:8000``
in the same pod). The CDN fans out to viewers, so these run at most a few times
per minute regardless of how many browsers are watching. Conditional GETs on the
snapshot short-circuit with a 304 via ETag.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session, select

from app.db import get_session
from ships.heat import LAT_STEP as HEAT_LAT_STEP
from ships.heat import LON_STEP as HEAT_LON_STEP
from ships.models import HeatCell, LatestPosition, Position, Vessel

logger = logging.getLogger("ships")

router = APIRouter(prefix="/api/ships", tags=["ships"])

# Clamp the track limit so a crafted request can't pull an unbounded history.
_MAX_TRACK_LIMIT = 5000

# Mirrors SHIPS_SNAPSHOT_CACHE_CONTROL in frontend/src/lib/cache-headers.js, keep in sync.
_SNAPSHOT_CACHE_CONTROL = (
    "public, s-maxage=120, stale-while-revalidate=600, stale-if-error=86400"
)
# Mirrors SHIPS_TRACK_CACHE_CONTROL in frontend/src/lib/cache-headers.js, keep in sync.
_TRACK_CACHE_CONTROL = (
    "public, s-maxage=60, stale-while-revalidate=300, stale-if-error=86400"
)
# Heatmap rollup refreshes hourly, so 5 min fresh with a 1 h SWR window is plenty.
# Mirrors SHIPS_HEAT_CACHE_CONTROL in frontend/src/lib/cache-headers.js, keep in sync.
_HEAT_CACHE_CONTROL = (
    "public, s-maxage=300, stale-while-revalidate=3600, stale-if-error=86400"
)


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


def _snapshot_etag(vessel_count: int, max_updated: datetime | None) -> str:
    """Stable ETag for a snapshot payload.

    Combines max(updated_at) with the vessel count so a vessel dropping out
    of latest_positions invalidates the cache even when no surviving row's
    timestamp moves.
    """
    stamp = max_updated.isoformat() if max_updated is not None else "null"
    return f'"{stamp}-{vessel_count}"'


def _parse_since(s: str | None) -> timedelta | None:
    """Parse a duration suffix (``1h``, ``30m``, ``2d``) into a timedelta.

    Returns None for an empty, malformed, or unrecognised value, matching the
    old backend's permissive behaviour (an unparsable ``since`` means "no
    lower bound" rather than an error).
    """
    if not s:
        return None
    try:
        if s.endswith("h"):
            return timedelta(hours=int(s[:-1]))
        if s.endswith("m"):
            return timedelta(minutes=int(s[:-1]))
        if s.endswith("d"):
            return timedelta(days=int(s[:-1]))
    except ValueError:
        return None
    return None


@router.get("/snapshot")
def get_snapshot(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """All current vessel positions for the /app/ships map. SSR-only, CDN-cached."""
    rows = session.exec(
        select(LatestPosition, Vessel).join(
            Vessel, Vessel.mmsi == LatestPosition.mmsi, isouter=True
        )
    ).all()

    vessels = []
    max_updated: datetime | None = None
    for pos, vessel in rows:
        updated = _as_utc(pos.updated_at)
        if updated is not None and (max_updated is None or updated > max_updated):
            max_updated = updated
        vessels.append(
            {
                "mmsi": pos.mmsi,
                "lat": pos.lat,
                "lon": pos.lon,
                "speed": pos.speed,
                "course": pos.course,
                "heading": pos.heading,
                "nav_status": pos.nav_status,
                # Prefer the position-message ship_name, fall back to vessel name.
                "ship_name": pos.ship_name or (vessel.name if vessel else None),
                "recorded_at": _iso(pos.recorded_at),
                "first_seen_at_location": _iso(pos.first_seen_at_location),
                "updated_at": _iso(pos.updated_at),
                "name": vessel.name if vessel else None,
                "ship_type": vessel.ship_type if vessel else None,
                "destination": vessel.destination if vessel else None,
                "eta": _iso(vessel.eta) if vessel else None,
            }
        )

    etag = _snapshot_etag(len(vessels), max_updated)
    headers = {"Cache-Control": _SNAPSHOT_CACHE_CONTROL, "ETag": etag}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return {"count": len(vessels), "vessels": vessels}


@router.get("/track/{mmsi}")
def get_track(
    mmsi: str,
    request: Request,
    response: Response,
    since: str | None = None,
    limit: int = 1000,
    session: Session = Depends(get_session),
):
    """One vessel's position history for the route overlay. SSR-only, CDN-cached.

    An unknown MMSI returns an empty track (not 404), matching the old
    get_vessel_track behaviour.
    """
    limit = max(1, min(limit, _MAX_TRACK_LIMIT))

    stmt = select(Position).where(Position.mmsi == mmsi)
    delta = _parse_since(since)
    if delta is not None:
        # Compute the cutoff in Python and compare in the query so this stays
        # portable to SQLite (no SQL now() - interval).
        cutoff = datetime.now(timezone.utc) - delta
        stmt = stmt.where(Position.recorded_at >= cutoff)
    stmt = stmt.order_by(Position.recorded_at.desc()).limit(limit)

    positions = session.exec(stmt).all()
    track = [
        {
            "lat": p.lat,
            "lon": p.lon,
            "speed": p.speed,
            "course": p.course,
            "heading": p.heading,
            "nav_status": p.nav_status,
            "recorded_at": _iso(p.recorded_at),
        }
        for p in positions
    ]

    response.headers["Cache-Control"] = _TRACK_CACHE_CONTROL
    return {"mmsi": mmsi, "count": len(track), "track": track}


@router.get("/heat")
def get_heat(
    response: Response,
    session: Session = Depends(get_session),
):
    """Precomputed traffic-density grid for the /app/ships heatmap.

    Returns the occupied ~500m cells as compact [lat_bin, lon_bin, count]
    triples plus the cell steps; the client reconstructs each cell polygon as
    [lat_bin*step_lat, lon_bin*step_lon] .. [+step]. SSR-only, CDN-cached. The
    heavy aggregation runs hourly in ships.heat; this just reads the rollup.
    """
    rows = session.exec(select(HeatCell)).all()
    cells = [[c.lat_bin, c.lon_bin, c.count] for c in rows]

    response.headers["Cache-Control"] = _HEAT_CACHE_CONTROL
    return {
        "step_lat": HEAT_LAT_STEP,
        "step_lon": HEAT_LON_STEP,
        "count": len(cells),
        "cells": cells,
    }
