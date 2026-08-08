"""Stars HTTP API. SSR-only: never added to httproute-public.yaml.

Two read endpoints back the /app/stars dark-sky planner:

- ``GET /api/stars/sites``, every dark-sky site (from stars.sites, sourced from
  the light-pollution grid) joined with its upcoming clear-dark viewing hours,
  for the site list + detail cards (the live layer).
- ``GET /api/stars/history``, every site's full 12-month clear-dark-hours
  breakdown from the ERA5/CERRA climatology (stars.site_month_climatology, ADR
  009), in one payload. The frontend filters to a month, sums the all-year view
  and draws the per-site chart client-side, so there is no per-month or per-site
  round trip.

The stars v2 metric is a concrete count of clear dark hours (sun < -12 deg and
cloud < 10%), not the old continuous quality Q = darkness x cloud x weather.
``clear_dark_hours`` is the count of upcoming clear-dark hours and drives the map
ordering and marker colour on the live layer.

For ~7 weeks each midsummer Scotland gets no astronomical darkness (the sun never
drops past -12 deg), which would leave the live layer empty. The /sites endpoint
adds a twilight fallback: it also reports ``clear_twilight_hours`` (clear hours
down to a -10 deg floor) and a page-level ``darkness`` mode (astronomical /
twilight / none) so the frontend can surface the darkest available windows with a
disclaimer instead of showing nothing. The strict -12 metric and the historical
climatology are unchanged; this only widens the live layer's fallback.

Reached only from SvelteKit SSR (``http://localhost:8000`` in the same pod);
the /app/stars page is the public surface and the CDN fans out to viewers, per
ADR 002. The live sites payload is materialized by the Argo stars-refresh job
and read from SeaweedFS here. Conditional GETs short-circuit with a 304 via
ETag.

The Argo materializer applies the read-time hour filter
(``hour_time >= top_of_hour(now)``) before publishing. The web endpoint serves
that compact snapshot and never hydrates the forecast table.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session, select

from core.db import get_session
from shared.forecast_freshness import top_of_hour
from stars.models import Site, SiteHour, SiteMonthClimatology
from stars.scoring import CLEAR_CLOUD_MAX_PCT, is_dark_hour, is_twilight_hour

logger = logging.getLogger("monolith.stars")

router = APIRouter(prefix="/api/stars", tags=["stars"])

# Cap each site's upcoming-window list. 25 is well above the ~8 the card shows at
# once: the surplus is headroom so the client can drop already-elapsed hours and
# still have future windows to list through a night. The map field, the night
# buckets and the card all derive from this one array client-side, so a coloured
# cell can never disagree with an empty card. clear_dark_hours /
# clear_twilight_hours still report the full counts for ranking.
_DISPLAY_HOURS = 25

# Forecasts refresh hourly, so 30 min edge freshness with a 1 h SWR window is
# plenty; max-age=0 makes the browser revalidate rather than hold a stale copy.
# Mirrors STARS_SITES_CACHE_CONTROL in frontend/src/lib/cache-headers.js if/when
# that constant is added; keep in sync.
_SITES_CACHE_CONTROL = "public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400"

# The historical layer is effectively IMMUTABLE between reloads: the bytes change
# only when the (roughly yearly) ERA5/CERRA climatology reload runs, so cache it
# at the edge for a year and invalidate explicitly. The reload is a manual op
# (backfill_cerra.py -> upload -> stars.load_climatology), so its runbook purges
# the Cloudflare cache for /app/stars/history* right after, the two always happen
# together, so the cache can never be wrongly stale. max-age=0 keeps the browser
# off a private stale copy so a CDN purge fully invalidates. NOTE: a code change
# to the /history response shape also needs that same purge. Mirrors
# STARS_HISTORY_CACHE_CONTROL in frontend/src/lib/cache-headers.js; keep in sync.
_HISTORY_CACHE_CONTROL = "public, max-age=0, s-maxage=31536000, stale-while-revalidate=604800, stale-if-error=604800"


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
):
    """Serve the compact forecast snapshot materialized by the Argo job."""
    try:
        body, etag = _read_materialized_sites()
    except Exception as exc:  # noqa: BLE001 - turn storage failure into 503
        logger.exception("stars sites materialized payload unavailable")
        raise HTTPException(status_code=503, detail="stars sites unavailable") from exc

    headers = {"Cache-Control": _SITES_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@router.get("/map")
def get_map(request: Request):
    """Serve the small map-only snapshot produced by the refresh job."""
    try:
        body, etag = _read_materialized_object("STARS_MAP_S3_KEY", "map.json")
    except Exception as exc:  # noqa: BLE001 - turn storage failure into 503
        logger.exception("stars map materialized payload unavailable")
        raise HTTPException(status_code=503, detail="stars map unavailable") from exc
    headers = {"Cache-Control": _SITES_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@router.get("/history-map")
def get_history_map(request: Request):
    """Serve the compact historical map snapshot produced by the refresh job."""
    try:
        body, etag = _read_materialized_object(
            "STARS_HISTORY_MAP_S3_KEY", "history-map.json"
        )
    except Exception as exc:  # noqa: BLE001 - turn storage failure into 503
        logger.exception("stars history map materialized payload unavailable")
        raise HTTPException(
            status_code=503, detail="stars history map unavailable"
        ) from exc
    headers = {"Cache-Control": _HISTORY_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def _read_materialized_sites() -> tuple[bytes, str]:
    """Read the precomputed sites payload from SeaweedFS."""
    return _read_materialized_object("STARS_SITES_S3_KEY", "sites.json")


def _read_materialized_object(key_env: str, default_key: str) -> tuple[bytes, str]:
    """Read one bounded materialized object from SeaweedFS."""
    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError("SEAWEEDFS_S3_ENDPOINT is not configured")

    from botocore.exceptions import ClientError
    from stars.grid import _s3_client

    bucket = os.environ.get("STARS_GRID_S3_BUCKET", "stars")
    key = os.environ.get(key_env, default_key)
    try:
        obj = _s3_client().get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchBucket", "404", "NotFound"):
            raise RuntimeError(f"missing materialized object {bucket}/{key}") from exc
        raise
    body = obj["Body"].read()
    # The Argo materializer validates the object before publishing it. Keep the
    # web path to one bounded bytes buffer and return it without ORM hydration
    # or JSON parse/serialize churn.
    etag = obj.get("ETag", "").strip('"')
    if not etag:
        raise RuntimeError("materialized stars payload has no ETag")
    return body, f'"artifact-{etag}"'


def _build_sites_from_db(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Build the payload in the Argo materializer, never in the web pod."""
    now = datetime.now(timezone.utc)
    cutoff = top_of_hour(now)

    # Static site metadata, keyed by site id, joined into each row group at read
    # time. Sourced from the stars.sites table (light-pollution grid, ADR 006).
    by_id = {
        site.id: site
        for site in session.exec(
            select(
                Site.id, Site.name, Site.lat, Site.lon, Site.altitude_m, Site.lp_zone
            )
        ).all()
    }

    # Read-time correctness filter: only hours at or after the current clock
    # hour. The prune job is best-effort housekeeping; the endpoint must not
    # trust the table to be already pruned.
    rows = session.exec(
        select(
            SiteHour.site_id,
            SiteHour.hour_time,
            SiteHour.cloud_area_fraction,
            SiteHour.sun_elevation_deg,
            SiteHour.fetched_at,
        ).where(SiteHour.hour_time >= cutoff)
    ).all()

    # Keep only the five scalar fields needed by the response. The previous
    # implementation hydrated ~340k SQLModel objects, which OOM-killed both the
    # web pod and the 512 MiB materializer job.
    by_site: dict[str, list[tuple[datetime, float, float]]] = {}
    max_fetched: datetime | None = None
    for site_id, hour_time, cloud, sun, fetched_at in rows:
        if fetched_at is not None and (max_fetched is None or fetched_at > max_fetched):
            max_fetched = fetched_at
        by_site.setdefault(site_id, []).append((hour_time, cloud, sun))

    # Track, across all sites, whether any site has a true-dark hour (sun < -12,
    # cloud-independent) and whether any kept hour exists at all (< -10). These
    # drive the page-level ``darkness`` mode: astronomical dark is available,
    # only twilight is available, or nothing is. Midsummer in Scotland the dark
    # set is empty for ~7 weeks, so the twilight fallback is what keeps the live
    # layer useful.
    any_dark_hour = False
    any_twilight_hour = False

    sites = []
    for site_id, hours in by_site.items():
        meta = by_id.get(site_id)
        if meta is None:
            # Defensive: the refresh job only writes ids that exist in
            # stars.sites, so this should not happen. Skip rather than emit a
            # site with no metadata.
            logger.debug("stars: skipping unknown site_id %s", site_id)
            continue

        # Clear hours split two ways. clear-DARK (sun < -12 AND cloud < 10) is
        # the unchanged headline metric and drives ranking + history. clear-
        # TWILIGHT (sun < -10 AND cloud < 10) is the wider summer fallback: every
        # clear-dark hour is also a clear-twilight hour, so the twilight count is
        # always >= the dark count. In a normal (dark) night they are equal; only
        # in the midsummer no-true-dark window does twilight exceed dark.
        clear_twilight = [
            h for h in hours if is_twilight_hour(h[2]) and h[1] < CLEAR_CLOUD_MAX_PCT
        ]
        clear_hours = [h for h in clear_twilight if is_dark_hour(h[2])]
        clear_dark_hours = len(clear_hours)
        clear_twilight_hours = len(clear_twilight)

        # Feed the page-level darkness mode. The elevation checks are cloud-
        # independent on purpose: "is there any astronomical darkness (or any
        # twilight) at all tonight" is a sky-geometry question, not a cloud one.
        # Every kept row is already below the -10 floor, but check explicitly so
        # the mode stays correct even against unexpected rows.
        if any(is_dark_hour(h[2]) for h in hours):
            any_dark_hour = True
        if any(is_twilight_hour(h[2]) for h in hours):
            any_twilight_hour = True

        # The site's upcoming clear windows in chronological order, capped at
        # _DISPLAY_HOURS. This is the clear-twilight superset, each hour tagged
        # ``dark`` (true nautical dark, sun < -12) vs a twilight-only fallback
        # hour. The live map field, the night-filter buckets and the card list
        # all derive from this one array on the client, so the marker colour and
        # the card can never disagree; the cap is the headroom that keeps the
        # card from emptying mid-night as the earliest hours elapse. Only the
        # fields the card renders are emitted (time / cloud / dark); the count
        # headlines above carry the magnitudes.
        best_hours = [
            {
                "time": _iso(h[0]),
                "cloud_area_fraction": h[1],
                "dark": is_dark_hour(h[2]),
            }
            for h in sorted(clear_twilight, key=lambda h: h[0])[:_DISPLAY_HOURS]
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
                "clear_twilight_hours": clear_twilight_hours,
                "best_hours": best_hours,
            }
        )

    # Sort by clear-dark first, then clear-twilight: in a normal night this is
    # just the dark ordering, but in the midsummer twilight window (every
    # clear_dark_hours == 0) the twilight count breaks the tie so the ranking is
    # still meaningful (best windows in the darker south float to the top).
    sites.sort(
        key=lambda s: (s["clear_dark_hours"], s["clear_twilight_hours"]),
        reverse=True,
    )

    # Page-level darkness mode, driving the frontend disclaimer:
    #   "astronomical" - at least one site has a true-dark hour (sun < -12);
    #   "twilight"     - no true dark anywhere, but some twilight windows exist;
    #   "none"         - not even twilight (deep midsummer in the far north).
    if any_dark_hour:
        darkness = "astronomical"
    elif any_twilight_hour:
        darkness = "twilight"
    else:
        darkness = "none"

    # The ETag folds in the current clock hour so the CDN turns over hourly even
    # when fetched_at has not changed: as hours fall past the cutoff the payload
    # shrinks, and the cutoff token forces a revalidation at each hour boundary.
    max_fetched_utc = _as_utc(max_fetched)
    etag = (
        f'"v2-{cutoff.isoformat()}-'
        f"{max_fetched_utc.isoformat() if max_fetched_utc else 'none'}-"
        f'{len(sites)}-{darkness}"'
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
        "darkness": darkness,
        "fetched_at": _iso(max_fetched),
    }


