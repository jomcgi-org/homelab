"""Unit tests for backfill_cerra -- pure helpers only (no xarray/cfgrib/scipy I/O).

backfill_cerra.py imports xarray and numpy at module level, and scipy inside
main(). xarray and cfgrib are heavy geo packages not present in the Bazel pip
sandbox, so we inject mock stubs into sys.modules before importing the module.
numpy IS available in the sandbox (it is a runtime dep), so _to_xyz and
sun_elevation_deg (the two pure-numpy helpers) can be tested against real
arithmetic.

load_cube and main() are not tested here: they require live GRIB file I/O and
the full scipy/xarray stack. This test file mirrors the generate_grid_v2_test.py
approach of "test the pure helpers; annotate the I/O-heavy parts as excluded".

Coverage:
  _to_xyz             -- scalar lat/lon and array lat/lon, unit-sphere constraint
  sun_elevation_deg   -- dark-hour threshold, bounded output, correct sign
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Mock xarray before importing backfill_cerra
# ---------------------------------------------------------------------------
_xarray_stub = MagicMock(spec=ModuleType("xarray"))
sys.modules.setdefault("xarray", _xarray_stub)

# cfgrib is only used inside load_cube via xarray's engine kwarg, but importing
# xarray itself (even mocked) can trigger cfgrib probing on some platforms.
sys.modules.setdefault("cfgrib", MagicMock(spec=ModuleType("cfgrib")))

import backfill_cerra  # noqa: E402 (must be after sys.modules stubs)

from backfill_cerra import (  # noqa: E402
    CLEAR_CLOUD_MAX_PCT,
    LAT_MAX,
    LAT_MIN,
    LON_MAX,
    LON_MIN,
    NAUTICAL_DARK_DEG,
    _to_xyz,
    sun_elevation_deg,
)


# ---------------------------------------------------------------------------
# _to_xyz
# ---------------------------------------------------------------------------


class TestToXyz:
    def test_north_pole_points_up(self):
        """Lat=90, lon=0 should give (0, 0, 1) on the unit sphere."""
        xyz = _to_xyz(np.array([90.0]), np.array([0.0]))
        np.testing.assert_allclose(xyz[0], [0.0, 0.0, 1.0], atol=1e-6)

    def test_equator_prime_meridian(self):
        """Lat=0, lon=0 should give (1, 0, 0)."""
        xyz = _to_xyz(np.array([0.0]), np.array([0.0]))
        np.testing.assert_allclose(xyz[0], [1.0, 0.0, 0.0], atol=1e-6)

    def test_equator_lon_90(self):
        """Lat=0, lon=90 should give (0, 1, 0)."""
        xyz = _to_xyz(np.array([0.0]), np.array([90.0]))
        np.testing.assert_allclose(xyz[0], [0.0, 1.0, 0.0], atol=1e-6)

    def test_output_is_unit_sphere(self):
        """Every output vector should have magnitude 1."""
        lats = np.array([57.0, 30.0, -45.0, 0.0])
        lons = np.array([-3.0, 45.0, 120.0, 0.0])
        xyz = _to_xyz(lats, lons)
        norms = np.linalg.norm(xyz, axis=-1)
        np.testing.assert_allclose(norms, np.ones_like(norms), atol=1e-6)

    def test_output_shape_matches_input(self):
        n = 5
        lats = np.linspace(55.0, 61.0, n)
        lons = np.linspace(-8.0, -1.0, n)
        xyz = _to_xyz(lats, lons)
        assert xyz.shape == (n, 3)

    def test_2d_grid_input(self):
        """_to_xyz should also handle 2-D lat/lon grids (stacked grid cells)."""
        lats = np.array([[57.0, 58.0], [59.0, 60.0]])
        lons = np.array([[-3.0, -2.0], [-4.0, -1.0]])
        xyz = _to_xyz(lats, lons)
        norms = np.linalg.norm(xyz, axis=-1)
        np.testing.assert_allclose(norms, np.ones_like(norms), atol=1e-6)


# ---------------------------------------------------------------------------
# sun_elevation_deg  (vectorized numpy version)
# ---------------------------------------------------------------------------


class TestSunElevationDeg:
    def test_returns_array_of_same_length_as_times(self):
        times = np.array(
            ["2024-12-21T00:00:00", "2024-12-21T01:00:00"],
            dtype="datetime64[s]",
        )
        result = sun_elevation_deg(57.0, -3.0, times)
        assert result.shape == (2,)

    def test_midwinter_midnight_scotland_below_nautical_dark(self):
        """December midnight at 57N should be below the -12 deg dark threshold."""
        times = np.array(["2024-12-21T00:00:00"], dtype="datetime64[s]")
        elev = sun_elevation_deg(57.0, -3.0, times)
        assert elev[0] < NAUTICAL_DARK_DEG

    def test_midsummer_noon_scotland_above_horizon(self):
        """June noon at 57N should have the sun above the horizon."""
        times = np.array(["2024-06-21T12:00:00"], dtype="datetime64[s]")
        elev = sun_elevation_deg(57.0, -3.0, times)
        assert elev[0] > 0

    def test_elevations_bounded_minus90_to_90(self):
        """All output elevations must be in [-90, 90]."""
        times = np.array(
            [
                "2024-01-15T00:00:00",
                "2024-04-15T06:00:00",
                "2024-06-21T12:00:00",
                "2024-09-23T18:00:00",
            ],
            dtype="datetime64[s]",
        )
        elev = sun_elevation_deg(57.0, -3.0, times)
        assert np.all(elev >= -90)
        assert np.all(elev <= 90)

    def test_midnight_values_are_negative(self):
        """At midnight UTC in winter, the sun is definitely below the horizon."""
        times = np.array(
            ["2024-11-01T00:00:00", "2024-12-15T01:00:00"],
            dtype="datetime64[s]",
        )
        elev = sun_elevation_deg(57.0, -3.0, times)
        assert np.all(elev < 0)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_scotland_bbox_values():
    """Sanity-check the hardcoded bbox constants cover mainland Scotland."""
    assert LAT_MIN < 55.0  # south of mainland
    assert LAT_MAX > 60.0  # north enough for Orkney
    assert LON_MIN < -5.0  # west coast
    assert LON_MAX > -1.0  # east coast


def test_thresholds_match_scoring_py():
    """KEEP IN SYNC with scoring.py: dark < -12, clear-dark < 10."""
    assert NAUTICAL_DARK_DEG == -12.0
    assert CLEAR_CLOUD_MAX_PCT == 10.0
