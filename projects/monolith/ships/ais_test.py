"""Unit tests for ships.ais: haversine, deduplication, ETA, message parsing.

Ported from the standalone marine app tests:
- projects/ships/backend/tests/unit_test.py (haversine, should_insert_position)
- projects/ships/ingest/ais_parsing_test.py (message parsing, ETA)

These exercise pure logic only (no DB, no network). The deduplication function
takes the prior position as an argument, so tests build PriorPosition directly.
"""

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from ships.ais import (
    PriorPosition,
    haversine_distance,
    parse_eta,
    parse_message,
    should_insert_position,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# haversine_distance
# ---------------------------------------------------------------------------


class TestHaversineDistance:
    def test_same_point_is_zero(self):
        assert haversine_distance(0.0, 0.0, 0.0, 0.0) == 0.0

    def test_london_to_paris_approx_343km(self):
        d = haversine_distance(51.5074, -0.1278, 48.8566, 2.3522)
        assert 330_000 < d < 360_000, f"Expected ~343 km, got {d:.0f} m"

    def test_one_degree_latitude_approx_111km(self):
        d = haversine_distance(0.0, 0.0, 1.0, 0.0)
        assert 110_000 < d < 112_000, f"Expected ~111 km, got {d:.0f} m"

    def test_short_distance_about_111m(self):
        # 0.001 degree of latitude is roughly 111 m.
        d = haversine_distance(0.0, 0.0, 0.001, 0.0)
        assert 100 < d < 125

    def test_return_type_is_float(self):
        assert isinstance(haversine_distance(0.0, 0.0, 1.0, 1.0), float)

    def test_uses_earth_radius_6371km(self):
        d = haversine_distance(0.0, 0.0, 90.0, 0.0)
        expected = math.pi / 2 * 6_371_000
        assert abs(d - expected) < 1000


# ---------------------------------------------------------------------------
# should_insert_position (pure dedup ladder)
# ---------------------------------------------------------------------------


class TestShouldInsertPosition:
    BASE = datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC)

    def _prior(self, lat, lon, speed=0.0, recorded_at=None, first_seen=None):
        ts = recorded_at or self.BASE
        return PriorPosition(
            lat=lat,
            lon=lon,
            speed=speed,
            recorded_at=ts,
            first_seen_at_location=first_seen or ts,
        )

    def test_no_mmsi_returns_false(self):
        ok, fs = should_insert_position({"lat": 0.0, "lon": 0.0}, None)
        assert ok is False
        assert fs is None

    def test_empty_mmsi_returns_false(self):
        ok, _ = should_insert_position({"mmsi": "", "lat": 0.0, "lon": 0.0}, None)
        assert ok is False

    def test_first_position_always_inserted(self):
        recorded = self.BASE
        data = {
            "mmsi": "123456789",
            "lat": 51.5,
            "lon": -0.1,
            "speed": 0.0,
            "recorded_at": recorded,
        }
        ok, fs = should_insert_position(data, None)
        assert ok is True
        assert fs == recorded

    def test_moving_vessel_always_inserted(self):
        """Speed > 0.5 knots always inserts."""
        last = self._prior(51.5, -0.1, speed=0.0)
        data = {
            "mmsi": "123",
            "lat": 51.5001,
            "lon": -0.1,
            "speed": 5.0,
            "recorded_at": self.BASE + timedelta(minutes=1),
        }
        ok, _ = should_insert_position(data, last)
        assert ok is True

    def test_stationary_same_spot_within_time_threshold_skipped(self):
        """Stationary, same spot, < 300 s elapsed: skip."""
        last = self._prior(51.5, -0.1, speed=0.0, recorded_at=self.BASE)
        data = {
            "mmsi": "123",
            "lat": 51.5,
            "lon": -0.1,
            "speed": 0.0,
            "recorded_at": self.BASE + timedelta(seconds=60),
        }
        ok, fs = should_insert_position(data, last)
        assert ok is False
        assert fs is None

    def test_stationary_same_spot_beyond_time_threshold_inserts_preserving_first_seen(
        self,
    ):
        """Stationary, same spot, > 300 s elapsed: insert and keep first_seen."""
        first_seen = datetime(2024, 1, 1, 8, 0, 0, tzinfo=UTC)
        last = self._prior(
            51.5, -0.1, speed=0.0, recorded_at=self.BASE, first_seen=first_seen
        )
        data = {
            "mmsi": "123",
            "lat": 51.5,
            "lon": -0.1,
            "speed": 0.0,
            "recorded_at": self.BASE + timedelta(seconds=360),
        }
        ok, fs = should_insert_position(data, last)
        assert ok is True
        assert fs == first_seen

    def test_moved_beyond_distance_within_moored_radius_preserves_first_seen(self):
        """Moved > 100 m but within the 500 m moored radius: keep first_seen."""
        first_seen = datetime(2024, 1, 1, 8, 0, 0, tzinfo=UTC)
        last = self._prior(
            51.5, -0.1, speed=0.0, recorded_at=self.BASE, first_seen=first_seen
        )
        # ~200 m north (0.0018 deg lat), still inside 500 m radius.
        data = {
            "mmsi": "123",
            "lat": 51.5018,
            "lon": -0.1,
            "speed": 0.0,
            "recorded_at": self.BASE + timedelta(seconds=60),
        }
        ok, fs = should_insert_position(data, last)
        assert ok is True
        assert fs == first_seen

    def test_moved_beyond_moored_radius_resets_first_seen(self):
        """Moved beyond the 500 m moored radius: reset first_seen to current time."""
        first_seen = datetime(2024, 1, 1, 8, 0, 0, tzinfo=UTC)
        last = self._prior(
            51.5, -0.1, speed=0.0, recorded_at=self.BASE, first_seen=first_seen
        )
        recorded = self.BASE + timedelta(minutes=1)
        # ~1.1 km north (0.01 deg lat), well beyond 500 m.
        data = {
            "mmsi": "123",
            "lat": 51.51,
            "lon": -0.1,
            "speed": 0.0,
            "recorded_at": recorded,
        }
        ok, fs = should_insert_position(data, last)
        assert ok is True
        assert fs == recorded

    def test_none_speed_treated_as_zero(self):
        last = self._prior(51.5, -0.1, speed=None, recorded_at=self.BASE)
        data = {
            "mmsi": "123",
            "lat": 51.5,
            "lon": -0.1,
            "speed": None,
            "recorded_at": self.BASE + timedelta(seconds=60),
        }
        ok, _ = should_insert_position(data, last)
        # Within 100 m and 60 s: deduplicated.
        assert ok is False


