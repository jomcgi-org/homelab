"""Tests for generate_grid: pure geometry helpers and site grid generation.

All functions under test are pure (no I/O, no side effects), so no mocks
are needed. Fixtures use simple synthetic geometries: unit squares, triangles,
and squares with holes.
"""

import math

import pytest

from generate_grid import (
    _bbox,
    _contains,
    _in_ring,
    _polygons,
    _scotland_polygons,
    generate,
)

# ---------------------------------------------------------------------------
# _bbox
# ---------------------------------------------------------------------------


class TestBbox:
    def test_square_returns_correct_bounds(self):
        ring = [[0, 0], [2, 0], [2, 3], [0, 3]]
        assert _bbox(ring) == (0, 0, 2, 3)

    def test_single_point(self):
        ring = [[5.0, 7.0]]
        assert _bbox(ring) == (5.0, 7.0, 5.0, 7.0)

    def test_negative_coordinates(self):
        ring = [[-4.5, 56.5], [-3.0, 58.0], [-2.0, 57.0]]
        minx, miny, maxx, maxy = _bbox(ring)
        assert minx == -4.5
        assert miny == 56.5
        assert maxx == -2.0
        assert maxy == 58.0

    def test_floats_preserved(self):
        ring = [[1.1, 2.2], [3.3, 4.4]]
        assert _bbox(ring) == (1.1, 2.2, 3.3, 4.4)

    def test_scotland_like_coords(self):
        # Rough Scotland bounding box (lon, lat)
        ring = [[-6.0, 55.0], [0.0, 55.0], [0.0, 61.0], [-6.0, 61.0]]
        minx, miny, maxx, maxy = _bbox(ring)
        assert minx == -6.0
        assert miny == 55.0
        assert maxx == 0.0
        assert maxy == 61.0


# ---------------------------------------------------------------------------
# _in_ring (ray-casting point-in-polygon)
# ---------------------------------------------------------------------------

# Unit square: closed ring
_UNIT_SQUARE = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]

# Right triangle with vertices at (0,0), (4,0), (0,4)
_TRIANGLE = [[0, 0], [4, 0], [0, 4], [0, 0]]


class TestInRing:
    def test_center_of_square_is_inside(self):
        assert _in_ring(0.5, 0.5, _UNIT_SQUARE) is True

    def test_point_left_of_square_is_outside(self):
        assert _in_ring(-0.5, 0.5, _UNIT_SQUARE) is False

    def test_point_right_of_square_is_outside(self):
        assert _in_ring(1.5, 0.5, _UNIT_SQUARE) is False

    def test_point_above_square_is_outside(self):
        assert _in_ring(0.5, 1.5, _UNIT_SQUARE) is False

    def test_point_below_square_is_outside(self):
        assert _in_ring(0.5, -0.5, _UNIT_SQUARE) is False

    def test_origin_is_a_vertex_not_reliably_inside(self):
        # Vertex/edge behaviour is intentionally not asserted; the algorithm
        # does not guarantee any particular result for boundary points.
        result = _in_ring(0.0, 0.0, _UNIT_SQUARE)
        assert isinstance(result, bool)

    def test_triangle_interior_point(self):
        # (1, 1): well inside the triangle (hypotenuse is x+y=4)
        assert _in_ring(1.0, 1.0, _TRIANGLE) is True

    def test_triangle_exterior_beyond_hypotenuse(self):
        # (3, 3): 3+3=6 > 4, outside the hypotenuse
        assert _in_ring(3.0, 3.0, _TRIANGLE) is False

    def test_large_polygon_interior(self):
        big_square = [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]
        assert _in_ring(50.0, 50.0, big_square) is True

    def test_large_polygon_exterior(self):
        big_square = [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]
        assert _in_ring(150.0, 50.0, big_square) is False


# ---------------------------------------------------------------------------
# _contains (multi-polygon with holes)
# ---------------------------------------------------------------------------

# A 2x2 square polygon — no holes
_SQUARE_2x2 = [
    (
        _bbox([[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]),
        [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]],
        [],
    )
]

