"""Hermetic orchestration tests for the dedicated stars grid job."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job import GridJobConfig, run


_ADMIN1 = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"geonunit": "Scotland"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[-5.0, 56.0], [-4.0, 56.0], [-4.0, 57.0], [-5.0, 57.0]]
                ],
            },
        }
    ],
}


class FakeS3:
    def __init__(self, *, fail_key: str | None = None):
        self.fail_key = fail_key
        self.downloads: list[tuple[str, str, Path]] = []

    def download_file(self, bucket: str, key: str, destination: str) -> None:
        if key == self.fail_key:
            raise RuntimeError("source unavailable")
        path = Path(destination)
        payload = json.dumps(_ADMIN1).encode() if key == "admin" else b"input"
        path.write_bytes(payload)
        self.downloads.append((bucket, key, path))


@pytest.fixture(name="config")
def config_fixture(tmp_path):
    return GridJobConfig(
        source_endpoint="https://objects.invalid",
        source_region="auto",
        source_bucket="source-bucket",
        admin1_key="admin",
        roads_key="roads",
        light_pollution_key="lp",
        dem_key="dem",
        work_dir=tmp_path / "work",
        spacing_km=3.0,
        max_road_distance_m=1500.0,
    )


def test_run_downloads_computes_and_ingests(config):
    s3 = FakeS3()
    observed = {}
    sites = [
        {
            "id": "scotland-0000",
            "lat": 56.5,
            "lon": -4.5,
            "altitude_m": 321,
            "lp_zone": "excellent",
        }
    ]

    def build_grid(scotland, roads, light_pollution, dem, **kwargs):
        observed["scotland"] = scotland
        observed["paths"] = [roads, light_pollution, dem]
        observed["kwargs"] = kwargs
        assert all(Path(path).is_file() for path in observed["paths"])
        return sites

    def ingest_grid(rows):
        observed["ingested"] = rows
        return len(rows)

    assert (
        run(
            config,
            s3=s3,
            build_grid=build_grid,
            ingest_grid=ingest_grid,
            boundary_parser=lambda admin1: ["scotland"],
        )
        == 1
    )
    assert [item[1] for item in s3.downloads] == ["admin", "roads", "lp", "dem"]
    assert observed["scotland"]
    assert observed["kwargs"] == {
        "spacing_km": 3.0,
        "max_road_distance_m": 1500.0,
    }
    assert observed["ingested"] == sites
    assert list(config.work_dir.iterdir()) == []


def test_run_propagates_source_failure_without_ingesting(config):
    ingested = []
    with pytest.raises(RuntimeError, match="source unavailable"):
        run(
            config,
            s3=FakeS3(fail_key="roads"),
            build_grid=lambda *args, **kwargs: pytest.fail("build must not run"),
            ingest_grid=lambda rows: ingested.append(rows),
            boundary_parser=lambda admin1: ["scotland"],
        )
    assert ingested == []
    assert list(config.work_dir.iterdir()) == []


def test_run_rejects_empty_computation_without_ingesting(config):
    ingested = []
    with pytest.raises(RuntimeError, match="produced no sites"):
        run(
            config,
            s3=FakeS3(),
            build_grid=lambda *args, **kwargs: [],
            ingest_grid=lambda rows: ingested.append(rows),
            boundary_parser=lambda admin1: ["scotland"],
        )
    assert ingested == []


def test_run_propagates_ingest_failure(config):
    def fail_ingest(rows):
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        run(
            config,
            s3=FakeS3(),
            build_grid=lambda *args, **kwargs: [{"id": "one"}],
            ingest_grid=fail_ingest,
            boundary_parser=lambda admin1: ["scotland"],
        )


def test_config_requires_every_input_key(monkeypatch, tmp_path):
    values = {
        "STARS_GRID_SOURCE_S3_BUCKET": "bucket",
        "STARS_GRID_ADMIN1_KEY": "admin",
        "STARS_GRID_ROADS_KEY": "roads",
        "STARS_GRID_LIGHT_POLLUTION_KEY": "lp",
        "STARS_GRID_DEM_KEY": "dem",
        "STARS_GRID_WORK_DIR": str(tmp_path),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("STARS_GRID_DEM_KEY", "")

    with pytest.raises(ValueError, match="STARS_GRID_DEM_KEY must be configured"):
        GridJobConfig.from_env()
