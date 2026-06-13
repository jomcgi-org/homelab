"""Unit tests for the pure met.no forecast logic (no network).

compute_windows ports the viability ladder from the legacy
update_forecast/update.py, so these tests pin the threshold edges
(precip 2.0 mm, wind 80 km/h), the past/horizon drops, the None defaults,
and the emitted tuple rounding. The daylight gate is the one intentional
divergence: it now follows real sunrise/sunset at the walk's coordinates
(see TestDaylightGate and TestSunTimes) rather than a fixed 07:00-19:00 band.
"""

from datetime import datetime, timedelta, timezone

from hikes.forecast import compute_windows, is_daylight, parse_hourly, sun_times

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
    # At the test coordinates (Ben Nevis area, 56.8 N) on 2026-06-14 the sun is
    # up roughly 03:28-21:14 UTC, far wider than the legacy 07:00-19:00 band.
    # The gate must admit the early-morning and late-evening summer hours the
    # old fixed band wrongly dropped, and still reject true darkness.
    def test_before_sunrise_is_not_viable(self):
        # 03:00 UTC, a few minutes before the ~03:28 sunrise.
        assert _windows(_entry("2026-06-14T03:00:00Z")) == []

    def test_early_summer_morning_is_viable(self):
        # 04:00 UTC (05:00 BST) is daylight in June but dark under the old band.
        assert len(_windows(_entry("2026-06-14T04:00:00Z"))) == 1

    def test_late_summer_evening_is_viable(self):
        # 21:00 UTC (22:00 BST), just before the ~21:14 sunset.
        assert len(_windows(_entry("2026-06-14T21:00:00Z"))) == 1

    def test_after_sunset_is_not_viable(self):
        # 22:00 UTC, after the sun has set.
        assert _windows(_entry("2026-06-14T22:00:00Z")) == []


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


class TestSunTimes:
    """The NOAA sunrise equation that backs the daylight gate. Tolerances are a
    few minutes (the model is ~1 min accurate); the point is to catch a
    wrong-direction band, a UTC/local mix-up, or a winter/summer swap, not to
    verify the almanac to the second.
    """

    def _between(self, dt, start, end):
        lo = datetime.fromisoformat(f"2026-{start}Z")
        hi = datetime.fromisoformat(f"2026-{end}Z")
        return lo <= dt <= hi

    def test_summer_solstice_window_is_long(self):
        # Ben Nevis area near the solstice: sunrise well before the old 07:00
        # gate, sunset well after 19:00.
        sunrise, sunset = sun_times(NOW.replace(month=6, day=14), LAT, LON)
        assert self._between(sunrise, "06-14T03:00:00", "06-14T04:00:00")
        assert self._between(sunset, "06-14T21:00:00", "06-14T21:30:00")
        assert sunset - sunrise > timedelta(hours=17)

    def test_winter_solstice_window_is_short(self):
        # Same place at midwinter: a ~7 h band shifted later in the UTC day,
        # the opposite of a fixed 07:00-19:00 assumption.
        sunrise, sunset = sun_times(NOW.replace(month=12, day=21), LAT, LON)
        assert self._between(sunrise, "12-21T08:30:00", "12-21T09:15:00")
        assert self._between(sunset, "12-21T15:30:00", "12-21T16:00:00")
        assert sunset - sunrise < timedelta(hours=8)

    def test_polar_night_has_no_daylight(self):
        # Svalbard at the winter solstice: the sun never rises.
        midwinter = datetime(2026, 12, 21, 12, tzinfo=timezone.utc)
        assert sun_times(midwinter, 78.0, 16.0) is None
        assert is_daylight(midwinter, 78.0, 16.0) is False

    def test_polar_day_is_all_daylight(self):
        # Svalbard at the summer solstice: the sun never sets, so even 02:00
        # UTC reads as daylight.
        midnight_sun = datetime(2026, 6, 21, 2, tzinfo=timezone.utc)
        assert is_daylight(midnight_sun, 78.0, 16.0) is True
