"""Offline generator for the stars grid (grid.json), uploaded to SeaweedFS.

This is NOT runtime code (it is excluded from the monolith image in BUILD). It
builds the grid as: a mesh inside stargazer's light-pollution dark mask,
intersected with **land** and clipped to **Scotland**. The LP atlas marks open
sea as dark, so without the land/Scotland clip the grid is ~94% offshore; this
keeps only Scottish land dark-sky points. Pure-Python ray-casting point-in-polygon
(no geopandas).

Inputs:
  - dark_regions.geojson: the LP dark mask (a MultiPolygon). The stargazer service
    was decommissioned 2026-06; its data is archived in SeaweedFS. Pull via S3,
    with $EP set to the in-cluster SeaweedFS S3 endpoint (seaweedfsS3Endpoint in
    the monolith chart; creds default to duckdb/duckdb, see chat/store.py):
        aws --endpoint-url "$EP" s3 cp s3://stargazer-archive/processed/dark_regions.geojson dark_regions.geojson
  - admin1.geojson: Natural Earth 10m admin-1, for the Scotland boundary:
        curl -o admin1.geojson https://raw.githubusercontent.com/nvkelso/\
natural-earth-vector/master/geojson/ne_10m_admin_1_states_provinces.geojson
    (features with properties.geonunit == "Scotland" are the Scottish council areas).

Usage:
    python3 generate_grid.py --dark-regions dark_regions.geojson \
        --admin1 admin1.geojson --spacing-km 4 --output grid.json
    # 4 km ~= 306 Scotland land points; finer spacing = more points = more met.no calls.
    # Upload (SeaweedFS S3 auth is disabled cluster-wide; unsigned PUT works):
    kubectl port-forward -n seaweedfs svc/seaweedfs-s3 8333:8333 &
    curl -X PUT http://localhost:8333/stars                          # create bucket once
    curl -X PUT -T grid.json http://localhost:8333/stars/grid.json   # bucket=stars key=grid.json
    # stars.load_grid ingests it into stars.sites on its next run (or trigger it).

Known limitation: this keeps all dark Scottish land (good: remote roadless spots are
the best skies), so it does NOT apply stargazer's road-accessibility filter, and
lp_zone is the binary "dark" (per-point light-pollution values need the LP raster,
which needs rasterio). Both are refine-and-re-upload follow-ups, no code change.
"""

from __future__ import annotations

import argparse
import json
import math


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


def generate(dark_regions: dict, scotland: list, spacing_km: float) -> list[dict]:
    dark = _polygons(dark_regions["features"][0]["geometry"])
    minx = min(b[0][0] for b in dark)
    miny = min(b[0][1] for b in dark)
    maxx = max(b[0][2] for b in dark)
    maxy = max(b[0][3] for b in dark)
    mean_lat = (miny + maxy) / 2.0
    lat_step = spacing_km / 111.32
    lon_step = spacing_km / (111.32 * math.cos(math.radians(mean_lat)))

    sites: list[dict] = []
    y = miny
    while y <= maxy:
        x = minx
        while x <= maxx:
            if _contains(dark, x, y) and _contains(scotland, x, y):
                sites.append(
                    {
                        "id": f"scotland-{len(sites):04d}",
                        "name": None,
                        "lat": round(y, 4),
                        "lon": round(x, 4),
                        "altitude_m": 0,
                        "lp_zone": "dark",
                    }
                )
            x += lon_step
        y += lat_step
    return sites


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dark-regions", default="dark_regions.geojson")
    ap.add_argument("--admin1", default="admin1.geojson")
    ap.add_argument("--spacing-km", type=float, default=4.0)
    ap.add_argument("--output", default="grid.json")
    args = ap.parse_args()

    with open(args.dark_regions) as fh:
        dark_regions = json.load(fh)
    with open(args.admin1) as fh:
        scotland = _scotland_polygons(json.load(fh))
    if not scotland:
        raise SystemExit("no Scotland polygons found (expected geonunit == 'Scotland')")
    sites = generate(dark_regions, scotland, args.spacing_km)
    with open(args.output, "w") as fh:
        json.dump(sites, fh)
    print(
        f"spacing={args.spacing_km}km -> {len(sites)} Scotland land sites -> {args.output}"
    )


if __name__ == "__main__":
    main()
