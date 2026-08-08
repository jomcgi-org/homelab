"""Extract GPS, timestamp, and camera optics fields from trip photos."""

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image
from PIL.ExifTags import GPSTAGS, TAGS

logger = logging.getLogger("monolith.trips.exif")


@dataclass
class Optics:
    """Camera exposure data from EXIF."""

    light_value: float | None = None  # Exposure Value (EV)
    iso: int | None = None
    shutter_speed: str | None = None  # e.g. "1/240"
    aperture: float | None = None  # f-number
    focal_length_35mm: int | None = None

    def is_empty(self) -> bool:
        return not any(
            (
                self.light_value,
                self.iso,
                self.shutter_speed,
                self.aperture,
                self.focal_length_35mm,
            )
        )


def gps_info(exif_gps: dict) -> dict:
    """Map raw GPSInfo tag ids to their names."""
    return {GPSTAGS.get(key, key): val for key, val in exif_gps.items()}


def dms_to_decimal(dms: tuple, ref: str) -> float:
    """Convert GPS coordinates from degrees/minutes/seconds to decimal."""
    degrees, minutes, seconds = dms
    decimal = float(degrees) + float(minutes) / 60 + float(seconds) / 3600
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


def light_value(
    aperture: float | None, shutter_time: float | None, iso: int | None
) -> float | None:
    """Exposure Value from the exposure triangle.

    LV = log2(N^2 / t) - log2(ISO / 100), N=aperture, t=shutter seconds.
    """
    if aperture is None or shutter_time is None or iso is None:
        return None
    if aperture <= 0 or shutter_time <= 0 or iso <= 0:
        return None
    try:
        lv = math.log2((aperture**2) / shutter_time) - math.log2(iso / 100)
        return round(lv, 1)
    except (ValueError, ZeroDivisionError):
        return None


def format_shutter_speed(exposure_time: float | None) -> str | None:
    """Format exposure time as a readable shutter string (0.004166.. -> '1/240')."""
    if exposure_time is None or exposure_time <= 0:
        return None
    if exposure_time >= 1:
        return f"{exposure_time:.1f}s"
    return f"1/{round(1 / exposure_time)}"


def extract_exif(
    image_path: Path,
) -> tuple[float | None, float | None, str | None, Optics | None]:
    """Extract (lat, lng, camera-local ISO timestamp, optics) from an image."""
    try:
        img = Image.open(image_path)
        raw = img._getexif()
        if not raw:
            return None, None, None, None

        exif = {TAGS.get(tag_id, tag_id): value for tag_id, value in raw.items()}

        lat = lng = timestamp = None

        if "GPSInfo" in exif:
            gps = gps_info(exif["GPSInfo"])
            if "GPSLatitude" in gps and "GPSLongitude" in gps:
                lat = dms_to_decimal(gps["GPSLatitude"], gps.get("GPSLatitudeRef", "N"))
                lng = dms_to_decimal(
                    gps["GPSLongitude"], gps.get("GPSLongitudeRef", "E")
                )

        # EXIF time is camera-local (no tz); the backfill localizes it later.
        for tag in ("DateTimeOriginal", "DateTime"):
            if tag in exif:
                timestamp = datetime.strptime(
                    exif[tag], "%Y:%m:%d %H:%M:%S"
                ).isoformat()
                break

        optics = Optics()
        iso_raw = exif.get("ISOSpeedRatings")
        if iso_raw:
            optics.iso = int(iso_raw[0] if isinstance(iso_raw, tuple) else iso_raw)

        fnumber = exif.get("FNumber")
        if fnumber:
            optics.aperture = round(float(fnumber), 1)

        exposure = exif.get("ExposureTime")
        exposure_f = None
        if exposure:
            exposure_f = float(exposure)
            optics.shutter_speed = format_shutter_speed(exposure_f)

        focal_35 = exif.get("FocalLengthIn35mmFilm")
        if focal_35:
            optics.focal_length_35mm = int(focal_35)

        optics.light_value = light_value(optics.aperture, exposure_f, optics.iso)

        return lat, lng, timestamp, (None if optics.is_empty() else optics)
    except Exception as exc:  # noqa: BLE001 - best-effort EXIF, log and skip
        logger.warning("could not extract EXIF from %s: %s", image_path, exc)
        return None, None, None, None
