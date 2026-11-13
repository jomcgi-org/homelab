"""Captured KiWIS shapes, real pinned serializers and in-memory atomic storage."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import boto3
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from botocore.validate import validate_parameters
import httpx
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from sepa import collector as c

START = c.utc("2021-10-22T23:45:00Z")
END = c.utc("2021-10-23T00:15:00Z")
INGESTED = c.utc("2026-11-13T09:00:00Z")


def fixture(name):
    return json.loads((Path(__file__).parent / "fixtures" / f"{name}.json").read_text())


def records(payload=None, ingested=INGESTED):
    return c.parse_values(
        fixture("values") if payload is None else payload,
        c.STATIONS[0],
        START,
        END,
        ingested,
    )


def client():
    # Literal test credentials prevent any default credential-chain/metadata lookup.
    return boto3.client(
        "s3",
        endpoint_url="https://example.invalid",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="auto",
    )


def test_documented_metadata_shapes_and_station_selection():
    c.validate_catalog(c.STATIONS, fixture("series"), fixture("stations"))
    assert c.configured_stations("9514") == (c.STATIONS[1],)
    for invalid in ("", "133171,133171", "36354", "9514,unknown", "*"):
        with pytest.raises(ValueError):
            c.configured_stations(invalid)
    assert fixture("quality")[2]["key"] == 140
    assert fixture("quality")[2]["code"] == "Prov"


@pytest.mark.parametrize(
    "field,value",
    [
        ("ts_unitsymbol", "mm"),
        ("stationparameter_no", "RE"),
        ("stationparameter_name", "Rain"),
        ("ts_path", "1/133171/RE/15m.Total"),
        ("station_id", "133171"),
        ("ts_name", "Hour.Cmd"),
    ],
)
def test_rejects_wrong_measurement_metadata(field, value):
    payload = fixture("series")
    payload[1][payload[0].index(field)] = value
    with pytest.raises(ValueError):
        c.validate_catalog(c.STATIONS, payload, fixture("stations"))


@pytest.mark.parametrize(
    "payload", [[], {}, {"type": "error"}, [["ts_id"], ["a", "b"]], [["a", "a"]]]
)
def test_bad_metadata_tables(payload):
    with pytest.raises(ValueError):
        c.validate_catalog(c.STATIONS, payload, fixture("stations"))


def test_partial_station_catalog():
    sites = fixture("stations")
    sites.pop()
    with pytest.raises(ValueError):
        c.validate_catalog(c.STATIONS, fixture("series"), sites)


def test_stage_timestamps_missing_and_quality():
    rows = records()
    assert rows[0]["value"] == 0.971
    assert rows[0]["units"] == "m"
    assert rows[0]["station_id"] == "36354"
    assert rows[0]["river"] == "Cree"
    assert rows[0]["quality_code"] == 50
    assert rows[0]["provisional"] is False
    payload = fixture("values")
    payload[0]["data"][0] = ["2021-10-23T00:45:00+01:00", None, 140]
    rows = records(payload)
    assert rows[0]["observed_at"] == "2021-10-22T23:45:00+00:00"
    assert rows[0]["missing"] is True
    assert rows[0]["provisional"] is True
    assert rows[0]["revision_possible"] is True


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.clear(),
        lambda p: p.append(deepcopy(p[0])),
        lambda p: p[0].update(ts_unitsymbol="ft"),
        lambda p: p[0].update(data=[]),
        lambda p: p[0].update(rows="4"),
        lambda p: p[0].update(columns="Timestamp,Value"),
        lambda p: p[0]["data"][0].pop(),
        lambda p: p[0]["data"][0].__setitem__(0, "2021-10-22T23:45:00"),
        lambda p: p[0]["data"][0].__setitem__(0, "2021-10-22T23:46:00Z"),
        lambda p: p[0]["data"][0].__setitem__(0, "2021-10-21T23:45:00Z"),
        lambda p: p[0]["data"][0].__setitem__(1, "0.971"),
        lambda p: p[0]["data"][0].__setitem__(1, float("nan")),
        lambda p: p[0]["data"][0].__setitem__(2, None),
        lambda p: p[0]["data"][0].__setitem__(2, True),
        lambda p: p[0]["data"].__setitem__(1, p[0]["data"][0]),
    ],
)
def test_malformed_partial_and_empty_values(change):
    payload = fixture("values")
    change(payload)
    with pytest.raises((ValueError, TypeError)):
        records(payload)


def test_column_order_is_taken_from_actual_payload():
    payload = fixture("values")
    payload[0]["columns"] = "Value,Quality Code,Timestamp"
    payload[0]["data"] = [[v, q, t] for t, v, q in payload[0]["data"]]
    assert records(payload) == records()


def test_sparse_data_preserves_only_observed_slots():
    payload = fixture("values")
    payload[0]["data"].pop(1)
    payload[0]["rows"] = "2"
    assert len(records(payload)) == 2


@pytest.mark.parametrize(
    "delay,expected_requests",
    [("2", 2), ("86400", 1), ("Wed, 21 Oct 2026 07:28:00 GMT", 1)],
)
def test_rate_limits_are_bounded(delay, expected_requests):
    sleeps = []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(429, headers={"Retry-After": delay})
        )
    ) as http:
        api = c.KiWIS(http, sleep=sleeps.append)
        with pytest.raises(RuntimeError):
            api.get("getTimeseriesValues")
        assert api.requests == expected_requests
        assert sleeps == ([2] if expected_requests == 2 else [])


def test_transient_error_then_success_and_request_budget():
    requests = []

    def handle(request):
        requests.append(request)
        return (
            httpx.Response(503)
            if len(requests) == 1
            else httpx.Response(200, json=fixture("values"))
        )

    sleeps = []
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        api = c.KiWIS(http, sleep=sleeps.append)
        assert api.get("getTimeseriesValues") == fixture("values")
        assert requests[1].url.params["format"] == "json"
        assert sleeps == [2]
        api.requests = 8
        with pytest.raises(RuntimeError):
            api.get("getTimeseriesValues")
        assert len(requests) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"type": "error", "code": "TooManyResults"}),
        httpx.Response(403),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, content=b" " * (c.MAX_BYTES + 1)),
    ],
)
def test_upstream_error_or_oversized_body_is_not_data(response):
    with httpx.Client(transport=httpx.MockTransport(lambda r: response)) as http:
        api = c.KiWIS(http, sleep=lambda _: pytest.fail("must not retry"))
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            api.get("getTimeseriesValues")
        assert api.requests == 1


class AtomicStorage:
    """Use the pinned SDK's request schema; fake only server-side atomic commit."""

    def __init__(self):
        self.meta = client().meta
        self.objects = {}
        self.lock = threading.Lock()

    def put_object(self, **params):
        validate_parameters(
            params, self.meta.service_model.operation_model("PutObject").input_shape
        )
        assert params["IfNoneMatch"] == "*"
        key = params["Key"]
        with self.lock:
            if key in self.objects:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            self.objects[key] = deepcopy(params)
        return {"ETag": '"test"'}

    def head_object(self, **params):
        validate_parameters(
            params, self.meta.service_model.operation_model("HeadObject").input_shape
        )
        return {"Metadata": self.objects[params["Key"]]["Metadata"]}


