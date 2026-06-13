"""Stars HTTP API. SSR-only: never added to httproute-public.yaml.

Two read endpoints back the /app/stars dark-sky planner:

- ``GET /api/stars/sites``, every dark-sky site (from stars.sites, sourced from
  the light-pollution grid) joined with its best upcoming viewing hours, for the
  site list + detail cards (the live layer).
- ``GET /api/stars/history``, per-site accumulated realized quality for a
  calendar month (from stars.site_month_stats, banked by the hourly prune as
  forecast hours elapse), for the historical heatmap layer (ADR 008).

Each hour's ``score`` is the ADR 007 continuous stargazing quality (0..100,
Q = darkness x cloud x weather), not the old additive astronomy score with a
fixed display floor. ``best_score`` is the max quality across a site's future
hours and drives the map ordering and marker colour.

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

logger = logging.getLogger("stars")

router = APIRouter(prefix="/api/stars", tags=["stars"])

# Cap each site's hour list to the top N by score, so the payload stays light
# even with a multi-day forecast horizon.
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
    """All dark-sky sites with their best upcoming hours. SSR-only, CDN-cached."""
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

        best_score = max(h.score for h in hours)

        # Per-night best score, so the map's night filter can recolour each
        # marker by the quality it can reach on the selected night(s).
        night_scores: dict[str, float] = {}
        for h in hours:
            key = _night_key(h.hour_time)
            if key not in night_scores or h.score > night_scores[key]:
                night_scores[key] = h.score

        # Select the best N hours by score (the windows worth showing), then
        # display them in chronological order: a planner reads the night top to
        # bottom, so the card must run by time, not by score.
        top_hours = sorted(hours, key=lambda h: h.score, reverse=True)[:_DISPLAY_HOURS]
        best_hours = [
            {
                "time": _iso(h.hour_time),
                "score": h.score,
                "cloud_area_fraction": h.cloud_area_fraction,
                "relative_humidity": h.relative_humidity,
                "wind_speed": h.wind_speed,
                "air_temperature": h.air_temperature,
                "dew_spread": h.dew_spread,
                "symbol": h.symbol,
            }
            for h in sorted(top_hours, key=lambda h: h.hour_time)
        ]
        sites.append(
            {
                "id": meta.id,
                "name": meta.name,
                "lat": meta.lat,
                "lon": meta.lon,
                "altitude_m": meta.altitude_m,
                "lp_zone": meta.lp_zone,
                "best_score": best_score,
                "best_hours": best_hours,
                "night_scores": night_scores,
            }
        )

    sites.sort(key=lambda s: s["best_score"], reverse=True)

    # Union of nights present across all sites, ascending, so the frontend can
    # render one stable row of night-filter chips (a site missing a night just
    # has no score for it).
    nights = sorted({key for s in sites for key in s["night_scores"]})

    # The ETag folds in the current clock hour so the CDN turns over hourly even
    # when fetched_at has not changed: as hours fall past the cutoff the payload
    # shrinks, and the cutoff token forces a revalidation at each hour boundary.
    max_fetched_utc = _as_utc(max_fetched)
    etag = (
        f'"v1-{cutoff.isoformat()}-'
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
        default=0, ge=0, le=12, description="Month-of-year 1-12; 0 = current UTC month"
    ),
):
    """Per-site combined realized quality for a calendar month. SSR-only.

    The historical heatmap layer (ADR 008 + 009): for the selected month-of-year,
    each site's heat is the sum of the live accumulator (stars.site_month_stats,
    banked by the hourly prune as forecast hours elapse) and the ERA5 climatology
    backfill (stars.site_month_climatology, a long-run seasonal baseline). The two
    tables carry identical sufficient stats, so they compose by per-field addition:
    ``window_count``, ``sum_q``, ``sum_darkness`` and ``sum_clarity`` add, and the
    averages are the combined component sums over the combined ``window_count``.

    A site is included if it appears in EITHER table (full-outer-join by site_id,
    done in Python). ``sum_q`` is the headline heat metric; ``avg_darkness`` /
    ``avg_clarity`` give the decomposition. ``month`` defaults to the current UTC
    month. Sites with no metadata (dropped from the grid) or a zero combined count
    are omitted. Ordered by combined ``sum_q`` descending. CDN-cached with the same
    headers as ``/sites``; conditional GETs short-circuit with a 304.
    """
    selected = month or datetime.now(timezone.utc).month

    by_id = {site.id: site for site in session.exec(select(Site)).all()}
    live = session.exec(
        select(SiteMonthStat).where(SiteMonthStat.month == selected)
    ).all()
    climo = session.exec(
        select(SiteMonthClimatology).where(SiteMonthClimatology.month == selected)
    ).all()

    # Full-outer-join the two accumulators by site_id, summing the sufficient
    # stats per field. A site present in only one table contributes that table's
    # values (the other side defaults to zero).
    combined: dict[str, dict[str, float]] = {}
    for row in (*live, *climo):
        agg = combined.setdefault(
            row.site_id,
            {"window_count": 0, "sum_q": 0.0, "sum_darkness": 0.0, "sum_clarity": 0.0},
        )
        agg["window_count"] += row.window_count
        agg["sum_q"] += row.sum_q
        agg["sum_darkness"] += row.sum_darkness
        agg["sum_clarity"] += row.sum_clarity

    sites = []
    for site_id, agg in combined.items():
        meta = by_id.get(site_id)
        if meta is None:
            # Orphans (a site dropped from the grid) are kept in both tables as
            # historical record but have no metadata to render; skip them.
            logger.debug("stars.history: skipping unknown site_id %s", site_id)
            continue
        count = agg["window_count"]
        if count <= 0:
            # Guard against a zero combined count producing a divide-by-zero average.
            continue
        sites.append(
            {
                "id": meta.id,
                "name": meta.name,
                "lat": meta.lat,
                "lon": meta.lon,
                "sum_q": agg["sum_q"],
                "window_count": count,
                "avg_darkness": agg["sum_darkness"] / count,
                "avg_clarity": agg["sum_clarity"] / count,
            }
        )

    sites.sort(key=lambda s: s["sum_q"], reverse=True)

    # The ETag folds in the selected month, the site count, the max combined
    # sum_q and the total combined window_count: it turns over when EITHER the
    # live prune banks more hours or the climatology backfill is reloaded.
    max_sum_q = max((s["sum_q"] for s in sites), default=0.0)
    total_count = sum(s["window_count"] for s in sites)
    etag = f'"v2-{selected}-{len(sites)}-{max_sum_q}-{total_count}"'
    headers = {"Cache-Control": _SITES_CACHE_CONTROL, "ETag": etag}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return {
        "month": selected,
        "sites": sites,
        "count": len(sites),
    }
