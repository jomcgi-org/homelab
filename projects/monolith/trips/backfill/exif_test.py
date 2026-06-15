"""Unit tests for the pure numeric helpers in trips.backfill.exif."""

from trips.backfill import exif


def test_dms_to_decimal_north_east():
    # 50 deg 40' 28.2" N -> ~50.6745
    assert round(exif.dms_to_decimal((50, 40, 28.2), "N"), 4) == 50.6745


def test_dms_to_decimal_south_west_negative():
    assert exif.dms_to_decimal((120, 0, 0), "W") == -120.0
    assert exif.dms_to_decimal((30, 0, 0), "S") == -30.0


def test_light_value():
    # f/2.5, 1/240s, ISO 393 -> ~8.6 EV
    assert exif.light_value(2.5, 1 / 240, 393) == 8.6
    assert exif.light_value(None, 1 / 240, 393) is None
    assert exif.light_value(2.5, 0, 393) is None


def test_format_shutter_speed():
    assert exif.format_shutter_speed(1 / 240) == "1/240"
    assert exif.format_shutter_speed(2.0) == "2.0s"
    assert exif.format_shutter_speed(0) is None
    assert exif.format_shutter_speed(None) is None


def test_optics_is_empty():
    assert exif.Optics().is_empty()
    assert not exif.Optics(iso=393).is_empty()
    assert not exif.Optics(light_value=8.6).is_empty()