def test_parquet_schema_utc_partition_and_readback():
    batches = c.object_batches(records(), "collectors/sepa/test")
    assert len(batches) == 2
    assert "/date=2021-10-22/station=133171/" in batches[0][0]
    assert "/date=2021-10-23/station=133171/" in batches[1][0]
    for _, _, rows in batches:
        table = pq.ParquetFile(BytesIO(c.parquet_bytes(rows))).read()
        assert str(table.schema.field("observed_at").type) == "timestamp[us, tz=UTC]"
        assert table.to_pylist()[0]["ingested_at"] == INGESTED
        assert table.schema.metadata[b"attribution"].startswith(b"Contains SEPA")
        assert table.to_pylist()[0]["quality_code"] == 50
        assert table.to_pylist()[0]["schema_version"] == 1


def test_missing_only_parquet_uses_explicit_float_schema():
    payload = fixture("values")
    for reading in payload[0]["data"]:
        reading[1] = None
    table = pq.ParquetFile(BytesIO(c.parquet_bytes(records(payload)))).read()
    assert str(table.schema.field("value").type) == "double"
    assert table.to_pylist()[0]["missing"] is True


def test_duplicate_retries_concurrent_creation_and_corrected_readings_preserve_objects():
    storage = AtomicStorage()
    first = records()
    assert c.write_batches(storage, "stars", first, "collectors/sepa") == (2, 0)
    before = deepcopy(storage.objects)
    later = records(ingested=INGESTED + timedelta(hours=1))
    assert c.write_batches(storage, "stars", later, "collectors/sepa") == (0, 2)
    assert storage.objects == before
    corrected = fixture("values")
    corrected[0]["data"][0][1] = 0.99
    revised = records(corrected)
    assert revised[0]["revision_id"] != first[0]["revision_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: c.write_batches(storage, "stars", revised, "collectors/sepa"),
                range(4),
            )
        )
    assert sum(created for created, _ in results) == 1
    assert len(storage.objects) == 3
    assert all(storage.objects[key] == value for key, value in before.items())
    # Readers deduplicate overlapping windows by revision_id, retaining both versions.
    all_rows = [
        row
        for obj in storage.objects.values()
        for row in pq.ParquetFile(BytesIO(obj["Body"])).read().to_pylist()
    ]
    assert len({row["revision_id"] for row in all_rows}) == 4


