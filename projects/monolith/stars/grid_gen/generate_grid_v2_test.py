"""Tests for generate_grid_v2: pure helpers only (no big-file I/O).

The road filter and raster sampling need geopandas/rasterio and the large PVC
files, so they are not exercised here. We test the pure, deterministic helpers:
classify_zone (nearest swatch), is_dark_zone (keep set), and generate_mesh
(Scotland-land point generator). Fixtures use synthetic geometries.
"""

import pytest

from generate_grid_v2 import (
    DARK_ZONES,
    SWATCHES,
    _polygons,
    classify_zone,
    generate_mesh,
    is_dark_zone,
)

# ---------------------------------------------------------------------------
# classify_zone (nearest-swatch, int-safe distance)
# ---------------------------------------------------------------------------


class TestClassifyZone:
    @pytest.mark.parametrize(
        "rgb,expected",
        [
            ((0, 0, 0), "pristine"),
            ((66, 66, 66), "excellent"),
            ((33, 84, 216), "rural"),
            ((31, 161, 42), "green"),
            ((184, 166, 37), "yellow"),
            ((253, 150, 80), "orange"),
            ((251, 153, 138), "pink"),
            ((242, 242, 242), "white"),
        ],
    )
    def test_exact_swatch_classifies_to_itself(self, rgb, expected):
        assert classify_zone(rgb) == expected

    def test_near_black_is_pristine(self):
        assert classify_zone((5, 4, 6)) == "pristine"

    def test_near_gray_is_excellent(self):
        assert classify_zone((70, 60, 64)) == "excellent"

    def test_near_white_is_white(self):
        assert classify_zone((240, 238, 245)) == "white"

    def test_bright_blue_pixel_is_rural(self):
        # A blue-dominant pixel sits nearest the blue swatch.
        assert classify_zone((40, 90, 210)) == "rural"

    def test_distance_does_not_overflow(self):
        # Pure white vs pure black: every channel delta is 255. A uint8 squared
        # implementation would wrap; the int path must pick the nearest cleanly.
        assert classify_zone((255, 255, 255)) == "white"
        assert classify_zone((1, 1, 1)) == "pristine"

    def test_accepts_any_indexable_rgb(self):
        # Lists (e.g. a numpy row converted via tolist) work too.
        assert classify_zone([0, 0, 0]) == "pristine"

    def test_returns_a_known_zone_name(self):
        names = {name for name, _ in SWATCHES}
        assert classify_zone((120, 120, 120)) in names


# ---------------------------------------------------------------------------
# is_dark_zone (the keep set)
# ---------------------------------------------------------------------------


class TestIsDarkZone:
    @pytest.mark.parametrize("zone", ["pristine", "excellent", "rural"])
    def test_dark_zones_kept(self, zone):
        assert is_dark_zone(zone) is True

    @pytest.mark.parametrize("zone", ["green", "yellow", "orange", "pink", "white"])
    def test_bright_zones_dropped(self, zone):
        assert is_dark_zone(zone) is False

    def test_unknown_zone_dropped(self):
        assert is_dark_zone("nonsense") is False

    def test_dark_zones_constant_matches_swatch_prefix(self):
        # The three darkest swatches are exactly the dark keep set.
        assert {name for name, _ in SWATCHES[:3]} == set(DARK_ZONES)


# ---------------------------------------------------------------------------
# generate_mesh (Scotland-land point generator)
# ---------------------------------------------------------------------------


def _polys_from_ring(ring_coords):
    return _polygons({"type": "Polygon", "coordinates": [ring_coords]})


# A 1-degree square near Scotland latitudes (lon, lat).
_SQUARE = [[-4.0, 56.0], [-3.0, 56.0], [-3.0, 57.0], [-4.0, 57.0], [-4.0, 56.0]]


class TestGenerateMesh:
    def test_points_are_inside_the_polygon(self):
        scotland = _polys_from_ring(_SQUARE)
        points = generate_mesh(scotland, spacing_km=20.0)
        assert len(points) > 0
        for lon, lat in points:
            assert -4.0 <= lon <= -3.0
            assert 56.0 <= lat <= 57.0

    def test_returns_lon_lat_tuples(self):
        scotland = _polys_from_ring(_SQUARE)
        points = generate_mesh(scotland, spacing_km=20.0)
        for point in points:
            assert len(point) == 2

    def test_finer_spacing_produces_more_points(self):
        scotland = _polys_from_ring(_SQUARE)
        coarse = generate_mesh(scotland, spacing_km=40.0)
        fine = generate_mesh(scotland, spacing_km=15.0)
        assert len(fine) > len(coarse)

    def test_disjoint_polygon_excludes_outside_points(self):
        # Two squares far apart: every generated point must fall in one of them.
        ring_a = [[0.0, 56.0], [0.5, 56.0], [0.5, 56.5], [0.0, 56.5], [0.0, 56.0]]
        ring_b = [[5.0, 56.0], [5.5, 56.0], [5.5, 56.5], [5.0, 56.5], [5.0, 56.0]]
        scotland = _polys_from_ring(ring_a) + _polys_from_ring(ring_b)
        points = generate_mesh(scotland, spacing_km=10.0)
        assert len(points) > 0
        for lon, lat in points:
            in_a = 0.0 <= lon <= 0.5 and 56.0 <= lat <= 56.5
            in_b = 5.0 <= lon <= 5.5 and 56.0 <= lat <= 56.5
            assert in_a or in_b, f"point ({lon}, {lat}) outside both squares"

    def test_tiny_polygon_stays_within_its_bounds(self):
        # A sub-kilometre ring sampled at coarse spacing produces at most a
        # handful of points, all confined to the ring's tiny bbox.
        ring = [[0.0, 56.0], [0.01, 56.0], [0.01, 56.01], [0.0, 56.01], [0.0, 56.0]]
        scotland = _polys_from_ring(ring)
        points = generate_mesh(scotland, spacing_km=50.0)
        for lon, lat in points:
            assert 0.0 <= lon <= 0.01
            assert 56.0 <= lat <= 56.01