@router.get("/history")
def get_history(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Per-site historical clear-dark hours, all 12 months, for every site. SSR-only.

    The historical heatmap layer reads the ERA5/CERRA climatology backfill
    (stars.site_month_climatology, a long-run seasonal baseline ingested by
    stars.load_climatology, ADR 009). The retired live bank-at-prune accumulator
    no longer contributes: history is the climatology alone.

    This is the whole climatology in one payload: each site carries a 12-element
    ``clear`` array (clear-dark hours, index 0 = January) and a matching ``dark``
    array (dark hours), so the frontend filters to a month, sums the all-year view
    and draws the per-site 12-bar chart entirely client-side, with no per-month or
    per-site round trip (the OOM-prone "load the table, filter in Python" path is
    gone: the aggregation is a streamed column read, not ORM hydration).

    Sites with no metadata (dropped from the grid) or zero dark hours across every
    month are omitted. Ordered by all-year clear-dark hours descending so the SSR
    payload and the default all-year view share one stable order; the client
    re-sorts per selected month. CDN-cached; conditional GETs short-circuit with a
    304.
    """
    # Column reads, not ORM hydration: the climatology is ~159k (site, month)
    # rows, and materializing them as SQLModel objects in the memory-capped public
    # pod is what OOM-killed this endpoint. Selecting the four scalar columns
    # streams lightweight Row tuples instead, so the working set is a few MB.
    meta = {
        row.id: row
        for row in session.exec(select(Site.id, Site.name, Site.lat, Site.lon)).all()
    }

    climo = session.exec(
        select(
            SiteMonthClimatology.site_id,
            SiteMonthClimatology.month,
            SiteMonthClimatology.dark_hours,
            SiteMonthClimatology.clear_dark_hours,
        )
    ).all()

    # site_id -> {"clear": [12], "dark": [12]}, indexed 0 = January. The += folds
    # any duplicate (site, month) rows defensively; the table's PK makes them
    # unique today, but the read never assumes it.
    by_site: dict[str, dict[str, list[int]]] = {}
    for site_id, month, dark_hours, clear_dark_hours in climo:
        if not 1 <= month <= 12:
            continue
        agg = by_site.get(site_id)
        if agg is None:
            agg = {"clear": [0] * 12, "dark": [0] * 12}
            by_site[site_id] = agg
        agg["clear"][month - 1] += clear_dark_hours
        agg["dark"][month - 1] += dark_hours

    sites = []
    for site_id, agg in by_site.items():
        site = meta.get(site_id)
        if site is None:
            # Orphans (a site dropped from the grid) are kept in the climatology
            # as historical record but have no metadata to render; skip them.
            logger.debug("stars.history: skipping unknown site_id %s", site_id)
            continue
        if sum(agg["dark"]) <= 0:
            # No dark hours in any month: nothing to show in any view.
            continue
        sites.append(
            {
                "id": site.id,
                "name": site.name,
                "lat": site.lat,
                "lon": site.lon,
                "clear": agg["clear"],
                "dark": agg["dark"],
            }
        )

    sites.sort(key=lambda s: sum(s["clear"]), reverse=True)

    # The ETag folds in the site count and the year totals across every month: it
    # turns over when the climatology backfill reloads. v3 busts the old per-month
    # v2 cache entries.
    total_clear = sum(sum(s["clear"]) for s in sites)
    total_dark = sum(sum(s["dark"]) for s in sites)
    etag = f'"v3-{len(sites)}-{total_clear}-{total_dark}"'
    headers = {"Cache-Control": _HISTORY_CACHE_CONTROL, "ETag": etag}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return {
        "sites": sites,
        "count": len(sites),
    }