# ---------------------------------------------------------------------------
# parse_eta
# ---------------------------------------------------------------------------


class TestParseEta:
    def test_valid_future_eta_returns_datetime(self):
        # Pick a date guaranteed to be in the future this year (or next).
        eta = parse_eta({"Month": 12, "Day": 31, "Hour": 23, "Minute": 59})
        assert isinstance(eta, datetime)
        assert eta.tzinfo is not None
        assert eta.month == 12
        assert eta.day == 31
        assert eta.hour == 23
        assert eta.minute == 59

    def test_unavailable_month_returns_none(self):
        assert parse_eta({"Month": 0, "Day": 1, "Hour": 12, "Minute": 0}) is None

    def test_unavailable_day_returns_none(self):
        assert parse_eta({"Month": 1, "Day": 0, "Hour": 12, "Minute": 0}) is None

    def test_hour_24_minute_60_default_to_midnight(self):
        eta = parse_eta({"Month": 6, "Day": 15, "Hour": 24, "Minute": 60})
        assert eta is not None
        assert eta.hour == 0
        assert eta.minute == 0

    def test_none_input_returns_none(self):
        assert parse_eta(None) is None

    def test_invalid_calendar_date_returns_none(self):
        # February 30 does not exist.
        assert parse_eta({"Month": 2, "Day": 30, "Hour": 12, "Minute": 0}) is None


# ---------------------------------------------------------------------------
# parse_message
# ---------------------------------------------------------------------------


