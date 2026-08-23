"""Hikes HTTP API. SSR-only: never added to httproute-public.yaml.

Two read endpoints back the /app/hikes walk planner:

- ``GET /api/hikes/walks``, the whole walk corpus as a LIGHT list (stats,
  coordinates, and the set of doable UK-local days), for the map + filters.
  Deliberately omits the hour-by-hour ``windows`` and the prose ``summary``:
  shipping ~49 hourly tuples for all ~1600 walks was ~3 MB, and the map and
  day filter only need "which days is this walk doable". A day is doable when
  its good hours hold a slot long enough for the walk's advertised duration
  (see _doable_days), not merely one good hour.
- ``GET /api/hikes/walks/{uuid}``, the per-walk detail (``summary`` +
  hourly ``windows``) the selected-walk card fetches on demand.

Reached only from SvelteKit SSR (``http://localhost:8000`` in the same pod);
the /app/hikes page is the public surface and the CDN fans out to viewers.
Conditional GETs on the list short-circuit with a 304 via its ETag; the detail
endpoint is CDN-cached without an ETag, and the client drops any already-started
hour by absolute timestamp, so a momentarily stale detail never shows past hours.

The read-time hour filter (``hour_time >= top_of_hour(now)``) is the source of
truth for "future windows": both endpoints query the typed walk_hours table and
never trust it to be already pruned. The hourly prune job is housekeeping only,
so a stale row that has not been pruned yet is still excluded here. The list
ETag folds the cutoff in (see _walks_etag) so the CDN turns over hourly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session, select

from core.db import get_session
from hikes.models import Walk, WalkHour
from shared.forecast_freshness import top_of_hour

logger = logging.getLogger("monolith.hikes")

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


# A day is "doable" for a walk when its good hours contain a long-enough slot to
# actually complete the walk, not merely one good hour (the old, length-blind
# rule). The slot must hold at least DURATION_GOOD_HOUR_FRACTION of the walk's
# advertised duration in good hours, tolerating one bad hour embedded in the run.
# Walks longer than LONG_WALK_HOURS get DARK_SHOULDER_HOURS of extra credit: you
# can start before sunrise and finish after sunset in the dark, so the two
# shoulder hours extend a real daylight run. The shoulder hours are never stored
# (the forecast pipeline keeps only daylight hours), so we credit them rather than
# weather-check them; winter daylight length, not dawn/dusk weather, is the
# binding constraint for a long Scottish walk.
DURATION_GOOD_HOUR_FRACTION = 0.8
LONG_WALK_HOURS = 7.0
DARK_SHOULDER_HOURS = 2
# Guards float drift on the 0.8 * duration_h threshold (e.g. 0.8 * 5.0 landing at
# 4.0000000001) so an exactly-met requirement is not rejected.
_REQUIREMENT_EPSILON = 1e-9


def _longest_good_run(hour_indices: list[int]) -> int:
    """Longest contiguous run of good hours, tolerating one single-hour gap.

    ``hour_indices`` are the sorted, unique integer hour buckets (UTC hours since
    the epoch) of a single day's good hours. Stored hours are always within the
    walk's daylight band, so an interior missing hour means bad weather, not
    darkness: bridging exactly one such gap implements "I'll accept one bad hour
    in the slot". The return value counts GOOD hours only; the tolerated bad hour
    is not counted toward the run length. A gap of two or more missing hours, or a
    second single-hour gap, ends the run.
    """
    if not hour_indices:
        return 0

    best = 0
    run = 1  # good hours in the current run (the first hour starts it)
    gap_used = False
    for i in range(1, len(hour_indices)):
        delta = hour_indices[i] - hour_indices[i - 1]
        if delta == 1:
            run += 1
        elif delta == 2 and not gap_used:
            # One missing hour: bridge it once, spending the bad-hour allowance.
            run += 1
            gap_used = True
        else:
            best = max(best, run)
            run = 1
            gap_used = False
    return max(best, run)


def _doable_days(hours: list[datetime], duration_h: float) -> list[str]:
    """Distinct UK-local days on which the walk is actually DOABLE given its length.

    A day qualifies when its good hours hold a contiguous slot of at least
    ``DURATION_GOOD_HOUR_FRACTION * duration_h`` good hours (tolerating one bad
    hour mid-slot), plus ``DARK_SHOULDER_HOURS`` of credit for walks longer than
    ``LONG_WALK_HOURS``. The shoulder credit only applies when there is a real
    daylight run to extend (``run > 0``): a fully washed-out day is never doable.

    We emit ABSOLUTE dates (not "next 7 days") so the value is independent of when
    it was computed and stays correct in a CDN cache across midnight; the client
    intersects them with its own rolling 7-day strip.
    """
    required = DURATION_GOOD_HOUR_FRACTION * duration_h
    shoulder = DARK_SHOULDER_HOURS if duration_h > LONG_WALK_HOURS else 0

    indices_by_day: dict[str, list[int]] = {}
    for hour_time in hours:
        coerced = _as_utc(hour_time)
        if coerced is None:
            continue
        day = coerced.astimezone(_UK_TZ).date().isoformat()
        indices_by_day.setdefault(day, []).append(int(coerced.timestamp()) // 3600)

    doable: list[str] = []
    for day, indices in indices_by_day.items():
        run = _longest_good_run(sorted(set(indices)))
        if run <= 0:
            continue
        if run + shoulder + _REQUIREMENT_EPSILON >= required:
            doable.append(day)
    return sorted(doable)


def _window_tuple(row: WalkHour) -> list:
    """Reassemble the compact wire tuple from a typed walk_hours row.

    The client reads windows as [ts_unix_seconds, temp_c, precip_mm, wind_kmh,
    cloud_pct] tuples (see frontend hikes/filters.js). Storage is normalised to
    typed rows, but the wire format is unchanged, so the frontend needs no edit.
    wind_kmh and cloud_pct were integers in the legacy tuple, so cast them back
    from the DOUBLE PRECISION columns; temp_c/precip_mm stay as stored.
    """
    coerced = _as_utc(row.hour_time)
    return [
        int(coerced.timestamp()),
        row.temp_c,
        row.precip_mm,
        int(row.wind_kmh),
        int(row.cloud_pct),
    ]


# Bump whenever the /walks response SHAPE changes (fields added/removed/renamed).
# The ETag is otherwise data-derived, so a shape-only code deploy would leave it
# unchanged: browsers revalidate, get a 304, and keep their stale-shape body.
# Folding this token in means a shape change busts every client's cache.
# History: v2 = dropped hourly `windows` + `summary`, added `viable_days`.
#          v3 = windows backed by typed walk_hours; ETag folds in the hourly
#               top_of_hour cutoff so the CDN turns over each clock hour.
#          v4 = `viable_days` is now duration-aware doability (a long-enough good
#               slot for the walk's length), not "any one good hour".
_WALKS_SCHEMA_VERSION = "v4"


def _walks_etag(walk_count: int, cutoff: datetime, max_fetched: datetime | None) -> str:
    """Stable ETag for the walks payload.

    Combines the response-schema token, the current clock-hour cutoff, the
    freshest walk_hours fetched_at, and the row count. The cutoff token turns
    the cache over at each hour boundary (as hours fall past the cutoff the
    viable_days sets shrink); fetched_at busts it on a forecast refresh; count
    busts it when a walk appears or disappears.
    """
    stamp = max_fetched.isoformat() if max_fetched is not None else "null"
    return f'"{_WALKS_SCHEMA_VERSION}-{cutoff.isoformat()}-{stamp}-{walk_count}"'


@router.get("/walks")
def get_walks(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """The whole walk corpus for the /app/hikes planner. SSR-only, CDN-cached."""
    now = datetime.now(timezone.utc)
    cutoff = top_of_hour(now)

    walk_rows = session.exec(select(Walk).order_by(Walk.name)).all()

    # Read-time correctness filter: only hours at or after the current clock
    # hour. The prune job is best-effort housekeeping; the endpoint must not
    # trust the table to be already pruned.
    hour_rows = session.exec(
        select(WalkHour.walk_uuid, WalkHour.hour_time, WalkHour.fetched_at).where(
            WalkHour.hour_time >= cutoff
        )
    ).all()

    hours_by_walk: dict[str, list[datetime]] = {}
    max_fetched: datetime | None = None
    for walk_uuid, hour_time, fetched_at in hour_rows:
        hours_by_walk.setdefault(walk_uuid, []).append(hour_time)
        coerced = _as_utc(fetched_at)
        if coerced is not None and (max_fetched is None or coerced > max_fetched):
            max_fetched = coerced

    walks = [
        {
            "uuid": walk.uuid,
            "name": walk.name,
            "url": walk.url,
            "distance_km": walk.distance_km,
            "ascent_m": walk.ascent_m,
            "duration_h": walk.duration_h,
            "latitude": walk.latitude,
            "longitude": walk.longitude,
            # Light: the days this walk is doable given its length, not the hourly
            # windows. The card fetches windows + summary from /walks/{uuid}. The
            # wire field stays `viable_days` (the frontend keys off it); its
            # meaning is now duration-aware (see _doable_days).
            "viable_days": _doable_days(
                hours_by_walk.get(walk.uuid, []), walk.duration_h
            ),
        }
        for walk in walk_rows
    ]

    etag = _walks_etag(len(walks), cutoff, max_fetched)
    headers = {"Cache-Control": _WALKS_CACHE_CONTROL, "ETag": etag}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return {
        "count": len(walks),
        "generated_at": _iso(max_fetched),
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
    marker is clicked. SSR-only, CDN-cached the same as the list. Hours are
    filtered to the current clock hour and reassembled into the wire tuples the
    frontend expects.
    """
    walk = session.get(Walk, uuid)
    if walk is None:
        raise HTTPException(status_code=404, detail="walk not found")

    cutoff = top_of_hour(datetime.now(timezone.utc))
    hour_rows = session.exec(
        select(WalkHour)
        .where(WalkHour.walk_uuid == uuid, WalkHour.hour_time >= cutoff)
        .order_by(WalkHour.hour_time)
    ).all()

    response.headers["Cache-Control"] = _WALKS_CACHE_CONTROL
    return {
        "uuid": walk.uuid,
        "summary": walk.summary,
        "windows": [_window_tuple(row) for row in hour_rows],
    }
