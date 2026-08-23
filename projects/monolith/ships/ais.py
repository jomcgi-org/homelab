"""Pure AIS logic: message parsing, deduplication, moored detection, haversine.

Ported from the standalone marine app (projects/ships/ingest/main.py and
projects/ships/backend/main.py). This module is intentionally side-effect free:
no database, no network, no FastAPI. The stateless persister calls
should_insert_position with the prior position it read back from Postgres, so
deduplication stays pure (the prior position is passed in, never cached here).
"""

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("monolith.ships")

# Deduplication settings (read from env, same names/defaults as the old backend).
# Skip a position if within this distance (meters) and speed below threshold.
DEDUP_DISTANCE_METERS = float(os.getenv("DEDUP_DISTANCE_METERS", "100"))
DEDUP_SPEED_THRESHOLD = float(os.getenv("DEDUP_SPEED_THRESHOLD", "0.5"))  # knots
DEDUP_TIME_THRESHOLD = int(os.getenv("DEDUP_TIME_THRESHOLD", "300"))  # seconds

# Moored detection settings.
MOORED_RADIUS_METERS = float(os.getenv("MOORED_RADIUS_METERS", "500"))
MOORED_MIN_DURATION_HOURS = float(os.getenv("MOORED_MIN_DURATION_HOURS", "1"))

# AISStream sends time_utc in a Go-style layout, e.g.
# "2024-01-15 10:00:00.000000000 +0000 UTC". ISO 8601 (the shape used in tests
# and most upstreams) is tried first; this regex handles the Go fallback.
_AIS_TIME_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"(?P<frac>\.\d+)?\s+(?P<offset>[+-]\d{4})(?:\s+\w+)?$"
)


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in meters using Haversine formula."""
    R = 6371000  # Earth's radius in meters

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


@dataclass(frozen=True)
class PriorPosition:
    """Immutable snapshot of a vessel's prior position.

    The persister builds this from a latest_positions row before calling
    should_insert_position. All timestamps are tz-aware datetimes.
    """

    lat: float
    lon: float
    speed: float | None
    recorded_at: datetime
    first_seen_at_location: datetime | None


def should_insert_position(
    data: dict, last: PriorPosition | None
) -> tuple[bool, datetime | None]:
    """Decide whether a position should be inserted (deduplication logic).

    Pure: the prior position is passed in as last (None means first sighting),
    not read from any cache. Returns (should_insert, first_seen_at_location).

    Ported from projects/ships/backend/main.py should_insert_position. The only
    behavioural change: timestamps are tz-aware datetimes (data["recorded_at"]
    and last.recorded_at) instead of ISO strings, so no fromisoformat parsing.
    """
    mmsi = data.get("mmsi")
    if not mmsi:
        return False, None

    lat = data.get("lat", 0)
    lon = data.get("lon", 0)
    recorded_at = data.get("recorded_at")

    if last is None:
        return True, recorded_at  # First position for this vessel

    # Always insert if speed is above threshold (vessel is moving).
    speed = data.get("speed") or 0
    if speed > DEDUP_SPEED_THRESHOLD:
        # Check if moved significantly to reset first_seen.
        distance = haversine_distance(last.lat, last.lon, lat, lon)
        first_seen = (
            last.first_seen_at_location
            if distance <= MOORED_RADIUS_METERS
            else recorded_at
        )
        return True, first_seen

    # Calculate distance from last position.
    distance = haversine_distance(last.lat, last.lon, lat, lon)

    # Insert if moved more than threshold.
    if distance > DEDUP_DISTANCE_METERS:
        first_seen = (
            last.first_seen_at_location
            if distance <= MOORED_RADIUS_METERS
            else recorded_at
        )
        return True, first_seen

    # Check time since last update.
    try:
        time_diff = (recorded_at - last.recorded_at).total_seconds()

        # Insert if enough time has passed (even for stationary vessels).
        if time_diff > DEDUP_TIME_THRESHOLD:
            # Still in same area, keep original first_seen.
            return True, last.first_seen_at_location or recorded_at
    except (ValueError, TypeError, AttributeError):
        return True, recorded_at  # Insert if timestamp arithmetic fails

    return False, None


def parse_eta(eta: dict | None) -> datetime | None:
    """Convert an AISStream ETA dict to a tz-aware datetime.

    AIS ETA (per ITU-R M.1371-5) has no year, only Month, Day, Hour, Minute.
    Unavailable values: Month=0, Day=0, Hour=24, Minute=60.

    We infer the year: if the date is in the past, assume next year.

    Ported from projects/ships/ingest/main.py format_eta, returning a datetime
    instead of an ISO string (the Vessel.eta column is a datetime).
    """
    if not eta or not isinstance(eta, dict):
        return None

    month = eta.get("Month", 0)
    day = eta.get("Day", 0)
    hour = eta.get("Hour", 24)
    minute = eta.get("Minute", 60)

    # Month=0 or Day=0 means unavailable.
    if month == 0 or day == 0:
        return None

    # Hour=24 or Minute=60 means unavailable, default to 00:00.
    if hour == 24:
        hour = 0
    if minute == 60:
        minute = 0

    # Infer year: if date is in the past, use next year.
    now = datetime.now(timezone.utc)
    year = now.year

    try:
        eta_dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        if eta_dt < now:
            eta_dt = datetime(year + 1, month, day, hour, minute, tzinfo=timezone.utc)
        return eta_dt
    except (ValueError, TypeError):
        # Invalid date (e.g. Feb 30) or non-numeric field.
        return None


def _parse_ais_time(value: object) -> datetime | None:
    """Parse an AISStream time_utc value into a tz-aware datetime, or None.

    Tolerant: returns None on missing/invalid input rather than raising.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()

    # ISO 8601 (e.g. "2024-01-15T10:00:00Z"), the shape used by test fixtures.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    # AISStream Go-style layout (e.g. "2024-01-15 10:00:00.000000000 +0000 UTC").
    match = _AIS_TIME_RE.match(text)
    if not match:
        return None
    base = match.group("base")
    frac = match.group("frac")
    offset = match.group("offset")
    try:
        sign = -1 if offset[0] == "-" else 1
        tz = timezone(
            sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        )
        micros = int(round(float(frac) * 1_000_000)) if frac else 0
        return datetime.strptime(base, "%Y-%m-%d %H:%M:%S").replace(
            microsecond=micros, tzinfo=tz
        )
    except (ValueError, TypeError):
        return None