def test_real_sdk_conditional_put_and_head_interface():
    s3 = client()
    rows = records()[:1]
    key, digest, _ = c.object_batches(rows, "collectors/sepa")[0]
    meta = {"sepa-content-sha256": digest, "schema-version": "1"}
    with Stubber(s3) as stub:
        params = dict(
            Bucket="stars",
            Key=key,
            Body=ANY,
            ContentType="application/vnd.apache.parquet",
            Metadata=meta,
            IfNoneMatch="*",
        )
        stub.add_response("put_object", {"ETag": '"test"'}, params)
        stub.add_client_error(
            "put_object",
            "PreconditionFailed",
            http_status_code=412,
            expected_params=params,
        )
        stub.add_response(
            "head_object", {"Metadata": meta}, {"Bucket": "stars", "Key": key}
        )
        assert c.write_batches(s3, "stars", rows, "collectors/sepa") == (1, 0)
        assert c.write_batches(s3, "stars", rows, "collectors/sepa") == (0, 1)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "code,status",
    [
        ("ConditionalRequestConflict", 409),
        ("NotImplemented", 501),
        ("AccessDenied", 403),
    ],
)
def test_storage_failures_never_fall_back_to_unconditional_put(code, status):
    s3 = client()
    with Stubber(s3) as stub:
        stub.add_client_error("put_object", code, http_status_code=status)
        with pytest.raises(ClientError):
            c.write_batches(s3, "stars", records(), "collectors/sepa")
        stub.assert_no_pending_responses()


def test_conflicting_object_fails_visibly():
    storage = AtomicStorage()
    c.write_batches(storage, "stars", records(), "collectors/sepa")
    next(iter(storage.objects.values()))["Metadata"] = {}
    with pytest.raises(RuntimeError, match="conflict"):
        c.write_batches(storage, "stars", records(), "collectors/sepa")


@pytest.mark.parametrize(
    "prefix",
    [
        "stars",
        "collectors/sepa/../grid",
        "collectors/sepa/",
        "collectors/sepab",
        "/collectors/sepa",
    ],
)
def test_prefix_cannot_alias_existing_snapshots(prefix):
    with pytest.raises(ValueError):
        c.object_batches(records(), prefix)


