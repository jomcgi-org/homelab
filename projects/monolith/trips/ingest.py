"""Server-side EXIF -> TripPoint construction.

The single shared point builder used by both the live HTTP ingestion endpoint
and the run-by-hand recovery backfill. ``build_point`` is pure: it takes raw
image bytes plus metadata and returns a dict shaped to construct a
``TripPoint``. No DB, no S3, no network (elevation is left None and filled in
out of band by the caller).
"""

import tempfile
from datetime import datetime
from pathlib import Path

from trips import exif, transform


def build_point(
    *,
    trip_slug: str,
    image_bytes: bytes,
    image_key: str,
    source: str,
    tags: list[str] | None,
    tz: str,
) -> dict:
    """Extract a TripPoint-shaped dict from raw image bytes.

    Raises ``ValueError`` if the image carries no usable GPS coordinates.
    """
    with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
        tmp.write(image_bytes)
        tmp.flush()
        lat, lng, taken_iso, optics = exif.extract_exif(Path(tmp.name))

    if lat is None or lng is None or not transform.is_valid_coordinates(lat, lng):
        raise ValueError("image has no GPS coordinates")

    taken_at = transform.localize(taken_iso, tz, fallback=datetime.now())

    point: dict = {
        "trip_slug": trip_slug,
        "id": transform.point_id_from_image_key(image_key),
        "lat": round(lat, 5),
        "lng": round(lng, 5),
        "taken_at": taken_at,
        "image": image_key,
        "source": source,
        "tags": list(tags) if tags else [],
        # Elevation needs a network lookup; keep build_point pure and let the
        # caller backfill it out of the hot path.
        "elevation": None,
    }

    if optics is not None and not optics.is_empty():
        point["light_value"] = optics.light_value
        point["iso"] = optics.iso
        point["shutter_speed"] = optics.shutter_speed
        point["aperture"] = optics.aperture
        point["focal_length_35mm"] = optics.focal_length_35mm

    return point
