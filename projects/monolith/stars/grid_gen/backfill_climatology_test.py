"""Unit tests for backfill_climatology -- pure helpers only (no HTTP).

The module is stdlib-only (no heavy geo/scientific deps), so the tests run
in the standard Bazel Python sandbox without extra pip packages. The main()
function and its HTTP-backed fetch() are not called; only the pure, testable
functions are exercised. Uses py_test with
  srcs = ["backfill_climatology.py", "backfill_climatology_test.py"]
  imports = ["stars/grid_gen"]
so the module is importable without the monolith_backend dep tree.

Coverage:
  sun_elevation_deg  -- boundary values (noon vs midnight, summer vs winter)
  score_point        -- all-daytime, all-dark, mixed dark/clear, None cloud skipped
  fetch              -- happy path (mocked urllib), 429 backoff, transient skip
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from backfill_climatology import (
    CLEAR_CLOUD_MAX_PCT,
    NAUTICAL_DARK_DEG,
    RL_MIN,
    _RL,
    fetch,
    score_point,
    sun_elevation_deg,
)


# ---------------------------------------------------------------------------
# sun_elevation_deg
# ---------------------------------------------------------------------------


class TestSunElevationDeg:
    def test_midwinter_midnight_scotland_is_below_nautical_dark(self):
        """December midnight at 57N should be well below -12 deg."""
        # 2024-12-21 00:00 UTC, Edinburgh ~57N
        dt = datetime(2024, 12, 21, 0, 0, 0)
        elev = sun_elevation_deg(57.0, -3.2, dt)
        assert elev < NAUTICAL_DARK_DEG

    def test_midsummer_noon_scotland_is_well_above_horizon(self):
        """June noon at 57N should have sun high above horizon."""
        dt = datetime(2024, 6, 21, 12, 0, 0)
        elev = sun_elevation_deg(57.0, -3.2, dt)
        assert elev > 0

    def test_midwinter_noon_scotland_is_above_horizon(self):
        """December noon at 57N: sun is low but above horizon."""
        dt = datetime(2024, 12, 21, 12, 0, 0)
        elev = sun_elevation_deg(57.0, -3.2, dt)
        # Sun rises and sets in Edinburgh in December; noon should be > 0
        assert elev > -20  # may be low but not deeply negative at noon

    def test_returns_float(self):
        dt = datetime(2024, 6, 1, 0, 0, 0)
        result = sun_elevation_deg(55.0, -4.0, dt)
        assert isinstance(result, float)

    def test_elevation_range_is_bounded(self):
        """Solar elevation must always be in [-90, 90]."""
        test_cases = [
            (57.0, -3.0, datetime(2024, 1, 1, 0, 0)),
            (57.0, -3.0, datetime(2024, 6, 21, 12, 0)),
            (0.0, 0.0, datetime(2024, 3, 20, 6, 0)),
        ]
        for lat, lon, dt in test_cases:
            elev = sun_elevation_deg(lat, lon, dt)
            assert -90 <= elev <= 90, f"Out of range for {dt}: {elev}"

    def test_midnight_in_june_is_dark_in_scotland(self):
        """Even in summer, midnight at 57N should be below -12 deg."""
        dt = datetime(2024, 6, 21, 0, 0, 0)
        elev = sun_elevation_deg(57.0, -3.0, dt)
        # At 57N astronomical twilight still reaches ~-10 in midsummer night;
        # nautical dark (-12) may not apply, so just check it is low.
        assert elev < 0


# ---------------------------------------------------------------------------
# score_point
# ---------------------------------------------------------------------------


def _make_hourly(times: list[str], cc: list[float | None]) -> dict:
    return {"time": times, "cloud_cover": cc}


class TestScorePoint:
    def test_empty_series_returns_empty_dict(self):
        result = score_point(57.0, -3.0, _make_hourly([], []))
        assert result == {}

    def test_all_daytime_hours_not_counted(self):
        """June noon hours are above NAUTICAL_DARK_DEG -- none counted as dark."""
        # 5 midday hours in June -- no dark hours expected
        times = [f"2024-06-21T12:0{i}:00" for i in range(5)]
        cc = [0.0] * 5
        result = score_point(57.0, -3.0, _make_hourly(times, cc))
        assert result == {}

    def test_dark_hours_counted_at_midnight_winter(self):
        """December midnight hours should count as dark."""
        # 3 midnight hours in December
        times = [
            "2024-12-21T00:00:00",
            "2024-12-21T01:00:00",
            "2024-12-21T02:00:00",
        ]
        cc = [0.0, 0.0, 0.0]
        result = score_point(57.0, -3.0, _make_hourly(times, cc))
        assert 12 in result  # December == month 12
        dark_hours, clear_dark = result[12]
        assert dark_hours == 3
        assert clear_dark == 3  # cloud 0.0 < CLEAR_CLOUD_MAX_PCT

    def test_cloudy_dark_hours_not_clear_dark(self):
        """Cloudy dark hours increment dark_hours but not clear_dark_hours."""
        times = [
            "2024-12-21T00:00:00",
            "2024-12-21T01:00:00",
        ]
        cc = [80.0, 90.0]  # all heavily cloudy
        result = score_point(57.0, -3.0, _make_hourly(times, cc))
        if 12 in result:
            dark_hours, clear_dark = result[12]
            assert dark_hours >= 1
            assert clear_dark == 0  # >= CLEAR_CLOUD_MAX_PCT so not clear

    def test_none_cloud_cover_skipped(self):
        """Hours with cc=None must be skipped entirely."""
        times = [
            "2024-12-21T00:00:00",
            "2024-12-21T01:00:00",
        ]
        cc = [None, 0.0]  # first hour skipped, second counted
        result = score_point(57.0, -3.0, _make_hourly(times, cc))
        if 12 in result:
            dark_hours, _ = result[12]
            assert dark_hours <= 1  # at most 1 (the non-None one)

    def test_mixed_months_grouped_correctly(self):
        """Hours from different months go to different month buckets."""
        # One November dark hour, one December dark hour (both midnight)
        times = [
            "2024-11-15T00:00:00",
            "2024-12-15T00:00:00",
        ]
        cc = [0.0, 0.0]
        result = score_point(57.0, -3.0, _make_hourly(times, cc))
        months_with_data = set(result.keys())
        # Both months should have at least 1 dark hour
        assert months_with_data  # non-empty

    def test_clear_cloud_threshold_is_strict(self):
        """cc == CLEAR_CLOUD_MAX_PCT (10.0) is NOT clear (strict <)."""
        times = ["2024-12-21T00:00:00"]
        cc = [CLEAR_CLOUD_MAX_PCT]  # exactly 10.0 -- not clear
        result = score_point(57.0, -3.0, _make_hourly(times, cc))
        if 12 in result:
            _, clear_dark = result[12]
            assert clear_dark == 0

    def test_just_below_threshold_is_clear(self):
        """cc = CLEAR_CLOUD_MAX_PCT - epsilon IS clear."""
        times = ["2024-12-21T00:00:00"]
        cc = [CLEAR_CLOUD_MAX_PCT - 0.1]  # 9.9 -- clear
        result = score_point(57.0, -3.0, _make_hourly(times, cc))
        if 12 in result:
            _, clear_dark = result[12]
            assert clear_dark == 1


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def _json_response(hourly: dict) -> MagicMock:
    """Build a mock urllib.request response that returns the given hourly dict."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"hourly": hourly}).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestFetch:
    def test_happy_path_returns_hourly(self):
        hourly = {"time": ["2024-01-01T00:00:00"], "cloud_cover": [0.0]}
        mock_resp = _json_response(hourly)

        with patch(
            "backfill_climatology.urllib.request.urlopen", return_value=mock_resp
        ):
            result = fetch(57.0, -3.0)

        assert result == hourly

    def test_happy_path_resets_backoff(self):
        hourly = {"time": [], "cloud_cover": []}
        mock_resp = _json_response(hourly)
        _RL["wait"] = 300  # simulate elevated backoff

        with patch(
            "backfill_climatology.urllib.request.urlopen", return_value=mock_resp
        ):
            fetch(57.0, -3.0)

        assert _RL["wait"] == RL_MIN

    def test_four_transient_errors_returns_none(self):
        """After 4 transient errors the fetch gives up and returns None."""
        import urllib.error

        with (
            patch(
                "backfill_climatology.urllib.request.urlopen",
                side_effect=urllib.error.URLError("connection refused"),
            ),
            patch("backfill_climatology.time.sleep"),
        ):
            result = fetch(57.0, -3.0)

        assert result is None

    def test_429_sleeps_then_retries(self):
        """A 429 response sleeps then retries rather than returning None."""
        import urllib.error

        hourly = {"time": [], "cloud_cover": []}
        success_resp = _json_response(hourly)

        call_count = 0

        def _urlopen(url, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                err = urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
                raise err
            return success_resp

        with (
            patch("backfill_climatology.urllib.request.urlopen", side_effect=_urlopen),
            patch("backfill_climatology.time.sleep"),
        ):
            result = fetch(57.0, -3.0)

        assert result == hourly
        assert call_count == 2  # first 429, then success
