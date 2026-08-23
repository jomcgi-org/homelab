"""dr_jobs HTTP API. SSR-only: never added to httproute-public.yaml.

One read endpoint backs the /app/dr-jobs listing:

- ``GET /api/dr-jobs/listings`` returns every scraped vacancy with a server
  computed ``is_live`` flag. Live = seen in the most recent scrape (last_seen_at
  within LIVE_GRACE) AND not past its closing date; everything else is history.
  The page renders both from one payload and toggles between them client-side,
  so the History button needs no second request.

Reached only from SvelteKit SSR (``http://localhost:8000`` in the same pod); the
/app/dr-jobs page is the public surface and the CDN fans the result out. The
ETag folds in the day so the live/history split turns over at midnight even when
no scrape has run, and conditional GETs short-circuit with a 304.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session, select

from core.db import get_session
from dr_jobs.models import Vacancy

logger = logging.getLogger("monolith.dr_jobs")

router = APIRouter(prefix="/api/dr-jobs", tags=["dr_jobs"])

# A vacancy counts as "live" if it was seen within this window of the last
# scrape. The scrape runs daily, so 36 h tolerates a single missed/late run
# before a post ages out of the live view; a longer gap correctly demotes it.
LIVE_GRACE = timedelta(hours=36)

# Scrape runs daily, so 30 min edge freshness with a 1 h SWR window is plenty.
# Mirrors DR_JOBS_LISTINGS_CACHE_CONTROL in
# frontend/src/lib/cache-headers.js, keep in sync.
_LISTINGS_CACHE_CONTROL = "public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400"

# Bump when the listings response SHAPE changes (fields added/removed/renamed),
# so a shape-only code deploy busts every client's data-derived ETag.
_LISTINGS_SCHEMA_VERSION = "v1"


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to tz-aware UTC.

    Postgres returns tz-aware values; SQLite (tests) can return naive ones even
    though we always write tz-aware UTC. Treat naive as UTC so ETag stamps and
    the live cutoff comparison stay stable across both backends.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_live(row: Vacancy, cutoff: datetime, today: date) -> bool:
    """Live = seen in the latest scrape AND not past its closing date."""
    last_seen = _as_utc(row.last_seen_at)
    if last_seen is None or last_seen < cutoff:
        return False
    return row.closing_date is None or row.closing_date >= today


def _listings_etag(count: int, today: date, max_seen: datetime | None) -> str:
    """Stable ETag: schema token + day (live/history rolls at midnight) + the
    freshest last_seen_at (busts on a scrape) + row count (busts on add/drop)."""
    stamp = max_seen.isoformat() if max_seen is not None else "null"
    return f'"{_LISTINGS_SCHEMA_VERSION}-{today.isoformat()}-{stamp}-{count}"'


def _serialize(row: Vacancy, is_live: bool) -> dict:
    return {
        "job_id": row.job_id,
        "reference": row.reference,
        "title": row.title,
        "employment_type": row.employment_type,
        "salary_band": row.salary_band,
        "salary_text": row.salary_text,
        "town": row.town,
        "postcode": row.postcode,
        "region": row.region,
        "posted_date": row.posted_date.isoformat() if row.posted_date else None,
        "closing_date": row.closing_date.isoformat() if row.closing_date else None,
        "url": row.url,
        "first_seen_at": _as_utc(row.first_seen_at).isoformat()
        if row.first_seen_at
        else None,
        "is_live": is_live,
    }


@router.get("/listings")
def get_listings(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Every scraped vacancy with an is_live flag. SSR-only, CDN-cached.

    Live rows sort by soonest closing first (what the partner acts on); history
    rows sort by most-recently closed first. The single payload carries both so
    the page's Live/History toggle is purely client-side.
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    cutoff = now - LIVE_GRACE

    rows = session.exec(select(Vacancy)).all()

    max_seen: datetime | None = None
    for row in rows:
        seen = _as_utc(row.last_seen_at)
        if seen is not None and (max_seen is None or seen > max_seen):
            max_seen = seen

    live: list[dict] = []
    history: list[dict] = []
    for row in rows:
        live_flag = _is_live(row, cutoff, today)
        (live if live_flag else history).append(_serialize(row, live_flag))

    # Soonest-closing first for live; most-recently-closing first for history.
    # date.max/min keep null closing dates out of the way at each end.
    live.sort(key=lambda j: j["closing_date"] or date.max.isoformat())
    history.sort(key=lambda j: j["closing_date"] or date.min.isoformat(), reverse=True)
    jobs = live + history

    etag = _listings_etag(len(jobs), today, max_seen)
    headers = {"Cache-Control": _LISTINGS_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return {
        "count": len(jobs),
        "live_count": len(live),
        "generated_at": max_seen.isoformat() if max_seen is not None else None,
        "jobs": jobs,
    }
