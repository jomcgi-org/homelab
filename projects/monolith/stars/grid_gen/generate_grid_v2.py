"""Offline grid-v2 generator: Scotland land mesh -> road-accessible -> dark.

This is NOT runtime code (grid_gen is excluded from the monolith image in BUILD).
It is run by hand on the workstation, where heavy geospatial deps (rasterio/GDAL,
geopandas, shapely 2) are available. Pipeline:

  1. Mesh Scotland land at 2 km spacing. The Natural Earth admin-1 council-area
     polygons ARE land boundaries, so a point-in-Scotland test already excludes
     sea (pure-Python ray casting, copied from generate_grid.py).
  2. ROAD filter: keep mesh points within 2 km of an OSM road. Roads + points are
     reprojected to EPSG:27700 (metres) and tested with shapely 2
     STRtree.query(points, predicate="dwithin", distance=2000). Non-drivable
     fclass values (path/footway/steps/cycleway/bridleway) are dropped so "road"
     means drivable.
  3. DARK filter: sample the local 3-band RGB LP render at each kept point and
     classify the pixel by NEAREST match to the authoritative 8-stop legend
     (classify_zone). Keep only the darkest zones (pristine/excellent/rural =
     black/gray/blue). Validated: every Scottish Dark Sky Park reads
     black/gray/blue, every city reads orange/pink/white. The color_palette.json
     on the PVC is a MISMATCHED legend, ignore it; the swatches below are
     authoritative.

Output: grid.json, an array of
  {id: "scotland-NNNN", name: null, lat, lon, altitude_m: 0, lp_zone}
where lp_zone is the classified zone name (pristine/excellent/rural). ids are
assigned after a stable sort by (lat, lon).

Inputs (the stargazer service was decommissioned 2026-06; its processed data was
archived to SeaweedFS at s3://stargazer-archive/processed/ before removal):

    # Roads (~193 MB) and the LP raster. Pull from the SeaweedFS archive. Set $EP
    # to the in-cluster SeaweedFS S3 endpoint (the seaweedfsS3Endpoint value in the
    # monolith chart; creds default to duckdb/duckdb, see chat/store.py).
    aws --endpoint-url "$EP" s3 cp s3://stargazer-archive/processed/scotland-roads.geojson /tmp/scotland-roads.geojson
    aws --endpoint-url "$EP" s3 cp s3://stargazer-archive/processed/scotland_lp_2024.tif /tmp/scotland_lp_2024.tif

    # Natural Earth 10m admin-1 (for the Scotland boundary); features with
    # properties.geonunit == "Scotland" are the Scottish council areas.
    curl -o /tmp/admin1.geojson https://raw.githubusercontent.com/nvkelso/\
natural-earth-vector/master/geojson/ne_10m_admin_1_states_provinces.geojson

Run:

    python3 generate_grid_v2.py \
        --admin1 /tmp/admin1.geojson \
        --roads /tmp/scotland-roads.geojson \
        --raster /tmp/scotland_lp_2024.tif \
        --spacing-km 2 --output grid.json

Expected output: a grid.json of road-accessible dark sites (order ~150-400
genuinely-dark accessible points over the Highlands; cities are absent because
their pixels classify as orange/pink/white). Upload it the same way as v1:

    kubectl port-forward -n seaweedfs svc/seaweedfs-s3 8333:8333 &
    curl -X PUT -T grid.json http://localhost:8333/stars/grid.json
    # stars.load_grid ingests it into stars.sites on its next run (or trigger it).
"""

from __future__ import annotations

import argparse
import json
import math

# ---------------------------------------------------------------------------
# Authoritative LP legend (darkest -> brightest). The render is a discrete
# 8-stop colorbar; classify each pixel by nearest swatch. The three darkest
# zones (black/gray/blue) are the "excellent and better" keep set.
# ---------------------------------------------------------------------------
SWATCHES: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("pristine", (0, 0, 0)),  # black
    ("excellent", (66, 66, 66)),  # gray
    ("rural", (33, 84, 216)),  # blue
    ("green", (31, 161, 42)),
    ("yellow", (184, 166, 37)),
    ("orange", (253, 150, 80)),
    ("pink", (251, 153, 138)),
    ("white", (242, 242, 242)),
)

