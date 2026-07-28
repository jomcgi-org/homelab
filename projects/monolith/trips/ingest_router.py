"""Private trip-image ingestion endpoint.

``POST /api/trips/ingest`` accepts a single geotagged image, derives a
``TripPoint`` from its EXIF via the shared ``ingest.build_point``, stores the
bytes in the ``monolith-trips`` SeaweedFS bucket (content-addressed) and upserts
the point row.

Auth: Cloudflare Access JWT validated at the Envoy gateway (cf-ingress
SecurityPolicy on the private HTTPRoute); no app-level key, consistent with the
rest of the private tier.

This router ships in the PRIVATE monolith image only: it is never wired into the
public read tier, so the write path is unreachable from the internet.
"""

import hashlib

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session

from core.db import get_session
from trips import ingest, s3
from trips.models import Trip, TripPoint

_DEFAULT_TZ = "America/Vancouver"

router = APIRouter(prefix="/api/trips", tags=["trips-ingest"])


def _trip_tz(session: Session, trip_slug: str) -> str:
    """The trip's IANA timezone, or the default when the trip is unknown."""
    trip = session.get(Trip, trip_slug)
    return trip.tz if trip is not None else _DEFAULT_TZ


@router.post("/ingest", status_code=201)
async def ingest_image(
    trip: str,
    source: str = "gopro",
    tags: str = "",
    image: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Ingest one geotagged image into a trip.

    Auth is enforced at the Envoy gateway (Cloudflare Access JWT), not here.

    Order matters: ``build_point`` validates the bytes are a decodable image
    and carry GPS BEFORE the S3 put, so a corrupt or GPS-less image never
    leaves an orphaned object. ``merge`` upserts on the composite PK, so
    re-POSTing the same image (same content-addressed key) is idempotent.
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
    except ValueError as exc:
        # Covers both decode-validation (corrupt/undersized) and the no-GPS
        # rejection; surface the specific reason rather than a fixed message.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    s3.put_image(image_key, data, content_type=image.content_type or "image/jpeg")

    session.merge(TripPoint(**point))
    session.commit()

    return {"id": point["id"], "image": image_key}
