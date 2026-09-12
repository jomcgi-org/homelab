"""Compute and ingest the stars site grid in a dedicated one-shot process.

The API and general-purpose jobs binaries exclude this package. This entrypoint
downloads explicitly configured geospatial inputs into ephemeral storage, runs
the light-pollution, road-accessibility, and DEM stages, then transactionally
replaces ``stars.sites``. Exceptions are deliberately not swallowed so Argo
records a failed workflow and retains its pod for investigation.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger("monolith.stars.grid_gen")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be configured")
    return value


def _positive_float_env(name: str, default: str) -> float:
    value = float(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class GridJobConfig:
    source_endpoint: str
    source_region: str
    source_bucket: str
    admin1_key: str
    roads_key: str
    light_pollution_key: str
    dem_key: str
    work_dir: Path
    spacing_km: float
    max_road_distance_m: float

    @classmethod
    def from_env(cls) -> "GridJobConfig":
        endpoint = os.environ.get("STARS_GRID_SOURCE_S3_ENDPOINT", "").strip()
        if endpoint and not endpoint.startswith(("http://", "https://")):
            raise ValueError(
                "STARS_GRID_SOURCE_S3_ENDPOINT must start with http:// or https://"
            )
        return cls(
            source_endpoint=endpoint,
            source_region=os.environ.get("STARS_GRID_SOURCE_S3_REGION", "").strip(),
            source_bucket=_required_env("STARS_GRID_SOURCE_S3_BUCKET"),
            admin1_key=_required_env("STARS_GRID_ADMIN1_KEY"),
            roads_key=_required_env("STARS_GRID_ROADS_KEY"),
            light_pollution_key=_required_env("STARS_GRID_LIGHT_POLLUTION_KEY"),
            dem_key=_required_env("STARS_GRID_DEM_KEY"),
            work_dir=Path(os.environ.get("STARS_GRID_WORK_DIR", "/tmp/stars-grid")),
            spacing_km=_positive_float_env("STARS_GRID_SPACING_KM", "2"),
            max_road_distance_m=_positive_float_env(
                "STARS_GRID_MAX_ROAD_DISTANCE_M", "2000"
            ),
        )


def _s3_client(config: GridJobConfig):
    import boto3
    from botocore.config import Config

    kwargs: dict[str, object] = {
        "config": Config(s3={"addressing_style": "path"}),
    }
    if config.source_endpoint:
        kwargs["endpoint_url"] = config.source_endpoint
    if config.source_region:
        kwargs["region_name"] = config.source_region
    access_key = os.environ.get("STARS_GRID_SOURCE_S3_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("STARS_GRID_SOURCE_S3_SECRET_ACCESS_KEY", "").strip()
    if bool(access_key) != bool(secret_key):
        raise ValueError("both source S3 credential variables must be set together")
    if access_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
    return boto3.client("s3", **kwargs)


def _download(s3, bucket: str, key: str, destination: Path) -> None:
    logger.info("downloading s3://%s/%s", bucket, key)
    s3.download_file(bucket, key, str(destination))
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError(f"downloaded input is empty: s3://{bucket}/{key}")


def run(
    config: GridJobConfig,
    *,
    s3=None,
    build_grid: Callable[..., list[dict]] | None = None,
    ingest_grid: Callable[[list[dict]], int] | None = None,
    boundary_parser: Callable[[dict], list] | None = None,
) -> int:
    """Run all stages once and return the number of ingested sites."""
    if build_grid is None:
        from stars.grid_gen.generate_grid_v2 import build as build_grid
    if ingest_grid is None:
        from stars.grid import replace_grid as ingest_grid
    if boundary_parser is None:
        from stars.grid_gen.generate_grid_v2 import (
            _scotland_polygons as boundary_parser,
        )
    if s3 is None:
        s3 = _s3_client(config)

    config.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=config.work_dir) as temp:
        work = Path(temp)
        inputs = {
            "admin1": (config.admin1_key, work / "admin1.geojson"),
            "roads": (config.roads_key, work / "roads.geojson"),
            "light_pollution": (
                config.light_pollution_key,
                work / "light-pollution.tif",
            ),
            "dem": (config.dem_key, work / "dem.tif"),
        }
        for key, destination in inputs.values():
            _download(s3, config.source_bucket, key, destination)

        with inputs["admin1"][1].open(encoding="utf-8") as handle:
            admin1 = json.load(handle)
        scotland = boundary_parser(admin1)
        if not scotland:
            raise ValueError("admin-1 input contains no feature with geonunit Scotland")

        sites = build_grid(
            scotland,
            str(inputs["roads"][1]),
            str(inputs["light_pollution"][1]),
            str(inputs["dem"][1]),
            spacing_km=config.spacing_km,
            max_road_distance_m=config.max_road_distance_m,
        )
        if not sites:
            raise RuntimeError("geospatial computation produced no sites")
        if len({site.get("id") for site in sites}) != len(sites):
            raise RuntimeError("geospatial computation produced duplicate site ids")

        written = ingest_grid(sites)
        if written != len(sites):
            raise RuntimeError(
                f"ingest wrote {written} sites but computation produced {len(sites)}"
            )
        logger.info("computed and ingested %d stars sites", written)
        return written


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run(GridJobConfig.from_env())


if __name__ == "__main__":
    main()
