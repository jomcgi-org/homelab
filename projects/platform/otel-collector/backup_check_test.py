"""Unit tests for the stdlib-only CNPG backup checker mounted by Helm."""

import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
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


@pytest.mark.parametrize("timestamp", [False, 0, {}, [], 1])
def test_malformed_backup_field_is_invalid_not_never_backed_up(timestamp):
    with pytest.raises(ValueError):
        check.backup_age(dict(CLUSTER, status={"lastSuccessfulBackup": timestamp}), NOW)


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
        "CNPG_TARGETS": json.dumps([{"namespace": "loom", "cluster": "loom-pg"}]),
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

    class FrozenTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(check, "datetime", FrozenTime)
    monkeypatch.delenv("CNPG_STATUS_CONFIGMAP", raising=False)

    def open_request(request, context=None):
        kwargs = {"timeout": check.REQUEST_TIMEOUT_S}
        if context is not None:
            kwargs["context"] = context
        calls.append((request, kwargs))
        result = replies[len(calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(check, "request_json", open_request)
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
    assert api_options == {"context": context, "timeout": 10}
    assert export.full_url == "http://otel-collector.otel-collector.svc:4318/v1/metrics"
    assert export.get_method() == "POST"
    assert export.headers == {"Content-type": "application/json"}
    assert export_options == {"timeout": 10}
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


TARGETS = [
    {"namespace": "monolith", "cluster": "monolith-pg"},
    {"namespace": "authentik", "cluster": "authentik-pg"},
    {"namespace": "mcp", "cluster": "context-forge-pg"},
    {"namespace": "loom", "cluster": "loom-pg"},
]


def _handoff(monkeypatch):
    monkeypatch.setenv("CNPG_STATUS_CONFIGMAP", "cnpg-backup-status")
    monkeypatch.setenv("CNPG_STATUS_NAMESPACE", "otel-collector")


def _snapshot(calls):
    patch = calls[-1][0]
    assert patch.get_method() == "PATCH"
    return json.loads(json.loads(patch.data)["data"]["observation.json"])


def test_all_targets_export_and_handoff_the_same_observation(client, monkeypatch):
    calls, replies, context = client
    monkeypatch.setenv("CNPG_TARGETS", json.dumps(TARGETS))
    _handoff(monkeypatch)
    replies[:] = [CLUSTER, {}] * 4 + [{}]
    assert check.main() == 0
    snapshot = _snapshot(calls)
    assert snapshot["version"] == 1
    assert len(snapshot["targets"]) == 4
    for i, target in enumerate(TARGETS):
        row = snapshot["targets"][i]
        assert row == dict(target, status="observed", export_ok=True,
                           observed_at=NOW.isoformat(), age_seconds=43200,
                           has_successful_backup=True,
                           last_successful_backup="2026-10-01T00:00:00Z")
        get, post = calls[i * 2][0], calls[i * 2 + 1][0]
        assert get.full_url.endswith(f"/namespaces/{target['namespace']}/clusters/{target['cluster']}")
        point = json.loads(post.data)["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"][0]
        assert point["asDouble"] == row["age_seconds"]
        assert point["attributes"][0]["value"]["stringValue"] == row["cluster"]
        assert point["attributes"][1]["value"]["stringValue"] == row["namespace"]
        assert "Authorization" not in post.headers
    patch, options = calls[-1]
    assert patch.full_url == "https://10.0.0.1:443/api/v1/namespaces/otel-collector/configmaps/cnpg-backup-status"
    assert patch.headers["Authorization"] == "Bearer test-token"
    assert options == {"timeout": 10, "context": context}
    # Four sequential read/export pairs plus one patch: <=90s of request time,
    # leaving 30s in the existing 120s CronJob deadline for startup and parsing.
    assert len(calls) * check.REQUEST_TIMEOUT_S == 90


@pytest.mark.parametrize("failure", [URLError("test-token"), {"status": {"lastSuccessfulBackup": "bad"}}, {"status": {"lastSuccessfulBackup": "2999-01-01T00:00:00Z"}}, {}])
def test_failed_middle_target_does_not_block_other_targets(client, monkeypatch, capsys, failure):
    calls, replies, _ = client
    monkeypatch.setenv("CNPG_TARGETS", json.dumps(TARGETS[:3]))
    _handoff(monkeypatch)
    replies[:] = [CLUSTER, {}, failure, CLUSTER, {}, {}]
    assert check.main() == 1
    rows = _snapshot(calls)["targets"]
    assert [r["status"] for r in rows] == ["observed", "error", "observed"]
    assert rows[1]["export_ok"] is False
    assert "test-token" not in capsys.readouterr().err


@pytest.mark.parametrize("failure", [URLError("test-token"), {"partialSuccess": {"rejectedDataPoints": 1}}, {"partialSuccess": {"errorMessage": "test-token"}}, {"partialSuccess": None}])
def test_export_failure_is_handed_off_then_can_recover(client, monkeypatch, failure):
    calls, replies, _ = client
    _handoff(monkeypatch)
    replies[:] = [CLUSTER, failure, {}]
    assert check.main() == 1
    row = _snapshot(calls)["targets"][0]
    assert row["status"] == "observed" and row["export_ok"] is False
    assert row["error"] == "metric_export"
    calls.clear()
    replies[:] = [CLUSTER, {}, {}]
    assert check.main() == 0
    assert _snapshot(calls)["targets"][0]["export_ok"] is True


def test_handoff_failure_is_nonzero_and_redacted(client, monkeypatch, capsys):
    _handoff(monkeypatch)
    client[1][:] = [CLUSTER, {}, URLError("test-token")]
    assert check.main() == 1
    assert "status handoff unavailable" in capsys.readouterr().err
    client[0].clear()
    client[1][:] = [CLUSTER, {}, {}]
    assert check.main() == 0


def test_new_never_backed_up_cluster_is_not_successful_evidence(client, monkeypatch):
    _handoff(monkeypatch)
    client[1][:] = [{"metadata": {"creationTimestamp": (NOW - timedelta(minutes=1)).isoformat()}}, {}, {}]
    assert check.main() == 0
    row = _snapshot(client[0])["targets"][0]
    assert row["has_successful_backup"] is False
    assert row["last_successful_backup"] is None
    assert row["age_seconds"] == 60


@pytest.mark.parametrize("targets", [[], TARGETS + TARGETS[:1], TARGETS[:1] * 2, [{"namespace": "../secrets", "cluster": "x"}], {}, [None]])
def test_invalid_target_configuration_does_not_make_requests(client, monkeypatch, targets):
    monkeypatch.setenv("CNPG_TARGETS", json.dumps(targets))
    assert check.main() == 1
    assert client[0] == []


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b"x" * (check.MAX_RESPONSE_BYTES + 1)])
def test_bounded_json_transport_rejects_bad_responses(monkeypatch, raw):
    class Opener:
        def open(self, request, timeout):
            assert timeout == 10
            return io.BytesIO(raw)

    monkeypatch.setattr(check, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError):
        check.request_json(check.Request("https://example.invalid"))
    assert check.signal.getitimer(check.signal.ITIMER_REAL)[0] == 0


def test_request_wall_deadline_and_redirect_refusal():
    with pytest.raises(TimeoutError):
        check._timeout(None, None)
    with pytest.raises(RuntimeError, match="redirect refused"):
        check._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid")