class TestParseMessage:
    def _position_message(self):
        return json.dumps(
            {
                "MessageType": "PositionReport",
                "MetaData": {
                    "MMSI": "123456789",
                    "time_utc": "2024-01-15T10:00:00Z",
                    "ShipName": "  STAR VANCOUVER  ",
                },
                "Message": {
                    "PositionReport": {
                        "Latitude": 48.5,
                        "Longitude": -123.4,
                        "Sog": 10.0,
                        "Cog": 180.0,
                        "TrueHeading": 179,
                        "NavigationalStatus": 0,
                    }
                },
            }
        )

    def _static_message(self):
        return json.dumps(
            {
                "MessageType": "ShipStaticData",
                "MetaData": {
                    "MMSI": "987654321",
                    "time_utc": "2024-01-15T10:00:00Z",
                    "ShipName": "FALLBACK",
                },
                "Message": {
                    "ShipStaticData": {
                        "ImoNumber": 9074729,
                        "CallSign": "  ABCD  ",
                        "Name": "  EVER GIVEN  ",
                        "Type": 70,
                        "Dimension": {"A": 200, "B": 100, "C": 20, "D": 30},
                        "Destination": "  ROTTERDAM  ",
                        "Eta": {"Month": 12, "Day": 31, "Hour": 12, "Minute": 0},
                        "MaximumStaticDraught": 14.5,
                    }
                },
            }
        )

    def test_position_report_parses_to_position_tuple(self):
        kind, data = parse_message(self._position_message())
        assert kind == "position"
        assert data["mmsi"] == "123456789"
        assert data["lat"] == 48.5
        assert data["lon"] == -123.4
        assert data["speed"] == 10.0
        assert data["course"] == 180.0
        assert data["heading"] == 179
        assert data["nav_status"] == 0
        # ShipName whitespace is stripped.
        assert data["ship_name"] == "STAR VANCOUVER"

    def test_position_recorded_at_is_tz_aware_datetime(self):
        _, data = parse_message(self._position_message())
        assert isinstance(data["recorded_at"], datetime)
        assert data["recorded_at"].tzinfo is not None
        assert data["recorded_at"] == datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)

    def test_position_accepts_bytes_input(self):
        kind, data = parse_message(self._position_message().encode())
        assert kind == "position"
        assert data["mmsi"] == "123456789"

    def test_position_missing_coordinates_returns_none(self):
        raw = json.dumps(
            {
                "MessageType": "PositionReport",
                "MetaData": {"MMSI": "123", "time_utc": "2024-01-15T10:00:00Z"},
                "Message": {"PositionReport": {"Sog": 5.0}},
            }
        )
        assert parse_message(raw) == (None, None)

    def test_static_data_parses_to_vessel_tuple(self):
        kind, data = parse_message(self._static_message())
        assert kind == "vessel"
        assert data["mmsi"] == "987654321"
        # ImoNumber (int) is normalised to a string for the Vessel.imo column.
        assert data["imo"] == "9074729"
        assert data["call_sign"] == "ABCD"
        assert data["name"] == "EVER GIVEN"
        assert data["ship_type"] == 70
        assert data["dimension_a"] == 200
        assert data["dimension_d"] == 30
        assert data["destination"] == "ROTTERDAM"
        assert data["draught"] == 14.5

    def test_static_data_eta_is_datetime(self):
        _, data = parse_message(self._static_message())
        assert isinstance(data["eta"], datetime)
        assert data["eta"].month == 12
        assert data["eta"].day == 31

    def test_static_data_name_falls_back_to_metadata_ship_name(self):
        raw = json.dumps(
            {
                "MessageType": "ShipStaticData",
                "MetaData": {"MMSI": "111", "ShipName": "META NAME"},
                "Message": {"ShipStaticData": {"Name": "   ", "Type": 70}},
            }
        )
        kind, data = parse_message(raw)
        assert kind == "vessel"
        assert data["name"] == "META NAME"

    def test_unknown_message_type_returns_none(self):
        raw = json.dumps(
            {
                "MessageType": "SomethingElse",
                "MetaData": {"MMSI": "123"},
                "Message": {},
            }
        )
        assert parse_message(raw) == (None, None)

    def test_missing_mmsi_returns_none(self):
        raw = json.dumps(
            {
                "MessageType": "PositionReport",
                "MetaData": {"time_utc": "2024-01-15T10:00:00Z"},
                "Message": {"PositionReport": {"Latitude": 1.0, "Longitude": 2.0}},
            }
        )
        assert parse_message(raw) == (None, None)

    def test_malformed_json_returns_none(self):
        assert parse_message("{bad json}") == (None, None)
        assert parse_message(b"") == (None, None)

    def test_non_object_json_returns_none(self):
        assert parse_message("[1, 2, 3]") == (None, None)
