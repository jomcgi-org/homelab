"""Server-side EXIF -> TripPoint construction.

The single shared point builder used by both the live HTTP ingestion endpoint
and the run-by-hand recovery backfill. ``build_point`` is pure: it takes raw
image bytes plus metadata and returns a dict shaped to construct a
``TripPoint``. No DB, no S3, no network (elevation is left None and filled in
out of band by the caller).
"""

import io
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, UnidentifiedImageError

from trips import exif, transform

# A real photo is KB to MB; a sub-kilobyte payload is almost certainly a
# truncated upload or an S3 error body (the trips migration stored 67-byte
# "operation Lookup failed" bodies as images), never a usable raster.
_MIN_IMAGE_BYTES = 256


def validate_image(data: bytes) -> None:
    """Raise ``ValueError`` if ``data`` is not a decodable raster image.

    Two cheap structural checks before any EXIF / S3 / DB work: a size floor
    that rejects error bodies and truncated uploads, then a Pillow
    ``Image.verify()`` that confirms the bytes are a parseable image without a
    full decode. ``verify()`` leaves the Image object unusable afterwards, so
    callers must re-open from the original bytes for any further work (EXIF
    extraction re-opens from a temp file, which is fine).
    """
    if not data or len(data) < _MIN_IMAGE_BYTES:
        raise ValueError(
            f"image too small ({len(data) if data else 0} bytes), "
            "likely corrupt or an error body"
        )
    try:
        Image.open(io.BytesIO(data)).verify()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ValueError(f"not a valid image: {exc}") from exc


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

    Raises ``ValueError`` if the bytes are not a decodable image, or if the
    image carries no usable GPS coordinates.
    """
    # Reject corrupt / undersized payloads before touching the temp file, EXIF,
    # S3 or the DB. validate_image leaves nothing for us to reuse, so EXIF
    # extraction below re-opens the bytes independently.
    validate_image(image_bytes)

    with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
        tmp.write(image_bytes)
        tmp.flush()
        lat, lng, taken_iso, optics = exif.extract_exif(Path(tmp.name))

    if lat is None or lng is None or not transform.is_valid_coordinates(lat, lng):
        raise ValueError("image has no GPS coordinates")

    # localize() returns the fallback verbatim when the EXIF timestamp is
    # missing or unparseable, and its contract requires a tz-aware value (the
    # taken_at column is TIMESTAMPTZ), so the fallback must carry tzinfo.
    taken_at = transform.localize(taken_iso, tz, fallback=datetime.now(ZoneInfo(tz)))

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
