"""Hermetic orchestration tests for the dedicated stars grid job."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from stars import grid_ingest
from stars.grid_gen import generate_grid_v2, job
from stars.grid_gen.job import GridJobConfig, run


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


class FakeSpan:
    def __init__(self, name: str):
        self.name = name
        self.attributes = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def set_attribute(self, name: str, value: object) -> None:
        self.attributes[name] = value


class FakeTracer:
    def __init__(self):
        self.spans: list[FakeSpan] = []

    def start_as_current_span(self, name: str) -> FakeSpan:
        span = FakeSpan(name)
        self.spans.append(span)
        return span


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
    tracer = FakeTracer()
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
            tracer=tracer,
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
    assert [span.name for span in tracer.spans] == [
        "stars.grid.download",
        "stars.grid.compute",
        "stars.grid.ingest",
    ]
    assert tracer.spans[1].attributes == {"stars.grid.site_count": 1}
    assert tracer.spans[2].attributes == {"stars.grid.site_count": 1}


def test_run_resolves_default_module_imports(config, monkeypatch):
    sites = [{"id": "scotland-0000", "lat": 56.5, "lon": -4.5}]
    monkeypatch.setattr(generate_grid_v2, "_scotland_polygons", lambda _data: ["land"])
    monkeypatch.setattr(generate_grid_v2, "build", lambda *_args, **_kwargs: sites)
    monkeypatch.setattr(grid_ingest, "replace_computed_grid", lambda rows: len(rows))

    assert run(config, s3=FakeS3()) == 1


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


def test_run_rejects_duplicate_ids_without_ingesting(config):
    ingested = []
    duplicate = {"id": "same", "lat": 56.5, "lon": -4.5}
    with pytest.raises(RuntimeError, match="duplicate site ids"):
        run(
            config,
            s3=FakeS3(),
            build_grid=lambda *args, **kwargs: [duplicate, dict(duplicate)],
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


def test_setup_otel_installs_exporter_and_grid_tracer(monkeypatch):
    endpoint = "http://collector.example:4318/v1/traces"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    provider = mock.Mock()
    processor = mock.Mock()

    with (
        mock.patch(
            "opentelemetry.sdk.trace.TracerProvider", return_value=provider
        ) as provider_class,
        mock.patch(
            "opentelemetry.sdk.trace.export.BatchSpanProcessor",
            return_value=processor,
        ) as processor_class,
        mock.patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        ) as exporter_class,
        mock.patch("opentelemetry.trace.set_tracer_provider") as set_provider,
        mock.patch("opentelemetry.trace.get_tracer") as get_tracer,
    ):
        result = job._setup_otel()

    assert result == (provider, get_tracer.return_value)
    exporter_class.assert_called_once_with(endpoint=endpoint)
    processor_class.assert_called_once_with(exporter_class.return_value)
    provider.add_span_processor.assert_called_once_with(processor)
    set_provider.assert_called_once_with(provider)
    get_tracer.assert_called_once_with("monolith.stars.grid_gen")
    resource = provider_class.call_args.kwargs["resource"]
    assert resource.attributes["service.name"] == "monolith-stars-grid"


def test_shutdown_otel_flushes_and_shuts_down():
    provider = mock.Mock()
    provider.force_flush.return_value = True

    job._shutdown_otel(provider)

    provider.force_flush.assert_called_once_with()
    provider.shutdown.assert_called_once_with()


def test_main_records_job_span_and_flushes(monkeypatch, config):
    provider = mock.Mock()
    provider.force_flush.return_value = True
    tracer = FakeTracer()
    monkeypatch.setattr(job, "_setup_otel", lambda: (provider, tracer))
    monkeypatch.setattr(GridJobConfig, "from_env", lambda: config)
    monkeypatch.setattr(job, "run", lambda _config, *, tracer: 7)

    job.main()

    assert [span.name for span in tracer.spans] == ["stars.grid.job"]
    assert tracer.spans[0].attributes == {"stars.grid.site_count": 7}
    provider.force_flush.assert_called_once_with()
    provider.shutdown.assert_called_once_with()


def test_main_flushes_after_job_failure(monkeypatch, config):
    provider = mock.Mock()
    provider.force_flush.return_value = True
    monkeypatch.setattr(job, "_setup_otel", lambda: (provider, None))
    monkeypatch.setattr(GridJobConfig, "from_env", lambda: config)

    def fail_run(_config, *, tracer):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(job, "run", fail_run)

    with pytest.raises(RuntimeError, match="database unavailable"):
        job.main()

    provider.force_flush.assert_called_once_with()
    provider.shutdown.assert_called_once_with()