def test_disabled_cli_and_handler_do_not_construct_clients(monkeypatch):
    from app.jobs_main import app

    monkeypatch.setenv("SEPA_ENABLED", "false")
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: pytest.fail("disabled network client")
    )
    monkeypatch.setattr(
        boto3, "client", lambda *a, **kw: pytest.fail("disabled storage client")
    )
    c.run()
    result = CliRunner().invoke(app, ["sepa-collect"])
    assert result.exit_code == 0
    assert "disabled; requests=0 objects=0" in result.output


def test_real_executable_disabled_path():
    # Fresh process exercises __main__, including the pre-CLI telemetry guard.
    path = Path(__file__).parents[1] / "app" / "jobs_main.py"
    env = dict(
        os.environ,
        SEPA_ENABLED="false",
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://127.0.0.1:1",
    )
    result = subprocess.run(
        [sys.executable, str(path), "sepa-collect"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SEPA disabled; requests=0 objects=0"
    assert "OpenTelemetry" not in result.stderr


@pytest.mark.parametrize("bad_second", [False, True])
def test_complete_job_wires_documented_requests_and_bounded_storage(
    monkeypatch, bad_second
):
    from core import s3 as shared_s3

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return END

    monkeypatch.setattr(c, "datetime", Clock)
    for key, value in {
        "SEPA_ENABLED": "true",
        "SEPA_STATION_NUMBERS": "133171,9514",
        "SEAWEEDFS_S3_ENDPOINT": "https://" + "a" * 32 + ".r2.cloudflarestorage.com",
        "STARS_GRID_S3_BUCKET": "stars",
        "S3_ACCESS_KEY_ID": "test",
        "S3_SECRET_ACCESS_KEY": "test",
    }.items():
        monkeypatch.setenv(key, value)
    calls = []

    def respond(request):
        calls.append(request)
        params = request.url.params
        assert params["service"] == "kisters"
        assert params["format"] == "json"
        if params["request"] == "getTimeseriesList":
            assert params["returnfields"] == c.SERIES_FIELDS
            return httpx.Response(200, json=fixture("series"))
        if params["request"] == "getStationList":
            return httpx.Response(200, json=fixture("stations"))
        assert params["request"] == "getTimeseriesValues"
        assert params["md_returnfields"] == c.VALUE_METADATA
        assert params["returnfields"] == "Timestamp,Value,Quality Code"
        assert params["timezone"] == "UTC"
        payload = fixture(
            "values" if params["ts_id"] == "54076010" else "values_norham"
        )
        if bad_second and params["ts_id"] == "61557010":
            payload[0]["ts_unitsymbol"] = "mm"
        return httpx.Response(200, json=payload)

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw),
    )
    storage = AtomicStorage()
    configs = []

    def make_storage(endpoint, *, config):
        configs.append(config)
        assert config.connect_timeout == 5
        assert config.read_timeout == 10
        assert config.retries["total_max_attempts"] == 2
        return storage

    monkeypatch.setattr(shared_s3, "path_style_client", make_storage)
    if bad_second:
        with pytest.raises(ValueError, match="metadata"):
            c.run()
        assert not configs
        assert not storage.objects
    else:
        c.run()
        assert len(configs) == 1
        assert len(storage.objects) == 4
    assert len(calls) == 4


def test_source_quality_correction_appends_new_identity():
    payload = fixture("values")
    payload[0]["data"][0][2] = 140
    before, after = records(), records(payload)
    assert after[0]["revision_id"] != before[0]["revision_id"]
    assert after[0]["provisional"] is True
    assert (
        c.object_batches(after, "collectors/sepa")[0][0]
        != c.object_batches(before, "collectors/sepa")[0][0]
    )


def test_transport_failures_stop_after_one_retry():
    sleeps = []

    def fail(request):
        raise httpx.ReadTimeout("fixture timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as http:
        api = c.KiWIS(http, sleep=sleeps.append)
        with pytest.raises(httpx.ReadTimeout):
            api.get("getTimeseriesValues")
        assert api.requests == 2
        assert sleeps == [2]
