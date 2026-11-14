"""Execute the chart's unchanged redaction processor in the pinned OTTL engine."""

import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest
import yaml

from redaction_fixtures import CREDENTIAL_CASES, ORDINARY_LINES


def _value(value):
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_value(v) for v in value]}}
    return {"kvlistValue": {"values": _attributes(value)}}


def _attributes(values):
    return [{"key": key, "value": _value(value)} for key, value in values.items()]


def _decode(value):
    if "intValue" in value:
        return int(value["intValue"])
    return value["stringValue"]


def _captured(path):
    records = {}
    if path.exists():
        # A concurrent exporter write may leave the last line incomplete.
        for line in path.read_text().splitlines(keepends=True):
            if not line.endswith("\n"):
                continue
            for resource in json.loads(line).get("resourceLogs", []):
                for scope in resource.get("scopeLogs", []):
                    for record in scope.get("logRecords", []):
                        attrs = {
                            a["key"]: _decode(a["value"]) for a in record["attributes"]
                        }
                        record_id = attrs.pop("fixture_id")
                        assert record_id not in records, f"duplicate {record_id}"
                        records[record_id] = (_decode(record["body"]), attrs)
    return records


def _capture(binary, processor, records, tmp_path):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    capture = tmp_path / "records.json"
    config = {
        "receivers": {
            "otlp": {"protocols": {"http": {"endpoint": f"127.0.0.1:{port}"}}}
        },
        "processors": {"transform/redact": processor},
        "exporters": {"file": {"path": str(capture), "flush_interval": "100ms"}},
        "service": {
            "telemetry": {"metrics": {"level": "none"}},
            "pipelines": {
                "logs": {
                    "receivers": ["otlp"],
                    "processors": ["transform/redact"],
                    "exporters": ["file"],
                }
            },
        },
    }
    config_path = tmp_path / "collector.yaml"
    # YAML serialization preserves the processor's statement strings, including
    # where clauses and $$ escapes. Only the collector resolves interpolation.
    config_path.write_text(yaml.safe_dump(config))
    diagnostics = tmp_path / "collector.log"
    try:
        with diagnostics.open("w") as log:
            process = subprocess.Popen(
                [binary, "--config", str(config_path)], stdout=log, stderr=log
            )
            try:
                deadline = time.monotonic() + 20
                while True:
                    assert process.poll() is None, "collector exited during readiness"
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        assert time.monotonic() < deadline, (
                            "collector readiness timed out"
                        )
                        time.sleep(0.05)
                payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": records}]}]}
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/logs",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                # Never consult proxy environment variables for local fixtures.
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    request, timeout=5
                ) as response:
                    result = json.load(response)
                    assert not result.get("partialSuccess"), result
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None, "collector exited during capture"
                    captured = _captured(capture)
                    if len(captured) >= len(records):
                        return captured
                    assert time.monotonic() < deadline, (
                        f"capture timed out: {len(captured)}/{len(records)} records"
                    )
                    time.sleep(0.05)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                assert process.returncode == 0, (
                    f"collector teardown exit {process.returncode}"
                )
    except Exception as error:
        raise AssertionError(
            f"{error}\nCollector diagnostics:\n{diagnostics.read_text()}"
        ) from error


@pytest.fixture(scope="module")
def captured(tmp_path_factory):
    binary = str(Path(os.environ["OTELCOL_BIN"]).resolve())
    version = subprocess.run(
        [binary, "--version"], check=True, capture_output=True, text=True, timeout=10
    )
    values = yaml.safe_load(
        (Path(__file__).resolve().parent / "values.yaml").read_text()
    )
    assert version.stdout.strip().split()[-1] == values["image"]["tag"], version.stdout
    rendered = subprocess.run(
        [
            os.environ["HELM_BIN"],
            "template",
            "runtime-test",
            str(Path(__file__).resolve().parent),
            "--set",
            "logs.enabled=true",
            "--set",
            "logs.redact=true",
            "--set",
            "logs.namespaces[0]=runtime-test",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert rendered.returncode == 0, rendered.stderr
    processors = [
        yaml.safe_load(doc["data"]["collector.yaml"])["processors"]["transform/redact"]
        for doc in yaml.safe_load_all(rendered.stdout)
        if doc
        and doc["kind"] == "ConfigMap"
        and "collector.yaml" in doc.get("data", {})
        and "transform/redact" in doc["data"]["collector.yaml"]
    ]
    (processor,) = processors
    fixtures = [
        (f"credential-{i}", body, {}) for i, (body, _) in enumerate(CREDENTIAL_CASES)
    ]
    fixtures += [(f"ordinary-{i}", body, {}) for i, body in enumerate(ORDINARY_LINES)]
    fixtures += [
        ("context", "postgres://app:pw@db:5432/app?sslmode=require", {}),
        ("escaped", 'payload {\\"user\\":\\"joe\\",\\"password\\":\\"x-y\\"}', {}),
        (
            "nested",
            "nested record",
            {
                "record": {
                    "message": "c9 password=nestedpw rejected",
                    "user_name": "app",
                    "password": "plainnested",
                },
                "headers": {
                    "Authorization": ["Basic dXNlcjpwYXNz"],
                    "Cookie": ["session=abc123"],
                    "X-Api-Key": ["k3y"],
                    "Accept": ["*/*"],
                },
                "client_secret": "cs",
                "detail": "retry with password: pw456",
                "secretName": "db-credentials",
                "max_tokens": 4096,
            },
        ),
        ("non-string", 42, {"count": 7}),
    ]
    records = [
        {"body": _value(body), "attributes": _attributes({"fixture_id": name, **attrs})}
        for name, body, attrs in fixtures
    ]
    result = _capture(binary, processor, records, tmp_path_factory.mktemp("collector"))
    assert set(result) == {name for name, _, _ in fixtures}
    return result


def test_runtime_masks_body_credentials(captured):
    for i, (_, secret) in enumerate(CREDENTIAL_CASES):
        body, attrs = captured[f"credential-{i}"]
        assert secret not in json.dumps([body, attrs]), f"credential-{i}: {body}"
        assert "[REDACTED" in body


def test_runtime_preserves_context(captured):
    for i, body in enumerate(ORDINARY_LINES):
        assert captured[f"ordinary-{i}"] == (body, {})
    assert captured["context"] == (
        "postgres://app:[REDACTED]@db:5432/app?sslmode=require",
        {},
    )
    assert captured["escaped"] == (
        'payload {\\"user\\":\\"joe\\",\\"password\\":\\"[REDACTED]\\"}',
        {},
    )
    assert captured["non-string"] == (42, {"count": 7})


def test_runtime_masks_and_deletes_nested_attributes(captured):
    body, attrs = captured["nested"]
    for secret in (
        "nestedpw",
        "plainnested",
        "dXNlcjpwYXNz",
        "abc123",
        "k3y",
        "cs",
        "pw456",
    ):
        assert secret not in json.dumps([body, attrs]), (
            f"attribute credential {secret}: {attrs}"
        )
    assert attrs == {
        "record.message": "c9 password=[REDACTED] rejected",
        "record.user_name": "app",
        "headers.Accept.0": "*/*",
        "detail": "retry with password: [REDACTED]",
        "secretName": "db-credentials",
        "max_tokens": 4096,
    }
