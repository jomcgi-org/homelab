"""Staged UKV sampled-field materializer. No provider acquisition is installed.

Input is a bounded local, explicitly sampled UKV extract. A future authorized
collector must crop/decode provider fields and supply its provenance here.
Real points are walk starts until DoBIH coverage is separately delivered.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_BYTES = 16 << 20
MAX_POINTS = 8192
LICENSE = "https://creativecommons.org/licenses/by-sa/4.0/"
SOURCE = "s3://met-office-atmospheric-model-data/uk-deterministic-2km/"
ADAPTATION = "UKV hourly fields sampled at declared points; no elevation correction or summit inference."
FIELDS = {
    "wind_kmh": (0, 500),
    "gust_kmh": (0, 500),
    "cloud_base_m": (0, 20000),
    "freezing_level_m_asl": (-1000, 20000),
}


def _utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamps must use UTC Z")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.minute or dt.second or dt.microsecond:
        raise ValueError("forecast times must be whole UTC hours")
    return dt


def _number(value, lo: float, hi: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and lo <= value <= hi


def build_payload(extract: dict, now: datetime, *, fixture: bool = False) -> dict:
    """Validate a sampled extract and preserve nulls. Never fill absent weather.

    Surface height for AGL cloud base is model orography, separately supplied
    from point elevation. Both use mean sea level. Unknown height references
    remain explicit and cannot support a hazard comparison.
    """
    run = _utc(extract["run_at"])
    now = now.astimezone(timezone.utc)
    if run > now or now - run > timedelta(hours=12):
        raise ValueError("future or stale model run")
    points = extract["points"]
    if not isinstance(points, list) or not 0 < len(points) <= MAX_POINTS:
        raise ValueError("point limit")
    seen = set()
    per_walk = {}
    clean = []
    for p in points:
        kind = p["kind"]
        if kind not in ("walk_start", "summit") or (kind == "summit" and not fixture):
            raise ValueError("real samples must be walk_start until DoBIH delivery")
        if not all(
            isinstance(p.get(k), str) and 0 < len(p[k]) <= 200
            for k in ("id", "walk_id")
        ):
            raise ValueError("point identity")
        if p["id"] in seen:
            raise ValueError("duplicate point")
        seen.add(p["id"])
        per_walk[p["walk_id"]] = per_walk.get(p["walk_id"], 0) + 1
        if per_walk[p["walk_id"]] > 8:
            raise ValueError("per-walk point limit")
        for k, lo, hi in (
            ("lat", 54, 61),
            ("lon", -9, 0),
            ("sample_lat", 54, 61),
            ("sample_lon", -9, 0),
        ):
            if not _number(p.get(k), lo, hi):
                raise ValueError("coordinates")
        if (
            abs(p["lat"] - p["sample_lat"]) > 0.05
            or abs(p["lon"] - p["sample_lon"]) > 0.05
        ):
            raise ValueError("sample too far from point")
        if p["sampling"] != "nearest_grid_point":
            raise ValueError("unsupported sampling")
        if p["cloud_base_reference"] not in (
            "mean_sea_level",
            "above_ground",
            "unknown",
        ):
            raise ValueError("cloud height reference")
        for k in ("elevation_m_asl", "orography_m_asl"):
            if p.get(k) is not None and not _number(p[k], -500, 2000):
                raise ValueError("elevation")
        hours = p["hours"]
        if not isinstance(hours, list) or len(hours) > 121:
            raise ValueError("hour limit")
        previous = None
        for h in hours:
            valid = _utc(h["valid_at"])
            if (
                valid < run
                or valid > run + timedelta(hours=120)
                or (previous and valid <= previous)
            ):
                raise ValueError("invalid or unordered forecast time")
            previous = valid
            for k, (lo, hi) in FIELDS.items():
                if h.get(k) is not None and not _number(h[k], lo, hi):
                    raise ValueError("invalid " + k)
        clean.append(
            {
                **{
                    k: p[k]
                    for k in (
                        "id",
                        "walk_id",
                        "kind",
                        "lat",
                        "lon",
                        "sample_lat",
                        "sample_lon",
                        "sampling",
                        "cloud_base_reference",
                    )
                },
                "elevation_m_asl": p.get("elevation_m_asl"),
                "orography_m_asl": p.get("orography_m_asl"),
                "hours": [
                    {"valid_at": h["valid_at"], **{k: h.get(k) for k in FIELDS}}
                    for h in hours
                ],
            }
        )
    payload = {
        "schema_version": 1,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "run_at": extract["run_at"],
        "source": SOURCE,
        "license_uri": LICENSE,
        "adaptation": ADAPTATION,
        "fixture": fixture,
        "points": clean,
    }
    if len(encode(payload)) > MAX_BYTES:
        raise ValueError("snapshot byte limit")
    return payload


def encode(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


def sample_grid(
    grid: dict, points: list[dict], now: datetime, *, fixture=False
) -> dict:
    """Sample a predecoded Scotland crop, with explicit provider units.

    No GRIB decoder or provider downloader is installed by this staged job.
    Grid cells carry coordinates, model orography and hourly values. Nearest
    sampling uses angular distance scaled by latitude, bounded again by the
    0.05-degree coordinate tolerance in the wire validator. No extrapolation,
    lapse-rate correction, or interpolation of missing timesteps is performed.
    """
    cells = grid["cells"]
    if not isinstance(cells, list) or not 0 < len(cells) <= 16384:
        raise ValueError("grid cell limit")
    if not isinstance(points, list) or not 0 < len(points) <= MAX_POINTS:
        raise ValueError("point limit")
    units = grid["units"]
    if (
        units["wind"] not in ("m/s", "km/h")
        or units["gust"] not in ("m/s", "km/h")
        or units["height"] != "m"
    ):
        raise ValueError("unsupported provider units")
    for c in cells:
        if not _number(c.get("lat"), 54, 61) or not _number(c.get("lon"), -9, 0):
            raise ValueError("grid coordinates")
    samples = []
    for p in points:
        if not _number(p.get("lat"), 54, 61) or not _number(p.get("lon"), -9, 0):
            raise ValueError("point coordinates")
        scale = math.cos(math.radians(p["lat"])) ** 2
        cell = min(
            cells,
            key=lambda c: (
                (c["lat"] - p["lat"]) ** 2 + scale * (c["lon"] - p["lon"]) ** 2
            ),
        )
        hours = []
        if len(cell["hours"]) > 121:
            raise ValueError("hour limit")
        for h in cell["hours"]:
            converted = {"valid_at": h["valid_at"]}
            for source, target in (
                ("wind", "wind_kmh"),
                ("gust", "gust_kmh"),
                ("cloud_base", "cloud_base_m"),
                ("freezing_level", "freezing_level_m_asl"),
            ):
                value = h.get(source)
                if value is not None:
                    if not _number(value, -1000, 20000):
                        raise ValueError("invalid provider field")
                    if source in ("wind", "gust") and units[source] == "m/s":
                        value *= 3.6
                converted[target] = value
            hours.append(converted)
        samples.append(
            {
                **p,
                "sample_lat": cell["lat"],
                "sample_lon": cell["lon"],
                "sampling": "nearest_grid_point",
                "orography_m_asl": cell.get("orography_m_asl"),
                "cloud_base_reference": grid["cloud_base_reference"],
                "hours": hours,
            }
        )
    return build_payload(
        {"run_at": grid["run_at"], "points": samples}, now, fixture=fixture
    )


def materialize_sync(*, collect=None, storage=None, now=None) -> int | None:
    """Flag precedes every input, storage and credential operation, even manually.

    collect/storage are fake seams for tests. Runtime accepts a local sampled
    extract only; it does not retrieve UKV data or implement GRIB adaptation.
    """
    if os.environ.get("UKV_PRODUCER_ENABLED", "false") != "true":
        return None
    if collect is None:
        path = Path(os.environ["UKV_SAMPLED_INPUT"])
        with path.open("rb") as stream:
            body = stream.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("input byte limit")
        extract = json.loads(body)
    else:
        extract = collect()
    at = now or datetime.now(timezone.utc)
    if "grid" in extract:
        payload = sample_grid(extract["grid"], extract["points"], at)
    else:
        payload = build_payload(extract, at)
    body = encode(payload)
    if storage is None:
        from hikes.materialize import _s3_client

        storage = _s3_client()
    storage.put_object(
        Bucket=os.environ.get("STARS_GRID_S3_BUCKET", "stars"),
        Key="summits.json",
        Body=body,
        ContentType="application/json",
        CacheControl="no-store",
    )
    return len(body)
