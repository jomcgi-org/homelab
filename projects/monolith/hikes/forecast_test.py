"""Unit tests for the pure met.no forecast logic (no network).

compute_windows ports the exact viability ladder from the legacy
update_forecast/update.py, so these tests pin the threshold edges
(precip 2.0 mm, wind 80 km/h), the 07:00-19:00 daylight gate, the
past/horizon drops, the None defaults, and the emitted tuple rounding.
"""

from datetime import datetime, timezone

from hikes.forecast import compute_windows, parse_hourly

NOW = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
LAT = 56.7969
LON = -5.0036


def _entry(
    time_str: str,
    temp_c: float | None = 10.0,
    wind_ms: float | None = 5.0,
    precip: float = 0.0,
    cloud: float | None = 20.0,
) -> dict:
    return {
        "time": time_str,
        "temp_c": temp_c,
        "wind_speed_ms": wind_ms,
        "precipitation_mm": precip,
        "cloud_area_fraction": cloud,
    }


def _windows(entry: dict) -> list[list]:
    return compute_windows([entry], NOW, LAT, LON)


# A safely in-range slot: next day, midday, within the 7-day horizon.
VIABLE_TIME = "2026-06-14T12:00:00Z"


class TestViabilityThresholds:
    def test_precip_exactly_2_0_mm_is_viable(self):
        assert len(_windows(_entry(VIABLE_TIME, precip=2.0))) == 1

    def test_precip_above_2_0_mm_is_not_viable(self):
        assert _windows(_entry(VIABLE_TIME, precip=2.1)) == []

    def test_wind_just_below_80_kmh_is_viable(self):
        # 22.2 m/s * 3.6 = 79.92 km/h, under the threshold.
        windows = _windows(_entry(VIABLE_TIME, wind_ms=22.2))
        assert len(windows) == 1
        assert windows[0][3] == 80  # round(79.92)

    def test_wind_above_80_kmh_is_not_viable(self):
        # 22.3 m/s * 3.6 = 80.28 km/h, over the threshold.
        assert _windows(_entry(VIABLE_TIME, wind_ms=22.3)) == []

    def test_none_wind_treated_as_zero(self):
        windows = _windows(_entry(VIABLE_TIME, wind_ms=None))
        assert len(windows) == 1
        assert windows[0][3] == 0


class TestDaylightGate:
    def test_hour_6_is_not_viable(self):
        assert _windows(_entry("2026-06-14T06:00:00Z")) == []

    def test_hour_7_is_viable(self):
        assert len(_windows(_entry("2026-06-14T07:00:00Z"))) == 1

    def test_hour_19_is_viable(self):
        assert len(_windows(_entry("2026-06-14T19:00:00Z"))) == 1

    def test_hour_20_is_not_viable(self):
        assert _windows(_entry("2026-06-14T20:00:00Z")) == []


class TestTimeBounds:
    def test_past_hour_is_dropped(self):
        assert _windows(_entry("2026-06-13T10:00:00Z")) == []

    def test_hour_at_now_is_kept(self):
        assert len(_windows(_entry("2026-06-13T12:00:00Z"))) == 1

    def test_hour_at_exact_horizon_is_kept(self):
        # now + 7 days exactly; the ladder only drops dt > horizon.
        assert len(_windows(_entry("2026-06-20T12:00:00Z"))) == 1

    def test_hour_beyond_7_days_is_dropped(self):
        assert _windows(_entry("2026-06-20T13:00:00Z")) == []


class TestTupleEmission:
    def test_none_temp_defaults_to_zero(self):
        windows = _windows(_entry(VIABLE_TIME, temp_c=None))
        assert windows[0][1] == 0

    def test_none_cloud_defaults_to_50(self):
        windows = _windows(_entry(VIABLE_TIME, cloud=None))
        assert windows[0][4] == 50

    def test_zero_precip_emitted_as_int_zero(self):
        windows = _windows(_entry(VIABLE_TIME, precip=0.0))
        assert windows[0][2] == 0

    def test_rounding_of_emitted_tuple(self):
        windows = _windows(
            _entry(VIABLE_TIME, temp_c=10.34, wind_ms=4.7, precip=1.44, cloud=87.6)
        )
        expected_ts = int(datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc).timestamp())
        assert windows == [[expected_ts, 10.3, 1.4, 17, 88]]


class TestParseHourly:
    def test_skips_entries_without_next_1_hours(self):
        forecast = {
            "properties": {
                "timeseries": [
                    {
                        "time": "2026-06-14T12:00:00Z",
                        "data": {
                            "instant": {
                                "details": {
                                    "air_temperature": 11.2,
                                    "wind_speed": 3.4,
                                    "cloud_area_fraction": 75.0,
                                }
                            },
                            "next_1_hours": {"details": {"precipitation_amount": 0.3}},
                        },
                    },
                    {
                        # 6-hourly tail entry: no next_1_hours, must be skipped.
                        "time": "2026-06-20T12:00:00Z",
                        "data": {
                            "instant": {"details": {"air_temperature": 9.0}},
                            "next_6_hours": {"details": {"precipitation_amount": 1.0}},
                        },
                    },
                ]
            }
        }
        hourly = parse_hourly(forecast)
        assert hourly == [
            {
                "time": "2026-06-14T12:00:00Z",
                "temp_c": 11.2,
                "wind_speed_ms": 3.4,
                "precipitation_mm": 0.3,
                "cloud_area_fraction": 75.0,
            }
        ]

    def test_missing_precipitation_amount_defaults_to_zero(self):
        forecast = {
            "properties": {
                "timeseries": [
                    {
                        "time": "2026-06-14T12:00:00Z",
                        "data": {
                            "instant": {"details": {}},
                            "next_1_hours": {"summary": {"symbol_code": "fair_day"}},
                        },
                    }
                ]
            }
        }
        assert parse_hourly(forecast)[0]["precipitation_mm"] == 0

    def test_none_and_missing_properties_return_empty(self):
        assert parse_hourly(None) == []
        assert parse_hourly({}) == []
        assert parse_hourly({"type": "Feature"}) == []