# The kept zones: black + gray + blue = "excellent and better".
DARK_ZONES: frozenset[str] = frozenset({"pristine", "excellent", "rural"})


# ---------------------------------------------------------------------------
# Pure geometry helpers (copied from generate_grid.py so this script is
# self-contained: the v2 test target only lists the v2 sources).
# ---------------------------------------------------------------------------
def _bbox(ring: list) -> tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _in_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _polygons(geometry: dict) -> list:
    """Flatten a Polygon/MultiPolygon geometry to a list of (bbox, exterior, holes)."""
    rings_list = (
        [geometry["coordinates"]]
        if geometry["type"] == "Polygon"
        else geometry["coordinates"]
    )
    return [(_bbox(poly[0]), poly[0], poly[1:]) for poly in rings_list]


def _contains(polys: list, x: float, y: float) -> bool:
    for (minx, miny, maxx, maxy), ext, holes in polys:
        if x < minx or x > maxx or y < miny or y > maxy:
            continue
        if _in_ring(x, y, ext) and not any(_in_ring(x, y, h) for h in holes):
            return True
    return False


def _scotland_polygons(admin1: dict) -> list:
    polys: list = []
    for f in admin1["features"]:
        if (f["properties"].get("geonunit") or "") == "Scotland":
            polys.extend(_polygons(f["geometry"]))
    return polys


# ---------------------------------------------------------------------------
# Pure, unit-tested helpers.
# ---------------------------------------------------------------------------
def classify_zone(rgb) -> str:
    """Return the legend zone name whose swatch is nearest to rgb.

    Nearest by squared Euclidean distance in RGB. Computed with Python ints
    (not uint8) so the squared channel deltas cannot overflow: a vectorised
    uint8 implementation would wrap (255-0)**2 in int16 and misclassify.
    """
    r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
    best_name = SWATCHES[0][0]
    best_dist = None
    for name, (sr, sg, sb) in SWATCHES:
        dr = r - sr
        dg = g - sg
        db = b - sb
        dist = dr * dr + dg * dg + db * db
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_name = name
    return best_name


def is_dark_zone(zone: str) -> bool:
    """True when the classified zone is excellent-and-better (kept in the grid)."""
    return zone in DARK_ZONES


def generate_mesh(scotland: list, spacing_km: float) -> list[tuple[float, float]]:
    """Yield (lon, lat) mesh points at spacing_km inside the Scotland land polygons.

    The admin-1 polygons are land boundaries, so this is already a land mesh.
    """
    minx = min(b[0][0] for b in scotland)
    miny = min(b[0][1] for b in scotland)
    maxx = max(b[0][2] for b in scotland)
    maxy = max(b[0][3] for b in scotland)
    mean_lat = (miny + maxy) / 2.0
    lat_step = spacing_km / 111.32
    lon_step = spacing_km / (111.32 * math.cos(math.radians(mean_lat)))

    points: list[tuple[float, float]] = []
    y = miny
    while y <= maxy:
        x = minx
        while x <= maxx:
            if _contains(scotland, x, y):
                points.append((x, y))
            x += lon_step
        y += lat_step
    return points


# ---------------------------------------------------------------------------
# I/O stages (need the heavy geospatial deps; lazily imported so the pure
# helpers above stay importable in the test environment without them).
# ---------------------------------------------------------------------------
_NON_DRIVABLE_FCLASS = frozenset({"path", "footway", "steps", "cycleway", "bridleway"})


