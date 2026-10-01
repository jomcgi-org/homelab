"""Unit tests for the stdlib-only CNPG backup checker mounted by Helm."""

import importlib.util
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

spec = importlib.util.spec_from_file_location(
    "backup_check", Path(__file__).parent / "files/backup_check.py"
)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
CLUSTER = {
    "metadata": {"creationTimestamp": "2026-09-29T00:00:00Z"},
    "status": {"lastSuccessfulBackup": "2026-10-01T00:00:00Z"},
}


def test_age_uses_the_last_successful_backup():
    assert check.backup_age(CLUSTER, NOW) == (43200, True)


@pytest.mark.parametrize(
    "status", [{}, {"lastSuccessfulBackup": None}, {"lastSuccessfulBackup": ""}]
)
def test_never_backed_up_cluster_ages_from_creation(status):
    document = {"metadata": CLUSTER["metadata"], "status": status}
    assert check.backup_age(document, NOW) == (216000, False)
    assert check.backup_age({"metadata": CLUSTER["metadata"]}, NOW) == (216000, False)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-01T00:00:00Z",
        "2026-10-01T00:00:00.000000Z",
        "2026-10-01T02:00:00+02:00",
    ],
)
def test_timestamp_parsing(timestamp):
    assert check.parse_timestamp(timestamp) == datetime(
        2026, 10, 1, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("timestamp", ["bad", "2026-10-01T00:00:00"])
def test_invalid_or_naive_timestamps_fail(timestamp):
    with pytest.raises(ValueError):
        check.parse_timestamp(timestamp)


def test_future_timestamp_fails():
    with pytest.raises(ValueError, match="future"):
        check.backup_age(CLUSTER, datetime(2026, 9, 30, tzinfo=timezone.utc))


@pytest.mark.parametrize("has_backup", [True, False])
def test_exact_otlp_payload(has_backup):
    assert check.payload(
        "loom-pg", "loom", 43200.5, has_backup, 1790856000000000000
    ) == {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "cnpg-backup-check"},
                        }
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "cnpg-backup-check"},
                        "metrics": [
                            {
                                "name": "cnpg.backup.last_success_age_seconds",
                                "unit": "s",
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": "1790856000000000000",
                                            "asDouble": 43200.5,
                                            "attributes": [
                                                {
                                                    "key": "cnpg.cluster.name",
                                                    "value": {"stringValue": "loom-pg"},
                                                },
                                                {
                                                    "key": "k8s.namespace.name",
                                                    "value": {"stringValue": "loom"},
                                                },
                                                {
                                                    "key": "cnpg.backup.has_successful_backup",
                                                    "value": {"boolValue": has_backup},
                                                },
                                            ],
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }


@pytest.fixture
def client(monkeypatch, tmp_path):
    for key, value in {
        "CNPG_NAMESPACE": "loom",
        "CNPG_CLUSTER": "loom-pg",
        "KUBERNETES_SERVICE_HOST": "10.0.0.1",
        "KUBERNETES_SERVICE_PORT": "443",
        "OTLP_METRICS_ENDPOINT": "http://otel-collector.otel-collector.svc:4318/v1/metrics",
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "token").write_text("test-token\n")
    monkeypatch.setattr(check, "SERVICE_ACCOUNT", tmp_path)
    tls_context = object()

    def context(*, cafile):
        assert cafile == str(tmp_path / "ca.crt")
        return tls_context

    monkeypatch.setattr(check.ssl, "create_default_context", context)
    calls = []
    replies = [CLUSTER, {}]

    def open_request(request, **kwargs):
        calls.append((request, kwargs))
        result = replies[len(calls) - 1]
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(check, "urlopen", open_request)
    return calls, replies, tls_context


def test_api_auth_ca_and_export_destination(client):
    calls, _, context = client
    assert check.main() == 0
    (api, api_options), (export, export_options) = calls
    assert (
        api.full_url
        == "https://10.0.0.1:443/apis/postgresql.cnpg.io/v1/namespaces/loom/clusters/loom-pg"
    )
    assert api.get_method() == "GET"
    assert api.headers == {"Authorization": "Bearer test-token"}
    assert api_options == {"context": context, "timeout": 20}
    assert export.full_url == "http://otel-collector.otel-collector.svc:4318/v1/metrics"
    assert export.get_method() == "POST"
    assert export.headers == {"Content-type": "application/json"}
    assert export_options == {"timeout": 20}
    assert (
        json.loads(export.data)["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0][
            "name"
        ]
        == check.METRIC
    )


def test_ipv6_api_host_is_bracketed(client, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fd00::1")
    assert check.main() == 0
    assert client[0][0][0].full_url.startswith("https://[fd00::1]:443/")


@pytest.mark.parametrize("stage", [0, 1], ids=["api", "export"])
@pytest.mark.parametrize(
    "failure",
    [
        URLError("connection refused"),
        HTTPError("https://example.invalid", 403, "forbidden", {}, None),
    ],
)
def test_api_and_export_failure_return_nonzero(client, capsys, stage, failure):
    client[1][stage] = failure
    assert check.main() == 1
    assert len(client[0]) == stage + 1
    assert "backup check failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "partial",
    [
        {"rejectedDataPoints": "1"},
        {"rejectedDataPoints": 1},
        {"errorMessage": "rejected"},
    ],
)
def test_otlp_partial_failure_returns_nonzero(client, partial):
    client[1][1] = {"partialSuccess": partial}
    assert check.main() == 1


def test_invalid_cluster_timestamp_does_not_export(client):
    client[1][0] = {"metadata": {"creationTimestamp": "invalid"}}
    assert check.main() == 1
    assert len(client[0]) == 1
