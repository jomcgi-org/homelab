"""Trips HTTP read API. SSR-only: never added to httproute-public.yaml.

Two read endpoints back the /app/trips pages:

- ``GET /api/trips/trips``, the trip index (slug/title/subtitle/default image)
  for the landing page.
- ``GET /api/trips/trip/{slug}``, one trip's metadata plus its ordered map
  points, for the per-trip map view.

Both are CDN-cached and reached only from SvelteKit SSR (``http://localhost:8000``
in the same pod), so they run at most a handful of times per minute regardless
of how many browsers are watching. This router imports ONLY the data model and
the DB session: it must never pull in the write path (ingest/s3/exif/transform),
which would drag pillow/boto3/defusedxml into the public import closure (guarded
by app/main_public_imports_test.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select

from core.db import get_session
from trips.models import Trip, TripPoint

router = APIRouter(prefix="/api/trips", tags=["trips"])

# Trip content changes rarely (a backfill run at most), so a short browser TTL
# with a long CDN TTL keeps the SSR pages snappy while letting a reload pick up
# fresh data within a day.
_CACHE = "public, max-age=60, s-maxage=300, stale-while-revalidate=3600"


@router.get("/trips")
def list_trips(response: Response, session: Session = Depends(get_session)):
    """All trips for the /app/trips index. SSR-only, CDN-cached.

    Newest-first by slug so the most recent journeys lead the landing page
    (slugs are date-prefixed, e.g. ``2025-liard-hot-springs``).
    """
    trips = session.exec(select(Trip).order_by(Trip.slug.desc())).all()
    response.headers["Cache-Control"] = _CACHE
    return {
        "count": len(trips),
        "trips": [
            {
                "slug": t.slug,
                "title": t.title,
                "short_title": t.short_title,
                "subtitle": t.subtitle,
                "default_image": t.default_image,
            }
            for t in trips
        ],
    }


@router.get("/trip/{slug}")
def get_trip(slug: str, response: Response, session: Session = Depends(get_session)):
    """One trip's metadata plus its ordered map points. SSR-only, CDN-cached."""
    trip = session.get(Trip, slug)
    if trip is None:
        raise HTTPException(status_code=404, detail="trip not found")

    points = session.exec(
        select(TripPoint)
        .where(TripPoint.trip_slug == slug)
        .order_by(TripPoint.taken_at)
    ).all()

    response.headers["Cache-Control"] = _CACHE
    return {
        "trip": {
            "slug": trip.slug,
            "title": trip.title,
            "short_title": trip.short_title,
            "subtitle": trip.subtitle,
            "tz": trip.tz,
            "default_image": trip.default_image,
            "default_zoom": trip.default_zoom,
            "days": trip.days,
            "highlights": trip.highlights,
            "stats": trip.stats,
        },
        "points": [
            {
                "id": p.id,
                "lat": p.lat,
                "lng": p.lng,
                "taken_at": p.taken_at.isoformat() if p.taken_at else None,
                "image": p.image,
                "source": p.source,
                "tags": p.tags,
                "elevation": p.elevation,
                "light_value": p.light_value,
                "iso": p.iso,
                "shutter_speed": p.shutter_speed,
                "aperture": p.aperture,
                "focal_length_35mm": p.focal_length_35mm,
            }
            for p in points
        ],
    }
