"""Offline point membership for materializers; never infer outside on failure."""

import gzip
import json
import math
from functools import lru_cache
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.prepared import prep


@lru_cache(maxsize=1)
def _boundaries():
    path = Path(__file__).with_name("camping_boundaries.json.gz")
    data = json.loads(gzip.decompress(path.read_bytes()))
    if data["crs"] != "EPSG:4326":
        raise ValueError("camping boundary CRS must be EPSG:4326")
    features = data["features"]
    if (
        sum(f["kind"] == "zone" for f in features) != 4
        or sum(f["kind"] == "park" for f in features) != 2
    ):
        raise ValueError("camping boundary resource is incomplete")
    return [(f["kind"], f["name"], prep(shape(f["geometry"]))) for f in features]


def membership(lat: float, lon: float) -> dict | None:
    """Classify a point, including polygon edges. Holes remain outside.

    None means no usable point; an empty zone/park list means verified outside.
    Resource errors abort materialization, preserving the last good snapshot.
    """
    if (
        lat is None
        or lon is None
        or not math.isfinite(lat)
        or not math.isfinite(lon)
        or not (-90 <= lat <= 90 and -180 <= lon <= 180)
    ):
        return None
    point = Point(lon, lat)
    zones, parks = [], []
    for kind, name, geometry in _boundaries():
        if geometry.covers(point):
            (zones if kind == "zone" else parks).append(name)
    return {"zones": sorted(zones), "parks": sorted(parks)}
