"""Offline generator for the stars grid (grid.json), uploaded to SeaweedFS.

This is NOT runtime code (it is excluded from the monolith image in BUILD). It
reproduces stargazer's generate_sample_grid (projects/stargazer/backend/spatial.py)
without geopandas: mesh the bounds of the light-pollution dark mask at a chosen
spacing and keep points inside it, via pure-Python ray-casting point-in-polygon.

Input: dark_regions.geojson (the LP-filtered dark-sky MultiPolygon) from the
stargazer pipeline's PVC (/data/processed/dark_regions.geojson). Output: grid.json
in the shape the stars.load_grid job ingests.

Usage:
    # 1. Pull the dark mask from the running stargazer pod (shell-less, use the api container):
    kubectl exec -n stargazer <stargazer-api-pod> -c api -- \
        cat /data/processed/dark_regions.geojson > dark_regions.geojson
    # 2. Generate (10 km mesh ~= 1300 points; smaller spacing = more points = more met.no calls):
    python3 generate_grid.py --input dark_regions.geojson --spacing-km 10 --output grid.json
    # 3. Upload to SeaweedFS S3 (auth disabled cluster-wide; unsigned PUT works):
    kubectl port-forward -n seaweedfs svc/seaweedfs-s3 8333:8333 &
    curl -X PUT http://localhost:8333/stars                              # create bucket once
    curl -X PUT -T grid.json http://localhost:8333/stars/grid.json       # bucket=stars key=grid.json
    # The stars.load_grid job ingests it into stars.sites on its next run.

Known limitation: the LP atlas marks open sea as dark, and this skips
stargazer's road-accessibility intersection (it needs the ~193 MB roads file),
so some points fall offshore. Refining to land/road-reachable points is a
follow-up: regenerate and re-upload, no code or deploy change.
"""

from __future__ import annotations

import argparse
import json
import math


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


def _bbox(ring: list) -> tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def generate(dark_regions: dict, spacing_km: float) -> list[dict]:
    geom = dark_regions["features"][0]["geometry"]
    if geom["type"] != "MultiPolygon":
        raise ValueError(f"expected MultiPolygon, got {geom['type']}")
    polygons = geom["coordinates"]
    bboxes = [_bbox(poly[0]) for poly in polygons]

    def in_mask(x: float, y: float) -> bool:
        for poly, (minx, miny, maxx, maxy) in zip(polygons, bboxes):
            if x < minx or x > maxx or y < miny or y > maxy:
                continue
            if not _in_ring(x, y, poly[0]):
                continue
            if any(_in_ring(x, y, hole) for hole in poly[1:]):
                continue
            return True
        return False

    minx = min(b[0] for b in bboxes)
    miny = min(b[1] for b in bboxes)
    maxx = max(b[2] for b in bboxes)
    maxy = max(b[3] for b in bboxes)
    mean_lat = (miny + maxy) / 2.0
    lat_step = spacing_km / 111.32
    lon_step = spacing_km / (111.32 * math.cos(math.radians(mean_lat)))

    sites: list[dict] = []
    y = miny
    while y <= maxy:
        x = minx
        while x <= maxx:
            if in_mask(x, y):
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
    ap.add_argument("--input", default="dark_regions.geojson")
    ap.add_argument("--spacing-km", type=float, default=10.0)
    ap.add_argument("--output", default="grid.json")
    args = ap.parse_args()

    with open(args.input) as fh:
        dark_regions = json.load(fh)
    sites = generate(dark_regions, args.spacing_km)
    with open(args.output, "w") as fh:
        json.dump(sites, fh)
    print(f"spacing={args.spacing_km}km -> {len(sites)} sites -> {args.output}")


if __name__ == "__main__":
    main()
