"""Offline OS Open Names association for existing snapshot coordinates.

Input is OS's CSV header followed by unmodified tile rows. No downloads, grid
replacement or database writes. See open_names.md for provenance and limits.
"""

from __future__ import annotations

import csv
import math
import os
import re
from collections import defaultdict
from pathlib import Path

MAX_DISTANCE_M = 20_000
# EPSG operation 1314 (OSGB36 to WGS84), inverted, then British National Grid.
# Explicit Helmert parameters avoid environment-dependent OSTN grids/downloads.
_PIPELINE = (
    "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
    "+step +proj=cart +ellps=WGS84 +step +inv +proj=helmert "
    "+x=446.448 +y=-125.157 +z=542.060 +rx=0.1502 +ry=0.2470 "
    "+rz=0.8421 +s=-20.4894 +convention=position_vector "
    "+step +inv +proj=cart +ellps=airy +step +proj=tmerc "
    "+lat_0=49 +lon_0=-2 +k=0.9996012717 +x_0=400000 +y_0=-100000 +ellps=airy"
)


class OpenNames:
    def __init__(self, path: str, release: str):
        if not re.fullmatch(r"20\d{2}-(01|04|07|10)", release):
            raise ValueError("OS Open Names release must be YYYY-MM (quarterly)")
        # Lazy: disabled runs neither import PROJ nor open the local input.
        from pyproj import Transformer

        self.project = Transformer.from_pipeline(_PIPELINE)
        self.release = release
        self.cells = defaultdict(list)
        seen = set()
        with Path(path).open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            required = {
                "ID",
                "NAME1",
                "NAME2",
                "TYPE",
                "LOCAL_TYPE",
                "GEOMETRY_X",
                "GEOMETRY_Y",
            }
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("OS Open Names CSV requires the official header")
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError("malformed OS Open Names CSV row")
                if row["TYPE"] != "populatedPlace":
                    continue
                name = row["NAME1"].strip() or row["NAME2"].strip()
                if not name:
                    continue
                # Concatenated tiles retain their BOM at the start of each tile.
                feature_id = row["ID"].removeprefix("\ufeff").strip()
                if not feature_id or feature_id in seen:
                    raise ValueError(
                        "missing or duplicate OS Open Names populated-place ID"
                    )
                seen.add(feature_id)
                x, y = float(row["GEOMETRY_X"]), float(row["GEOMETRY_Y"])
                if not (
                    math.isfinite(x)
                    and math.isfinite(y)
                    and 0 <= x <= 700000
                    and 0 <= y <= 1300000
                ):
                    raise ValueError("invalid British National Grid coordinate")
                self.cells[(int(x // MAX_DISTANCE_M), int(y // MAX_DISTANCE_M))].append(
                    (x, y, feature_id, name)
                )
        if not seen:
            raise ValueError("OS Open Names input contains no named populated places")

    def nearby(self, lat: float, lon: float) -> dict | None:
        x, y = self.project.transform(lon, lat, errcheck=True)
        cx, cy = int(x // MAX_DISTANCE_M), int(y // MAX_DISTANCE_M)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for px, py, feature_id, name in self.cells.get((cx + dx, cy + dy), ()):
                    distance = math.hypot(x - px, y - py)
                    candidate = (distance, feature_id, name)
                    if distance <= MAX_DISTANCE_M and (
                        best is None or candidate < best
                    ):
                        best = candidate
        if best is None:
            return None
        distance, feature_id, name = best
        return {
            "label": f"Near {name}",
            "name": name,
            "feature_id": feature_id,
            "source": "OS Open Names",
            "source_version": self.release,
            "distance_km": round(distance / 1000, 3),
            "distance_method": "British National Grid straight-line to populated-place point",
            "attribution": f"Contains OS data © Crown Copyright and database rights {self.release[:4]}.",
        }


def configured_names() -> OpenNames | None:
    path = os.environ.get("STARS_OPEN_NAMES_CSV", "").strip()
    return (
        OpenNames(path, os.environ.get("STARS_OPEN_NAMES_RELEASE", ""))
        if path
        else None
    )


def enrich(payload: dict, names: OpenNames | None) -> None:
    for site in payload["sites"]:
        site["nearby_place"] = names.nearby(site["lat"], site["lon"]) if names else None
