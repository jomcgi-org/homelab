"""Unit tests for stars.forecast.score_location.

elevation() is monkeypatched per entry so the dark/daylight gate is fully
deterministic and independent of the real ephemeris. ADR 007: score_location
keeps every civil-dark hour ranked by the continuous quality Q = D x C x W and
drops daytime hours and dark-but-hopeless (Q == 0) hours. The qualifying-hour
shape, recorded sun elevation, and ascending sort are asserted directly.
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
# Hours 22, 23, 0 are deep dark (below astronomical -18); hour 12 is daytime.
_ELEV_BY_HOUR = {22: -20.0, 23: -20.0, 0: -20.0, 12: 30.0}


def _fake_elevation(observer, t):
    return _ELEV_BY_HOUR[t.hour]


def _instant(cloud, humidity, wind, temp, dew, pressure=1013.25, fog=0):
    return {
        "data": {
            "instant": {
                "details": {
                    "cloud_area_fraction": cloud,
                    "relative_humidity": humidity,
                    "fog_area_fraction": fog,
                    "wind_speed": wind,
                    "air_temperature": temp,
                    "dew_point_temperature": dew,
                    "air_pressure_at_sea_level": pressure,
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
                # deep-dark + clear -> qualifies (Q == 100)
                {"time": "2026-06-13T23:00:00Z", **_instant(5, 55, 2, 8, 2)},
                # dark but fully clouded -> Q == 0, dropped
                {"time": "2026-06-14T00:00:00Z", **_instant(100, 90, 2, 8, 7)},
                # daytime (sun up, not dark) -> dropped before scoring
                {"time": "2026-06-13T12:00:00Z", **_instant(0, 40, 1, 12, 2)},
                # deep-dark + clear, earlier hour -> qualifies, must sort first
                {"time": "2026-06-13T22:00:00Z", **_instant(5, 55, 2, 8, 2)},
            ]
        }
    }


_EXPECTED_KEYS = {
    "time",
    "score",
    "sun_elevation_deg",
    "darkness_factor",
    "cloud_factor",
    "cloud_area_fraction",
    "relative_humidity",
    "wind_speed",
    "air_temperature",
    "dew_spread",
    "symbol",
}


def test_score_location_keeps_only_qualifying_hours_sorted(monkeypatch):
    monkeypatch.setattr(forecast, "elevation", _fake_elevation)

    hours = forecast.score_location(_LOC, _forecast())

    # Two deep-dark clear hours qualify; the fully-clouded (Q == 0) and daytime
    # hours are dropped.
    assert [h["time"] for h in hours] == [
        "2026-06-13T22:00:00Z",
        "2026-06-13T23:00:00Z",
    ]
    for h in hours:
        assert set(h.keys()) == _EXPECTED_KEYS
    first = hours[0]
    # Deep dark (-20 -> darkness 1.0) + 5% cloud (under the 10% allowance) +
    # ideal weather -> full quality.
    assert first["score"] == 100.0
    assert first["sun_elevation_deg"] == -20.0
    # Deep dark -> darkness factor 1.0; 5% cloud under the darkness-scaled
    # allowance -> clarity 1.0. These are the components the prune banks.
    assert first["darkness_factor"] == 1.0
    assert first["cloud_factor"] == 1.0
    assert first["cloud_area_fraction"] == 5
    assert first["relative_humidity"] == 55
    assert first["wind_speed"] == 2
    assert first["air_temperature"] == 8
    assert first["dew_spread"] == 6.0
    assert first["symbol"] == "clearsky_night"


def test_score_location_empty_forecast_returns_empty(monkeypatch):
    monkeypatch.setattr(forecast, "elevation", _fake_elevation)
    assert forecast.score_location(_LOC, {}) == []


def test_score_location_garbage_forecast_returns_empty(monkeypatch):
    monkeypatch.setattr(forecast, "elevation", _fake_elevation)
    garbage = {"properties": {"timeseries": [{"time": "not-a-timestamp"}]}}
    assert forecast.score_location(_LOC, garbage) == []
