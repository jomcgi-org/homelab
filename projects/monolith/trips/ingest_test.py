"""Unit tests for the shared EXIF -> TripPoint builder in trips.ingest.

Uses two tiny committed JPEG fixtures (trips/testdata/): one geotagged with
GPS + timestamp + optics EXIF, one with no EXIF at all. The fixtures are read
by the real trips.exif.extract_exif path so build_point is exercised exactly as
the live ingest endpoint and the recovery backfill will call it.
"""

from pathlib import Path

import pytest

from trips import ingest

_TESTDATA = Path(__file__).parent / "testdata"
_GEOTAGGED = _TESTDATA / "geotagged.jpg"
_NO_GPS = _TESTDATA / "no_gps.jpg"


def test_build_point_extracts_geo_time_optics():
    point = ingest.build_point(
        trip_slug="bc-2024",
        image_bytes=_GEOTAGGED.read_bytes(),
        image_key="img_point-01.jpg",  # gitleaks:allow - test fixture key, not a secret
        source="gopro",
        tags=["scenic"],
        tz="America/Vancouver",
    )

    assert point["trip_slug"] == "bc-2024"
    # id is derived from the image key (img_ prefix + extension stripped).
    assert point["id"] == "point-01"
    assert -90 <= point["lat"] <= 90
    assert -180 <= point["lng"] <= 180
    assert point["lat"] != 0.0 and point["lng"] != 0.0
    assert point["taken_at"] is not None
    assert point["image"] == "img_point-01.jpg"
    assert point["source"] == "gopro"
    assert point["tags"] == ["scenic"]
    assert point["elevation"] is None
    # Optics from the fixture EXIF flow through.
    assert point["iso"] == 393
    assert point["aperture"] == 2.5
    assert point["shutter_speed"] == "1/240"
    assert point["focal_length_35mm"] == 24
    assert point["light_value"] is not None


def test_build_point_rejects_missing_gps():
    with pytest.raises(ValueError, match="no GPS"):
        ingest.build_point(
            trip_slug="bc-2024",
            image_bytes=_NO_GPS.read_bytes(),
            image_key="img_blank.jpg",
            source="gopro",
            tags=None,
            tz="America/Vancouver",
        )


def test_build_point_rejects_corrupt_bytes():
    # Non-image bytes that clear the size floor (>1KB) so the Pillow decode
    # branch is what rejects them, mirroring a larger-but-still-garbage payload.
    corrupt = b"operation Lookup failed " * 64  # ~1.5KB of plain text, not a JPEG
    with pytest.raises(ValueError, match="not a valid image|too small"):
        ingest.build_point(
            trip_slug="bc-2024",
            image_bytes=corrupt,
            image_key="img_corrupt.jpg",
            source="gopro",
            tags=None,
            tz="America/Vancouver",
        )


def test_build_point_rejects_tiny():
    with pytest.raises(ValueError, match="too small"):
        ingest.build_point(
            trip_slug="bc-2024",
            image_bytes=b"tinybytes!",  # 10 bytes, below the 1KB floor
            image_key="img_tiny.jpg",
            source="gopro",
            tags=None,
            tz="America/Vancouver",
        )


def test_validate_image_accepts_real_jpeg():
    # A real fixture passes verify(); guards against an over-eager floor.
    ingest.validate_image(_GEOTAGGED.read_bytes())
