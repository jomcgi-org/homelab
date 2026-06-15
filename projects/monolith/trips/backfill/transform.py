"""Pure transforms for the trips backfill.

No network/disk I/O so these are unit tested directly; main.py wires them to
S3, the elevation API and Postgres. Deterministic id namespaces match the
original publish-* tools so re-derived rows keep stable ids.
"""

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import defusedxml.ElementTree as ET

GAP_KEY_NAMESPACE = uuid.UUID("b2c3d4e5-f6a7-8901-bcde-f23456789012")

_KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}


def is_valid_coordinates(lat: float, lng: float) -> bool:
    """Reject null island (0, 0) and out-of-range coordinates."""
    if lat == 0.0 and lng == 0.0:
        return False
    return -90 <= lat <= 90 and -180 <= lng <= 180


def point_id_from_image_key(image_key: str) -> str:
    """Deterministic point id from an S3 image key.

    Mirrors the old NATS pipeline: 'img_2d30f3c65619.jpg' -> '2d30f3c65619'.
    """
    return image_key.replace("img_", "").rsplit(".", 1)[0]


def gap_point_id(lat: float, lng: float, timestamp: str) -> str:
    """Deterministic id for a route-only gap point."""
    identity = f"gap:{lat:.5f}:{lng:.5f}:{timestamp}"
    return f"gap_{uuid.uuid5(GAP_KEY_NAMESPACE, identity).hex[:12]}"


def localize(naive_iso: str | None, tz: str, fallback: datetime) -> datetime:
    """Interpret a naive camera-local ISO timestamp in ``tz`` as tz-aware.

    Falls back to ``fallback`` (already tz-aware) when the timestamp is missing
    or unparseable.
    """
    zone = ZoneInfo(tz)
    if not naive_iso:
        return fallback
    try:
        parsed = datetime.fromisoformat(naive_iso)
    except ValueError:
        return fallback
    if parsed.tzinfo is not None:
        return parsed
    return parsed.replace(tzinfo=zone)


def parse_kml_coordinates(kml_text: str) -> list[tuple[float, float]]:
    """Extract (lat, lng) tuples from the LineStrings in a KML document."""
    root = ET.fromstring(kml_text)
    coords: list[tuple[float, float]] = []
    for linestring in root.findall(".//kml:LineString/kml:coordinates", _KML_NS):
        if not linestring.text:
            continue
        for token in linestring.text.strip().split():
            parts = token.split(",")
            if len(parts) >= 2:
                lng, lat = float(parts[0]), float(parts[1])
                coords.append((lat, lng))
    return coords


def sample_coordinates(
    coords: list[tuple[float, float]], max_points: int
) -> list[tuple[float, float]]:
    """Uniformly thin a coordinate list while keeping its shape and endpoints."""
    if max_points <= 0 or len(coords) <= max_points:
        return list(coords)
    step = len(coords) / max_points
    sampled = [coords[int(i * step)] for i in range(max_points)]
    if sampled[-1] != coords[-1]:
        sampled.append(coords[-1])
    return sampled


def gap_points(
    coords: list[tuple[float, float]], start: datetime, tz: str
) -> list[dict]:
    """Build route-only gap point dicts from a sampled coordinate list.

    Timestamps are sequential milliseconds after ``start`` (naive, camera-local)
    purely for ordering; ``start`` is localized via ``tz``.
    """
    points = []
    for i, (lat, lng) in enumerate(coords):
        if not is_valid_coordinates(lat, lng):
            continue
        ts = (start + timedelta(milliseconds=i)).isoformat()
        points.append(
            {
                "id": gap_point_id(lat, lng, ts),
                "lat": round(lat, 5),
                "lng": round(lng, 5),
                "taken_at": localize(ts, tz, start.replace(tzinfo=ZoneInfo(tz))),
                "image": None,
                "source": "gap",
                "tags": ["gap", "car"],
            }
        )
    return points