def filter_by_road(
    points: list[tuple[float, float]],
    roads_path: str,
    max_dist_m: float = 2000.0,
    drop_non_drivable: bool = True,
) -> list[tuple[float, float]]:
    """Keep (lon, lat) points within max_dist_m of a (drivable) road.

    Roads and points are reprojected to EPSG:27700 (metres) and tested with a
    shapely 2 STRtree dwithin query.
    """
    import geopandas as gpd
    from shapely import STRtree
    from shapely.geometry import Point

    roads = gpd.read_file(roads_path)
    if drop_non_drivable and "fclass" in roads.columns:
        roads = roads[~roads["fclass"].isin(_NON_DRIVABLE_FCLASS)]
    roads = roads.to_crs(epsg=27700)

    pts_wgs = gpd.GeoSeries([Point(lon, lat) for lon, lat in points], crs="EPSG:4326")
    pts_27700 = list(pts_wgs.to_crs(epsg=27700).geometry)

    tree = STRtree(roads.geometry.values)
    # query returns a 2xN array: row 0 = input (point) indices, row 1 = road indices.
    pairs = tree.query(pts_27700, predicate="dwithin", distance=max_dist_m)
    kept_idx = sorted(set(int(i) for i in pairs[0]))
    return [points[i] for i in kept_idx]


def classify_points(
    points: list[tuple[float, float]], raster_path: str
) -> list[tuple[tuple[float, float], str]]:
    """Sample the 3-band RGB LP raster at each (lon, lat) point and classify it.

    Returns (point, zone_name) for every point. The raster is EPSG:4326, so the
    (lon, lat) points sample directly.
    """
    import rasterio

    results: list[tuple[tuple[float, float], str]] = []
    with rasterio.open(raster_path) as src:
        coords = [(lon, lat) for lon, lat in points]
        for point, vals in zip(points, src.sample(coords)):
            rgb = (int(vals[0]), int(vals[1]), int(vals[2]))
            results.append((point, classify_zone(rgb)))
    return results


def build(
    scotland: list,
    roads_path: str,
    raster_path: str,
    spacing_km: float = 2.0,
    drop_non_drivable: bool = True,
) -> list[dict]:
    """Run the full pipeline and return the grid.json site list."""
    mesh = generate_mesh(scotland, spacing_km)
    on_road = filter_by_road(mesh, roads_path, drop_non_drivable=drop_non_drivable)
    classified = classify_points(on_road, raster_path)
    kept = [(pt, zone) for pt, zone in classified if is_dark_zone(zone)]
    # Stable-sort by (lat, lon); ids are assigned after sorting so they follow it.
    kept.sort(key=lambda pz: (pz[0][1], pz[0][0]))

    sites: list[dict] = []
    for (lon, lat), zone in kept:
        sites.append(
            {
                "id": f"scotland-{len(sites):04d}",
                "name": None,
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "altitude_m": 0,
                "lp_zone": zone,
            }
        )
    return sites


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline stars grid-v2 generator.")
    ap.add_argument("--admin1", default="admin1.geojson")
    ap.add_argument("--roads", default="scotland-roads.geojson")
    ap.add_argument("--raster", default="scotland_lp_2024.tif")
    ap.add_argument("--spacing-km", type=float, default=2.0)
    ap.add_argument("--output", default="grid.json")
    ap.add_argument(
        "--keep-non-drivable",
        action="store_true",
        help="Do not drop path/footway/steps/cycleway/bridleway roads.",
    )
    args = ap.parse_args()

    with open(args.admin1) as fh:
        scotland = _scotland_polygons(json.load(fh))
    if not scotland:
        raise SystemExit("no Scotland polygons found (expected geonunit == 'Scotland')")

    sites = build(
        scotland,
        args.roads,
        args.raster,
        spacing_km=args.spacing_km,
        drop_non_drivable=not args.keep_non_drivable,
    )
    with open(args.output, "w") as fh:
        json.dump(sites, fh)
    print(
        f"spacing={args.spacing_km}km -> {len(sites)} road-accessible dark sites "
        f"-> {args.output}"
    )


if __name__ == "__main__":
    main()
