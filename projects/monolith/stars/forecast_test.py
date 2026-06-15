"""Unit tests for stars.forecast.score_location (stars v2).

elevation() is monkeypatched per entry so the twilight/daylight gate is fully
deterministic and independent of the real ephemeris. score_location keeps every
hour below the -10 deg twilight floor (the summer fallback), tags each with
is_clear (cloud < 10%) and dark (sun < -12, true nautical dark); only hours
brighter than -10 are dropped. A kept-but-cloudy hour is retained (is_clear
False) so the read path can still count it toward the denominators.
"""

import stars.forecast as forecast

# A grid-sourced site dict (the shape stars.jobs._load_sites passes to fetch_all).
_LOC: dict = {
    "id": "test-site",
    "lat": 57.0,
    "lon": -4.0,
    "altitude_m": 100,
}

# Sun elevation (degrees) keyed by the UTC hour of the timeseries entry.
# Hours 22, 23, 0 are deep dark (below -12); hour 12 is daytime.
_ELEV_BY_HOUR = {22: -20.0, 23: -20.0, 0: -20.0, 12: 30.0}


def _fake_elevation(observer, t):
    return _ELEV_BY_HOUR[t.hour]


def _instant(cloud, temp, dew):
    return {
        "data": {
            "instant": {
                "details": {
                    "cloud_area_fraction": cloud,
                    "air_temperature": temp,
                    "dew_point_temperature": dew,
                }
            },
            "next_1_hours": {"summary": {"symbol_code": "clearsky_night"}},
        }
    }


def _forecast():
    # Deliberately out of time order so the ascending sort is exercised.
    return {
        "properties": {
            "timeseries": [
                # deep-dark + clear -> qualifies, is_clear True
                {"time": "2026-06-13T23:00:00Z", **_instant(5, 8, 2)},
                # deep-dark but fully clouded -> kept, is_clear False
                {"time": "2026-06-14T00:00:00Z", **_instant(100, 8, 7)},
                # daytime (sun up, not dark) -> dropped before scoring
                {"time": "2026-06-13T12:00:00Z", **_instant(0, 12, 2)},
                # deep-dark + clear, earlier hour -> qualifies, must sort first
                {"time": "2026-06-13T22:00:00Z", **_instant(5, 8, 2)},
            ]
        }
    }


_EXPECTED_KEYS = {
    "time",
    "sun_elevation_deg",
    "cloud_area_fraction",
    "air_temperature",
    "dew_spread",
    "symbol",
    "is_clear",
    "dark",
}


def test_score_location_keeps_all_dark_hours_sorted(monkeypatch):
    monkeypatch.setattr(forecast, "elevation", _fake_elevation)

    hours = forecast.score_location(_LOC, _forecast())

    # All three dark hours are kept (clear and cloudy alike); only the daytime
    # hour is dropped. Sorted ascending by time.
    assert [h["time"] for h in hours] == [
        "2026-06-13T22:00:00Z",
        "2026-06-13T23:00:00Z",
        "2026-06-14T00:00:00Z",
    ]
    for h in hours:
        assert set(h.keys()) == _EXPECTED_KEYS

    first = hours[0]
    assert first["sun_elevation_deg"] == -20.0
    assert first["cloud_area_fraction"] == 5
    assert first["air_temperature"] == 8
    assert first["dew_spread"] == 6.0
    assert first["symbol"] == "clearsky_night"
    # 5% cloud under the 10% threshold -> clear.
    assert first["is_clear"] is True

    # Deep-dark hours (sun -20) are flagged dark.
    assert first["dark"] is True

    # The fully-clouded dark hour is retained but flagged not-clear.
    cloudy = hours[2]
    assert cloudy["time"] == "2026-06-14T00:00:00Z"
    assert cloudy["cloud_area_fraction"] == 100
    assert cloudy["is_clear"] is False
    assert cloudy["dark"] is True


def test_score_location_keeps_twilight_drops_below_floor(monkeypatch):
    # The summer fallback: with no true dark, hours between the -10 floor and the
    # -12 dark threshold are kept (dark=False); a true-dark hour is dark=True; an
    # hour shallower than -10 is dropped entirely.
    elev_by_hour = {
        21: -9.0,  # shallower than the -10 floor -> dropped
        22: -11.0,  # twilight: kept, dark=False
        23: -13.0,  # true dark: kept, dark=True
    }
    monkeypatch.setattr(forecast, "elevation", lambda observer, t: elev_by_hour[t.hour])
    fc = {
        "properties": {
            "timeseries": [
                {"time": "2026-06-21T21:00:00Z", **_instant(5, 8, 2)},
                {"time": "2026-06-21T22:00:00Z", **_instant(5, 8, 2)},
                {"time": "2026-06-21T23:00:00Z", **_instant(5, 8, 2)},
            ]
        }
    }
    hours = forecast.score_location(_LOC, fc)

    # The -9 hour is gone; the twilight and dark hours survive, sorted ascending.
    assert [h["time"] for h in hours] == [
        "2026-06-21T22:00:00Z",
        "2026-06-21T23:00:00Z",
    ]
    twilight, dark = hours
    assert twilight["sun_elevation_deg"] == -11.0
    assert twilight["dark"] is False
    # Still clear (cloud 5%), just not true dark.
    assert twilight["is_clear"] is True
    assert dark["sun_elevation_deg"] == -13.0
    assert dark["dark"] is True


def test_score_location_cloud_boundary_is_not_clear(monkeypatch):
    monkeypatch.setattr(forecast, "elevation", _fake_elevation)
    fc = {
        "properties": {
            "timeseries": [
                {"time": "2026-06-13T22:00:00Z", **_instant(10.0, 8, 2)},
            ]
        }
    }
    hours = forecast.score_location(_LOC, fc)
    assert len(hours) == 1
    # Cloud exactly at the 10% threshold is not clear (strictly-below contract).
    assert hours[0]["is_clear"] is False


def test_score_location_missing_cloud_defaults_to_overcast(monkeypatch):
    monkeypatch.setattr(forecast, "elevation", _fake_elevation)
    fc = {
        "properties": {
            "timeseries": [
                {
                    "time": "2026-06-13T22:00:00Z",
                    "data": {
                        "instant": {"details": {"air_temperature": 8}},
                        "next_1_hours": {},
                    },
                },
            ]
        }
    }
    hours = forecast.score_location(_LOC, fc)
    assert len(hours) == 1
    assert hours[0]["cloud_area_fraction"] == 100
    assert hours[0]["is_clear"] is False


def test_score_location_empty_forecast_returns_empty(monkeypatch):
    monkeypatch.setattr(forecast, "elevation", _fake_elevation)
    assert forecast.score_location(_LOC, {}) == []


def test_score_location_garbage_forecast_returns_empty(monkeypatch):
    monkeypatch.setattr(forecast, "elevation", _fake_elevation)
    garbage = {"properties": {"timeseries": [{"time": "not-a-timestamp"}]}}
    assert forecast.score_location(_LOC, garbage) == []