# A 4x4 square with a 2x2 hole in the centre
_SQUARE_WITH_HOLE = [
    (
        _bbox([[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]),
        [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
        [[[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]]],
    )
]


class TestContains:
    def test_point_inside_simple_polygon(self):
        assert _contains(_SQUARE_2x2, 1.0, 1.0) is True

    def test_point_outside_simple_polygon(self):
        assert _contains(_SQUARE_2x2, 3.0, 3.0) is False

    def test_point_far_outside_bbox(self):
        assert _contains(_SQUARE_2x2, 100.0, 100.0) is False

    def test_empty_poly_list_returns_false(self):
        assert _contains([], 1.0, 1.0) is False

    def test_point_in_outer_ring_but_inside_hole_excluded(self):
        # Centre of the square: (2, 2) is inside the hole [1,3]x[1,3]
        assert _contains(_SQUARE_WITH_HOLE, 2.0, 2.0) is False

    def test_point_in_outer_ring_outside_hole_included(self):
        # (0.5, 0.5) is inside the outer ring but outside the hole
        assert _contains(_SQUARE_WITH_HOLE, 0.5, 0.5) is True

    def test_point_outside_hole_polygon_entirely(self):
        # (5, 5) is outside the 4x4 outer ring
        assert _contains(_SQUARE_WITH_HOLE, 5.0, 5.0) is False

    def test_multiple_polygons_match_second(self):
        # Two non-overlapping squares: point in the second one
        sq1 = (
            _bbox([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]),
            [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]],
            [],
        )
        sq2 = (
            _bbox([[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]),
            [[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]],
            [],
        )
        assert _contains([sq1, sq2], 5.5, 5.5) is True

    def test_multiple_polygons_point_in_neither(self):
        sq1 = (
            _bbox([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]),
            [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]],
            [],
        )
        sq2 = (
            _bbox([[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]),
            [[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]],
            [],
        )
        assert _contains([sq1, sq2], 3.0, 3.0) is False


# ---------------------------------------------------------------------------
# _scotland_polygons
# ---------------------------------------------------------------------------


def _make_polygon_feature(geonunit, coords=None):
    """Return a minimal GeoJSON Feature with the given geonunit property."""
    if coords is None:
        coords = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
    return {
        "type": "Feature",
        "properties": {"geonunit": geonunit},
        "geometry": {"type": "Polygon", "coordinates": coords},
    }


def _make_admin1(*features):
    return {"type": "FeatureCollection", "features": list(features)}


class TestScotlandPolygons:
    def test_single_scotland_feature_returned(self):
        admin1 = _make_admin1(
            _make_polygon_feature("Scotland"),
            _make_polygon_feature("England"),
        )
        polys = _scotland_polygons(admin1)
        assert len(polys) == 1

    def test_multiple_scotland_features_all_returned(self):
        admin1 = _make_admin1(
            _make_polygon_feature("Scotland"),
            _make_polygon_feature("Scotland"),
            _make_polygon_feature("Wales"),
        )
        polys = _scotland_polygons(admin1)
        assert len(polys) == 2

    def test_no_scotland_features_returns_empty(self):
        admin1 = _make_admin1(
            _make_polygon_feature("England"),
            _make_polygon_feature("Wales"),
        )
        polys = _scotland_polygons(admin1)
        assert polys == []

    def test_missing_geonunit_property_skipped(self):
        feature_no_geonunit = {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
            },
        }
        admin1 = _make_admin1(feature_no_geonunit)
        polys = _scotland_polygons(admin1)
        assert polys == []

    def test_null_geonunit_skipped(self):
        feature = {
            "type": "Feature",
            "properties": {"geonunit": None},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
            },
        }
        admin1 = _make_admin1(feature)
        polys = _scotland_polygons(admin1)
        assert polys == []

    def test_empty_feature_collection_returns_empty(self):
        admin1 = {"type": "FeatureCollection", "features": []}
        polys = _scotland_polygons(admin1)
        assert polys == []

    def test_returned_polys_have_bbox_exterior_holes_structure(self):
        """Each poly entry is (bbox_tuple, exterior_ring, holes_list)."""
        admin1 = _make_admin1(_make_polygon_feature("Scotland"))
        polys = _scotland_polygons(admin1)
        assert len(polys) == 1
        bbox, exterior, holes = polys[0]
        assert len(bbox) == 4  # (minx, miny, maxx, maxy)
        assert isinstance(exterior, list)
        assert isinstance(holes, list)

    def test_multipolygon_scotland_feature_expanded(self):
        """A MultiPolygon Scotland feature expands to multiple polys."""
        feature = {
            "type": "Feature",
            "properties": {"geonunit": "Scotland"},
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [
                    [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                    [[[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]],
                ],
            },
        }
        admin1 = _make_admin1(feature)
        polys = _scotland_polygons(admin1)
        assert len(polys) == 2


# ---------------------------------------------------------------------------
# generate(): site grid
# ---------------------------------------------------------------------------


def _dark_regions_geojson(ring_coords):
    """Minimal dark_regions GeoJSON with one Polygon feature."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring_coords]},
                "properties": {},
            }
        ],
    }


def _scotland_polys_from_ring(ring_coords):
    """Build a scotland polys list from a single closed exterior ring."""
    geom = {"type": "Polygon", "coordinates": [ring_coords]}
    return _polygons(geom)


class TestGenerate:
    def test_no_sites_when_dark_and_scotland_do_not_overlap(self):
        """No sites are generated when the dark region and Scotland do not intersect."""
        # dark region at x in [0, 2]
        dark = _dark_regions_geojson([[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]])
        # Scotland at x in [5, 8] — disjoint
        scotland = _scotland_polys_from_ring([[5, 0], [8, 0], [8, 2], [5, 2], [5, 0]])
        sites = generate(dark, scotland, spacing_km=10.0)
        assert sites == []

    def test_sites_only_inside_intersection(self):
        """Sites are constrained to the intersection of dark region and Scotland."""
        # Large dark region covering x in [0, 10]
        dark = _dark_regions_geojson([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])
        # Scotland covers only x in [0, 5]
        scotland = _scotland_polys_from_ring([[0, 0], [5, 0], [5, 10], [0, 10], [0, 0]])
        # Coarse spacing to keep site count small
        sites = generate(dark, scotland, spacing_km=200.0)
        assert len(sites) > 0
        # All generated sites must lie within Scotland (lon < 5)
        for site in sites:
            assert site["lon"] <= 5.0, f"site lon {site['lon']} outside Scotland"

    def test_site_fields_are_complete(self):
        """Every generated site has id, name, lat, lon, altitude_m, and lp_zone."""
        dark = _dark_regions_geojson([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])
        scotland = _scotland_polys_from_ring(
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        )
        sites = generate(dark, scotland, spacing_km=500.0)
        assert len(sites) > 0
        for site in sites:
            assert "id" in site
            assert "name" in site
            assert "lat" in site
            assert "lon" in site
            assert "altitude_m" in site
            assert "lp_zone" in site

    def test_site_defaults(self):
        """Generated sites have name=None, altitude_m=0, lp_zone='dark'."""
        dark = _dark_regions_geojson([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])
        scotland = _scotland_polys_from_ring(
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        )
        sites = generate(dark, scotland, spacing_km=500.0)
        assert len(sites) > 0
        for site in sites:
            assert site["name"] is None
            assert site["altitude_m"] == 0
            assert site["lp_zone"] == "dark"

    def test_site_ids_are_sequential(self):
        """Site IDs follow the pattern scotland-NNNN with no gaps."""
        dark = _dark_regions_geojson([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])
        scotland = _scotland_polys_from_ring(
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        )
        sites = generate(dark, scotland, spacing_km=500.0)
        assert len(sites) > 0
        for i, site in enumerate(sites):
            assert site["id"] == f"scotland-{i:04d}", f"unexpected id at index {i}"

    def test_lat_lon_rounded_to_4dp(self):
        """lat and lon are rounded to 4 decimal places."""
        dark = _dark_regions_geojson([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])
        scotland = _scotland_polys_from_ring(
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        )
        sites = generate(dark, scotland, spacing_km=500.0)
        for site in sites:
            assert site["lat"] == round(site["lat"], 4)
            assert site["lon"] == round(site["lon"], 4)

    def test_finer_spacing_produces_more_sites(self):
        """Halving the spacing (approximately) produces more sites."""
        dark = _dark_regions_geojson([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])
        scotland = _scotland_polys_from_ring(
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        )
        coarse = generate(dark, scotland, spacing_km=300.0)
        fine = generate(dark, scotland, spacing_km=150.0)
        assert len(fine) > len(coarse)