def _parse_position(
    message: dict, mmsi: str, metadata: dict
) -> tuple[str, dict] | tuple[None, None]:
    """Build a position dict from a PositionReport message."""
    position = (message.get("Message") or {}).get("PositionReport") or {}
    if not position:
        return None, None

    lat = position.get("Latitude")
    lon = position.get("Longitude")
    # Skip if coordinates are invalid (mirrors the old ingest early return).
    if lat is None or lon is None:
        return None, None

    data = {
        "mmsi": mmsi,
        "lat": lat,
        "lon": lon,
        "speed": position.get("Sog"),  # Speed over ground
        "course": position.get("Cog"),  # Course over ground
        "heading": position.get("TrueHeading"),
        "nav_status": position.get("NavigationalStatus"),
        "ship_name": (metadata.get("ShipName") or "").strip(),
        "recorded_at": _parse_ais_time(metadata.get("time_utc")),
    }
    return "position", data


def _parse_vessel(
    message: dict, mmsi: str, metadata: dict
) -> tuple[str, dict] | tuple[None, None]:
    """Build a vessel dict from a ShipStaticData message."""
    static = (message.get("Message") or {}).get("ShipStaticData") or {}
    if not static:
        return None, None

    # Dimensions: A=bow, B=stern, C=port, D=starboard to reference point.
    dimension = static.get("Dimension") or {}
    imo = static.get("ImoNumber")

    data = {
        "mmsi": mmsi,
        # The Vessel.imo column is a string; AISStream sends an int.
        "imo": str(imo) if imo is not None else None,
        "call_sign": (static.get("CallSign") or "").strip(),
        "name": (static.get("Name") or "").strip()
        or (metadata.get("ShipName") or "").strip(),
        "ship_type": static.get("Type"),
        "dimension_a": dimension.get("A"),
        "dimension_b": dimension.get("B"),
        "dimension_c": dimension.get("C"),
        "dimension_d": dimension.get("D"),
        "destination": (static.get("Destination") or "").strip(),
        "eta": parse_eta(static.get("Eta")),
        "draught": static.get("MaximumStaticDraught"),
    }
    return "vessel", data


def parse_message(raw: str | bytes) -> tuple[str, dict] | tuple[None, None]:
    """Parse a raw AISStream JSON message.

    Returns ("position", {...}) for a PositionReport, ("vessel", {...}) for a
    ShipStaticData, or (None, None) for any other type, missing MMSI, or parse
    failure. Tolerant: invalid input never raises.

    Combines the parsing in projects/ships/ingest/main.py (process_message,
    _process_position_report, _process_static_data) with the position/vessel
    routing the old backend did by NATS subject. The output dict keys line up
    with the ships.models columns so the persister can insert them directly.
    """
    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, None
    if not isinstance(message, dict):
        return None, None

    metadata = message.get("MetaData")
    if not isinstance(metadata, dict):
        return None, None

    mmsi = str(metadata.get("MMSI", ""))
    if not mmsi:
        return None, None

    msg_type = message.get("MessageType")
    try:
        if msg_type == "PositionReport":
            return _parse_position(message, mmsi, metadata)
        if msg_type == "ShipStaticData":
            return _parse_vessel(message, mmsi, metadata)
    except (AttributeError, TypeError, ValueError):
        return None, None

    return None, None
