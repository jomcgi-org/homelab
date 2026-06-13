"""Stars HTTP API. SSR-only: never added to httproute-public.yaml.

One read endpoint backs the /app/stars dark-sky planner:

- ``GET /api/stars/sites``, every dark-sky site (from stars.sites, sourced from
  the light-pollution grid) joined with its best upcoming viewing hours, for the
  site list + detail cards.

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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session, select

from app.db import get_session
from shared.forecast_freshness import top_of_hour
from stars.models import Site, SiteHour

logger = logging.getLogger("stars")

router = APIRouter(prefix="/api/stars", tags=["stars"])

# Cap each site's hour list to the top N by score, so the payload stays light
# even with a multi-day forecast horizon.
_DISPLAY_HOURS = 8

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
            for h in sorted(hours, key=lambda h: h.score, reverse=True)[:_DISPLAY_HOURS]
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
            }
        )

    sites.sort(key=lambda s: s["best_score"], reverse=True)

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
        "fetched_at": _iso(max_fetched),
    }
