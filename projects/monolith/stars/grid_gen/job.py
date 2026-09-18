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
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

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
    def from_env(cls) -> GridJobConfig:
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


def _setup_otel() -> tuple[object | None, object | None]:
    """Install tracing when an OTLP endpoint is configured."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if not endpoint:
        return None, None

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": "monolith-stars-grid"})
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
        trace.set_tracer_provider(provider)
        logger.info("OpenTelemetry instrumentation enabled")
        return provider, trace.get_tracer("monolith.stars.grid_gen")
    except Exception:
        logger.warning("Failed to initialize OpenTelemetry", exc_info=True)
        return None, None


def _shutdown_otel(provider: object | None) -> None:
    """Flush job spans without allowing telemetry to change the exit status."""
    if provider is None:
        return
    try:
        if not provider.force_flush():
            logger.warning("OpenTelemetry force_flush timed out")
    except Exception:
        logger.warning("Failed to flush OpenTelemetry", exc_info=True)
    try:
        provider.shutdown()
    except Exception:
        logger.warning("Failed to shut down OpenTelemetry", exc_info=True)


def _span(tracer: object | None, name: str):
    if tracer is None:
        return nullcontext()
    return tracer.start_as_current_span(name)


def run(
    config: GridJobConfig,
    *,
    s3=None,
    build_grid: Callable[..., list[dict]] | None = None,
    ingest_grid: Callable[[list[dict]], int] | None = None,
    boundary_parser: Callable[[dict], list] | None = None,
    tracer: object | None = None,
) -> int:
    """Run all stages once and return the number of ingested sites."""
    if build_grid is None:
        from stars.grid_gen.generate_grid_v2 import build as build_grid
    if ingest_grid is None:
        from stars.grid_ingest import replace_computed_grid as ingest_grid
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
        with _span(tracer, "stars.grid.download"):
            for key, destination in inputs.values():
                _download(s3, config.source_bucket, key, destination)

            with inputs["admin1"][1].open(encoding="utf-8") as handle:
                admin1 = json.load(handle)
            scotland = boundary_parser(admin1)
            if not scotland:
                raise ValueError(
                    "admin-1 input contains no feature with geonunit Scotland"
                )

        with _span(tracer, "stars.grid.compute") as compute_span:
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
            if compute_span is not None:
                compute_span.set_attribute("stars.grid.site_count", len(sites))

        with _span(tracer, "stars.grid.ingest") as ingest_span:
            written = ingest_grid(sites)
            if written != len(sites):
                raise RuntimeError(
                    f"ingest wrote {written} sites but computation produced {len(sites)}"
                )
            if ingest_span is not None:
                ingest_span.set_attribute("stars.grid.site_count", written)
        logger.info("computed and ingested %d stars sites", written)
        return written


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    provider, tracer = _setup_otel()
    try:
        with _span(tracer, "stars.grid.job") as job_span:
            written = run(GridJobConfig.from_env(), tracer=tracer)
            if job_span is not None:
                job_span.set_attribute("stars.grid.site_count", written)
    finally:
        _shutdown_otel(provider)


if __name__ == "__main__":
    main()
