"""Unit tests for trips.backfill.transform (pure, no I/O)."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from trips.backfill import transform

_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document><Placemark><LineString><coordinates>
    -123.1,49.2,0 -120.3,50.6,0 -118.0,52.9,0
  </coordinates></LineString></Placemark></Document>
</kml>"""


def test_is_valid_coordinates():
    assert transform.is_valid_coordinates(49.2, -123.1)
    assert not transform.is_valid_coordinates(0.0, 0.0)  # null island
    assert not transform.is_valid_coordinates(91.0, 0.0)
    assert not transform.is_valid_coordinates(0.0, 181.0)


def test_point_id_from_image_key():
    assert transform.point_id_from_image_key("img_2d30f3c65619.jpg") == "2d30f3c65619"
    assert transform.point_id_from_image_key("img_abc123.png") == "abc123"


def test_gap_point_id_is_deterministic():
    a = transform.gap_point_id(50.6, -120.3, "2025-01-03T10:28:00")
    b = transform.gap_point_id(50.6, -120.3, "2025-01-03T10:28:00")
    assert a == b
    assert a.startswith("gap_")
    assert a != transform.gap_point_id(50.7, -120.3, "2025-01-03T10:28:00")


def test_localize_naive_applies_zone():
    fallback = datetime(2000, 1, 1, tzinfo=timezone.utc)
    out = transform.localize("2025-01-03T18:30:00", "America/Vancouver", fallback)
    assert out.tzinfo == ZoneInfo("America/Vancouver")
    assert out.hour == 18
    # 18:30 PST is 02:30 UTC next day.
    assert out.astimezone(timezone.utc).hour == 2


def test_localize_missing_uses_fallback():
    fallback = datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert transform.localize(None, "America/Vancouver", fallback) == fallback
    assert transform.localize("not-a-date", "America/Vancouver", fallback) == fallback


def test_localize_passes_through_aware():
    fallback = datetime(2000, 1, 1, tzinfo=timezone.utc)
    out = transform.localize("2025-01-03T18:30:00+00:00", "America/Vancouver", fallback)
    assert out == datetime(2025, 1, 3, 18, 30, tzinfo=timezone.utc)


def test_parse_kml_coordinates():
    coords = transform.parse_kml_coordinates(_KML)
    assert coords == [(49.2, -123.1), (50.6, -120.3), (52.9, -118.0)]


def test_sample_coordinates_keeps_endpoints():
    coords = [(float(i), 0.0) for i in range(100)]
    sampled = transform.sample_coordinates(coords, 10)
    assert len(sampled) <= 11
    assert sampled[0] == (0.0, 0.0)
    assert sampled[-1] == (99.0, 0.0)
    # No thinning needed when already under the cap.
    assert transform.sample_coordinates(coords[:5], 10) == coords[:5]


def test_gap_points_shape():
    coords = [(49.2, -123.1), (50.6, -120.3)]
    start = datetime(2025, 1, 3, 10, 28, 0)
    pts = transform.gap_points(coords, start, "America/Vancouver")
    assert len(pts) == 2
    first = pts[0]
    assert first["image"] is None
    assert first["source"] == "gap"
    assert first["tags"] == ["gap", "car"]
    assert first["taken_at"].tzinfo == ZoneInfo("America/Vancouver")
    # ids are deterministic and ordered timestamps differ.
    assert pts[0]["id"] != pts[1]["id"]
    assert pts[1]["taken_at"] > pts[0]["taken_at"]


def test_gap_points_skips_invalid():
    pts = transform.gap_points(
        [(0.0, 0.0), (50.6, -120.3)], datetime(2025, 1, 3, 10, 28, 0), "UTC"
    )
    assert len(pts) == 1
    assert pts[0]["lat"] == 50.6
