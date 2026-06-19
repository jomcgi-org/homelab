"""Private authenticated trip-image ingestion endpoint.

``POST /api/trips/ingest`` accepts a single geotagged image, derives a
``TripPoint`` from its EXIF via the shared ``ingest.build_point``, stores the
bytes in the ``monolith-trips`` SeaweedFS bucket (content-addressed) and upserts
the point row. Guarded by a static ``X-Trips-Ingest-Key`` header so only the
trusted uploader (the GoPro sync job) can write.

This router ships in the PRIVATE monolith image only: it is never wired into the
public read tier, so the write path is unreachable from the internet.
"""

import hashlib
import os
import secrets

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from sqlmodel import Session

from app.db import get_session
from trips import ingest, s3
from trips.models import Trip, TripPoint

_DEFAULT_TZ = "America/Vancouver"

router = APIRouter(prefix="/api/trips", tags=["trips-ingest"])


def _require_key(x_trips_ingest_key: str = Header(default="")) -> None:
    """Reject requests without a matching ingest key.

    A missing/empty ``TRIPS_INGEST_KEY`` env fails closed: the endpoint is
    unauthenticated-by-omission only if someone forgets to set it, and we would
    rather 401 than accept anonymous writes. ``compare_digest`` keeps the check
    constant-time.
    """
    expected = os.environ.get("TRIPS_INGEST_KEY", "")
    if not expected or not secrets.compare_digest(x_trips_ingest_key, expected):
        raise HTTPException(status_code=401, detail="invalid ingest key")


def _trip_tz(session: Session, trip_slug: str) -> str:
    """The trip's IANA timezone, or the default when the trip is unknown."""
    trip = session.get(Trip, trip_slug)
    return trip.tz if trip is not None else _DEFAULT_TZ


@router.post("/ingest", status_code=201, dependencies=[Depends(_require_key)])
async def ingest_image(
    trip: str,
    source: str = "gopro",
    tags: str = "",
    image: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Ingest one geotagged image into a trip.

    Order matters: ``build_point`` validates GPS BEFORE the S3 put, so a bad
    image never leaves an orphaned object. ``merge`` upserts on the composite
    PK, so re-POSTing the same image (same content-addressed key) is idempotent.
    """
    data = await image.read()
    image_key = f"img_{hashlib.sha256(data).hexdigest()[:12]}.jpg"
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    try:
        point = ingest.build_point(
            trip_slug=trip,
            image_bytes=data,
            image_key=image_key,
            source=source,
            tags=tag_list,
            tz=_trip_tz(session, trip),
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="image has no GPS coordinates")

    s3.put_image(image_key, data, content_type=image.content_type or "image/jpeg")

    session.merge(TripPoint(**point))
    session.commit()

    return {"id": point["id"], "image": image_key}
