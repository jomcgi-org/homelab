"""Stars HTTP API. SSR-only: never added to httproute-public.yaml.

Two read endpoints back the /app/stars dark-sky planner:

- ``GET /api/stars/sites``, every dark-sky site (from stars.sites, sourced from
  the light-pollution grid) joined with its upcoming clear-dark viewing hours,
  for the site list + detail cards (the live layer).
- ``GET /api/stars/history``, per-site accumulated clear-dark hours for a
  calendar month (from stars.site_month_stats, banked by the hourly prune as
  forecast hours elapse, combined with the ERA5 climatology baseline), for the
  historical heatmap layer.

The stars v2 metric is a concrete count of clear dark hours (sun < -12 deg and
cloud < 10%), not the old continuous quality Q = darkness x cloud x weather.
``clear_dark_hours`` is the count of upcoming clear-dark hours and drives the map
ordering and marker colour on the live layer.

Reached only from SvelteKit SSR (``http://localhost:8000`` in the same pod);
the /app/stars page is the public surface and the CDN fans out to viewers, per
ADR 002. Conditional GETs short-circuit with a 304 via ETag.

The read-time hour filter (``hour_time >= top_of_hour(now)``) is the source of
truth for "future windows": the endpoint never trusts the table to be already
pruned. The hourly prune job is housekeeping only, so a stale row that has not
been pruned yet is still excluded here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlmodel import Session, select

from app.db import get_session
from shared.forecast_freshness import top_of_hour
from stars.models import Site, SiteHour, SiteMonthClimatology, SiteMonthStat
from stars.scoring import is_clear_dark_hour

logger = logging.getLogger("stars")

router = APIRouter(prefix="/api/stars", tags=["stars"])

# Cap each site's displayed hour list, so the payload stays light even with a
# multi-day forecast horizon. clear_dark_hours still reports the full count.
_DISPLAY_HOURS = 8

# Dark hours run across midnight, so a viewing "night" is not a calendar day.
# We label each night by its evening date in UK local time: shifting the local
# clock back 12 h folds an evening and the following pre-dawn hours into one
# night key, so the map's night filter groups hours the way a stargazer thinks
# about them (one outing = one night).
_LONDON = ZoneInfo("Europe/London")
_NIGHT_SHIFT = timedelta(hours=12)

# Forecasts refresh hourly, so 30 min edge freshness with a 1 h SWR window is
# plenty; max-age=0 makes the browser revalidate rather than hold a stale copy.
# Mirrors STARS_SITES_CACHE_CONTROL in frontend/src/lib/cache-headers.js if/when
# that constant is added; keep in sync.
_SITES_CACHE_CONTROL = "public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400"


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


def _night_key(hour_time: datetime) -> str:
    """ISO date (YYYY-MM-DD) of the night a dark hour belongs to.

    A night runs from one evening into the next morning, so labelling by the
    calendar date would split a single outing at midnight. Convert to UK local
    time and shift back 12 h before taking the date: evening hours keep their
    own date and pre-dawn hours fold back onto the evening that opened the
    night. The frontend formats this key into the chip label.
    """
    local = _as_utc(hour_time).astimezone(_LONDON)
    return (local - _NIGHT_SHIFT).date().isoformat()


@router.get("/sites")
def get_sites(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """All dark-sky sites with their upcoming clear-dark hours. SSR-only, CDN-cached."""
    now = datetime.now(timezone.utc)
    cutoff = top_of_hour(now)

    # Static site metadata, keyed by site id, joined into each row group at read
    # time. Sourced from the stars.sites table (light-pollution grid, ADR 006).
    by_id = {site.id: site for site in session.exec(select(Site)).all()}

    # Read-time correctness filter: only hours at or after the current clock
    # hour. The prune job is best-effort housekeeping; the endpoint must not
    # trust the table to be already pruned.
    rows = session.exec(select(SiteHour).where(SiteHour.hour_time >= cutoff)).all()

    by_site: dict[str, list[SiteHour]] = {}
    max_fetched: datetime | None = None
    for row in rows:
        if row.fetched_at is not None and (
            max_fetched is None or row.fetched_at > max_fetched
        ):
            max_fetched = row.fetched_at
        by_site.setdefault(row.site_id, []).append(row)

    sites = []
    for site_id, hours in by_site.items():
        meta = by_id.get(site_id)
        if meta is None:
            # Defensive: the refresh job only writes ids that exist in
            # stars.sites, so this should not happen. Skip rather than emit a
            # site with no metadata.
            logger.debug("stars: skipping unknown site_id %s", site_id)
            continue

        # The clear-dark hours among the upcoming dark hours are the windows
        # worth showing; their count is the live headline metric.
        clear_hours = [
            h
            for h in hours
            if is_clear_dark_hour(h.sun_elevation_deg, h.cloud_area_fraction)
        ]
        clear_dark_hours = len(clear_hours)

        # Per-night count of clear-dark hours, so the map's night filter can
        # recolour each marker by how many clear-dark hours the selected
        # night(s) hold.
        night_clear_dark: dict[str, int] = {}
        for h in clear_hours:
            key = _night_key(h.hour_time)
            night_clear_dark[key] = night_clear_dark.get(key, 0) + 1

        # Display the upcoming clear-dark hours in chronological order, capped:
        # a planner reads the night top to bottom.
        best_hours = [
            {
                "time": _iso(h.hour_time),
                "cloud_area_fraction": h.cloud_area_fraction,
                "air_temperature": h.air_temperature,
                "dew_spread": h.dew_spread,
                "symbol": h.symbol,
            }
            for h in sorted(clear_hours, key=lambda h: h.hour_time)[:_DISPLAY_HOURS]
        ]
        sites.append(
            {
                "id": meta.id,
                "name": meta.name,
                "lat": meta.lat,
                "lon": meta.lon,
                "altitude_m": meta.altitude_m,
                "lp_zone": meta.lp_zone,
                "clear_dark_hours": clear_dark_hours,
                "best_hours": best_hours,
                "night_clear_dark": night_clear_dark,
            }
        )

    sites.sort(key=lambda s: s["clear_dark_hours"], reverse=True)

    # Union of nights present across all sites, ascending, so the frontend can
    # render one stable row of night-filter chips (a site missing a night just
    # has no clear-dark hours for it).
    nights = sorted({key for s in sites for key in s["night_clear_dark"]})

    # The ETag folds in the current clock hour so the CDN turns over hourly even
    # when fetched_at has not changed: as hours fall past the cutoff the payload
    # shrinks, and the cutoff token forces a revalidation at each hour boundary.
    max_fetched_utc = _as_utc(max_fetched)
    etag = (
        f'"v2-{cutoff.isoformat()}-'
        f'{max_fetched_utc.isoformat() if max_fetched_utc else "none"}-{len(sites)}"'
    )
    headers = {"Cache-Control": _SITES_CACHE_CONTROL, "ETag": etag}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return {
        "sites": sites,
        "count": len(sites),
        "total_sites": len(by_id),
        "nights": nights,
        "fetched_at": _iso(max_fetched),
    }


@router.get("/history")
def get_history(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    month: int = Query(
        default=0,
        ge=0,
        le=12,
        description="Month-of-year 1-12; 0 = all year (every month summed)",
    ),
):
    """Per-site combined clear-dark hours for a month, or the whole year. SSR-only.

    The historical heatmap layer: each site's heat is the sum of the live
    accumulator (stars.site_month_stats, banked by the hourly prune as forecast
    hours elapse) and the ERA5 climatology backfill (stars.site_month_climatology,
    a long-run seasonal baseline). The two tables carry identical clear-dark
    counts, so they compose by per-field addition: ``dark_hours`` and
    ``clear_dark_hours`` add, and ``clear_rate`` is the combined
    clear_dark_hours over the combined dark_hours.

    ``month`` 1-12 filters the headline counts to that month-of-year; ``month`` 0
    is the all-year view (the default), which sums every month bucket per site.
    Regardless of the selected view, each site carries a ``months`` map of all 12
    months' clear_dark_hours (built from the UNFILTERED rows) so the frontend can
    always draw a 12-bar seasonal graph.

    A site is included if it appears in EITHER table for the selected view
    (full-outer-join by site_id, done in Python). ``clear_dark_hours`` is the
    headline heat metric. Sites with no metadata (dropped from the grid) or zero
    dark hours in the selected view are omitted. Ordered by combined
    ``clear_dark_hours`` descending. CDN-cached with the same headers as
    ``/sites``; conditional GETs short-circuit with a 304.
    """
    by_id = {site.id: site for site in session.exec(select(Site)).all()}

    # Read every month bucket from both accumulators once. A single pass builds
    # two things: the per-site months map (always all 12 months, for the graph)
    # and the selected-view headline counts (the chosen month, or all when 0).
    live = session.exec(select(SiteMonthStat)).all()
    climo = session.exec(select(SiteMonthClimatology)).all()

    # site_id -> {month: combined clear_dark_hours} for the 12-bar graph.
    months_index: dict[str, dict[int, int]] = {}
    # site_id -> {dark, clear} for the selected view (month, or all-year sum).
    combined: dict[str, dict[str, int]] = {}
    for row in (*live, *climo):
        per_month = months_index.setdefault(row.site_id, {})
        per_month[row.month] = per_month.get(row.month, 0) + row.clear_dark_hours
        if month == 0 or row.month == month:
            agg = combined.setdefault(row.site_id, {"dark": 0, "clear": 0})
            agg["dark"] += row.dark_hours
            agg["clear"] += row.clear_dark_hours

    sites = []
    for site_id, agg in combined.items():
        meta = by_id.get(site_id)
        if meta is None:
            # Orphans (a site dropped from the grid) are kept in both tables as
            # historical record but have no metadata to render; skip them.
            logger.debug("stars.history: skipping unknown site_id %s", site_id)
            continue
        dark = agg["dark"]
        clear = agg["clear"]
        if dark <= 0:
            # No dark hours in the selected view: nothing to show, and the rate
            # would divide by zero.
            continue
        per_month = months_index.get(site_id, {})
        sites.append(
            {
                "id": meta.id,
                "name": meta.name,
                "lat": meta.lat,
                "lon": meta.lon,
                "clear_dark_hours": clear,
                "dark_hours": dark,
                "clear_rate": clear / dark if dark else 0.0,
                # All 12 months, zero-filled, so the frontend always has 12 bars
                # regardless of the selected view.
                "months": {mo: per_month.get(mo, 0) for mo in range(1, 13)},
            }
        )

    sites.sort(key=lambda s: s["clear_dark_hours"], reverse=True)

    # The ETag folds in the selected month (0 = all year), the site count, the
    # max combined clear_dark_hours and the total combined dark_hours: it turns
    # over when EITHER the live prune banks more hours or the climatology
    # backfill reloads.
    max_clear = max((s["clear_dark_hours"] for s in sites), default=0)
    total_dark = sum(s["dark_hours"] for s in sites)
    etag = f'"v2-{month}-{len(sites)}-{max_clear}-{total_dark}"'
    headers = {"Cache-Control": _SITES_CACHE_CONTROL, "ETag": etag}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return {
        "month": month,
        "sites": sites,
        "count": len(sites),
    }
