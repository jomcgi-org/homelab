"""Shared backup checker and advisory consumer contract, without live I/O."""

import asyncio
import copy
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory import ops_health

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
TARGETS = [
    {"namespace": "monolith", "cluster": "monolith-pg"},
    {"namespace": "authentik", "cluster": "authentik-pg"},
    {"namespace": "mcp", "cluster": "context-forge-pg"},
]
LOOM = [{"namespace": "loom", "cluster": "loom-pg"}]


def snapshot(age=60, *, elapsed=0, backup=True, export=True, loom=False):
    observed = NOW - timedelta(seconds=elapsed)
    return {
        "version": 1,
        "targets": [
            dict(
                target,
                status="observed",
                export_ok=export,
                observed_at=observed.isoformat(),
                age_seconds=age,
                has_successful_backup=backup,
                last_successful_backup=(observed - timedelta(seconds=age)).isoformat()
                if backup
                else None,
            )
            for target in TARGETS + (LOOM if loom else [])
        ],
    }


def evaluate(document):
    return ops_health.evaluate_cnpg_backups(document, TARGETS, LOOM, NOW)


@pytest.mark.parametrize("age,ok", [(0, True), (129600, True), (129600.001, False)])
def test_exact_backup_threshold(age, ok):
    assert evaluate(snapshot(age))["ok"] is ok


def test_age_advances_between_checker_runs():
    assert evaluate(snapshot(129600, elapsed=1))["ok"] is False


@pytest.mark.parametrize("age", [0, 60, 129600, 129601])
def test_never_backed_up_is_unhealthy_regardless_of_creation_age(age):
    result = evaluate(snapshot(age, backup=False))
    assert result["ok"] is False
    for target in TARGETS:
        assert (
            f"{target['namespace']}/{target['cluster']}: never backed up"
            in result["detail"]
        )


@pytest.mark.parametrize(
    "elapsed,unknown", [(7200, False), (7200.001, True), (-1, True)]
)
def test_observation_freshness_boundary(elapsed, unknown):
    result = evaluate(snapshot(elapsed=elapsed))
    assert (result.get("status") == "unknown") is unknown


@pytest.mark.parametrize(
    "document",
    [None, {}, [], {"version": 2, "targets": []}, {"version": 1, "targets": []}],
)
def test_absent_or_invalid_observations_are_unknown(document):
    assert evaluate(document)["status"] == "unknown"


@pytest.mark.parametrize(
    "key,value",
    [
        ("observed_at", None),
        ("observed_at", "bad"),
        ("observed_at", "2026-10-01T12:00:00"),
        ("last_successful_backup", None),
        ("last_successful_backup", "bad"),
        ("last_successful_backup", (NOW + timedelta(seconds=1)).isoformat()),
        ("has_successful_backup", "true"),
        ("age_seconds", -1),
        ("age_seconds", float("nan")),
        ("age_seconds", float("inf")),
        ("age_seconds", True),
        ("age_seconds", 500),
        ("export_ok", None),
        ("status", "error"),
    ],
)
def test_one_invalid_target_cannot_report_healthy(key, value):
    document = snapshot()
    document["targets"][1][key] = value
    result = evaluate(document)
    assert result["ok"] is True and result["status"] == "unknown"


@pytest.mark.parametrize(
    "key",
    [
        "observed_at",
        "last_successful_backup",
        "has_successful_backup",
        "age_seconds",
        "export_ok",
    ],
)
def test_missing_fields_cannot_report_healthy(key):
    document = snapshot()
    del document["targets"][1][key]
    assert evaluate(document)["status"] == "unknown"


def test_known_failure_dominates_unknown_and_export_failure():
    document = snapshot(129601, export=False)
    document["targets"][0] = dict(
        TARGETS[0], status="error", error="kubernetes_read", export_ok=False
    )
    result = evaluate(document)
    assert result["ok"] is False
    assert "monolith/monolith-pg: observation unavailable" in result["detail"]
    assert "authentik/authentik-pg: last successful backup" in result["detail"]
    assert evaluate(snapshot(export=False))["status"] == "unknown"


def test_missing_required_or_duplicate_or_unexpected_target_is_unknown():
    missing = snapshot()
    missing["targets"].pop()
    assert evaluate(missing)["status"] == "unknown"
    duplicate = snapshot()
    duplicate["targets"].append(duplicate["targets"][0])
    assert evaluate(duplicate)["status"] == "unknown"
    unexpected = snapshot()
    unexpected["targets"][0]["cluster"] = "unconfigured"
    assert evaluate(unexpected)["status"] == "unknown"


def test_loom_is_optional_but_checked_when_producer_includes_it():
    assert evaluate(snapshot())["ok"] is True
    document = snapshot(loom=True)
    assert evaluate(document)["ok"] is True
    document["targets"][-1].update(
        has_successful_backup=False, last_successful_backup=None
    )
    assert evaluate(document)["ok"] is False
    assert "loom/loom-pg: never backed up" in evaluate(document)["detail"]


def test_actual_producer_snapshot_is_accepted_by_consumer(monkeypatch, tmp_path):
    path = (
        Path(__file__).resolve().parents[2]
        / "platform/otel-collector/files/backup_check.py"
    )
    spec = importlib.util.spec_from_file_location("checker_contract", path)
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)

    class FrozenTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(producer, "datetime", FrozenTime)
    (tmp_path / "token").write_text("test-token")
    monkeypatch.setattr(producer, "SERVICE_ACCOUNT", tmp_path)
    monkeypatch.setattr(producer.ssl, "create_default_context", lambda **kw: None)
    for key, value in {
        "CNPG_TARGETS": json.dumps(TARGETS),
        "KUBERNETES_SERVICE_HOST": "127.0.0.1",
        "KUBERNETES_SERVICE_PORT": "443",
        "OTLP_METRICS_ENDPOINT": "http://collector.invalid/metrics",
        "CNPG_STATUS_NAMESPACE": "otel-collector",
        "CNPG_STATUS_CONFIGMAP": "cnpg-backup-status",
    }.items():
        monkeypatch.setenv(key, value)
    recorded = []
    document = {
        "metadata": {"creationTimestamp": NOW.isoformat()},
        "status": {"lastSuccessfulBackup": (NOW - timedelta(seconds=60)).isoformat()},
    }

    def request(request, context=None):
        if request.get_method() == "GET":
            return copy.deepcopy(document)
        if request.get_method() == "PATCH":
            recorded.append(
                json.loads(json.loads(request.data)["data"]["observation.json"])
            )
        return {}

    monkeypatch.setattr(producer, "request_json", request)
    assert producer.main() == 0
    assert evaluate(recorded[-1])["ok"] is True
    document["status"] = {}
    assert producer.main() == 0
    assert evaluate(recorded[-1])["ok"] is False


def test_actual_reader_is_gated_cached_and_reads_only_the_named_configmap(monkeypatch):
    calls, data = [], [json.dumps(snapshot())]

    class Api:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def read_namespaced_config_map(self, name, namespace, **kwargs):
            calls.append((name, namespace, kwargs))
            if isinstance(data[0], Exception):
                raise data[0]
            return SimpleNamespace(data={"observation.json": data[0]})

    api = Api()
    monkeypatch.setitem(
        sys.modules,
        "kubernetes_asyncio",
        SimpleNamespace(
            config=SimpleNamespace(load_incluster_config=lambda: None),
            client=SimpleNamespace(ApiClient=lambda: api, CoreV1Api=lambda _: api),
        ),
    )
    monkeypatch.setattr(ops_health, "_now", lambda: NOW)
    ops_health.reset_caches()
    monkeypatch.delenv("CNPG_BACKUP_HEALTH_ENABLED", raising=False)
    assert asyncio.run(ops_health._cnpg_backups())["status"] == "unknown"
    assert calls == []
    for key, value in {
        "CNPG_BACKUP_HEALTH_ENABLED": "true",
        "CNPG_BACKUP_STATUS_NAMESPACE": "otel-collector",
        "CNPG_BACKUP_STATUS_CONFIGMAP": "cnpg-backup-status",
        "CNPG_BACKUP_TARGETS": json.dumps(TARGETS),
        "CNPG_BACKUP_OPTIONAL_TARGETS": json.dumps(LOOM),
    }.items():
        monkeypatch.setenv(key, value)

    async def cached():
        return (
            await ops_health.cnpg_backups_health(),
            await ops_health.cnpg_backups_health(),
        )

    assert all(r["ok"] for r in asyncio.run(cached()))
    assert calls == [("cnpg-backup-status", "otel-collector", {"_request_timeout": 5})]
    for bad in (
        "",
        "bad",
        "x" * 16385,
        RuntimeError("do not echo credentials"),
        json.dumps(snapshot(elapsed=7201)),
    ):
        data[0] = bad
        ops_health.reset_caches()
        result = asyncio.run(ops_health.cnpg_backups_health())
        assert result["status"] == "unknown"
        assert "credentials" not in result["detail"]
    data[0] = json.dumps(snapshot())
    ops_health.reset_caches()
    assert asyncio.run(ops_health.cnpg_backups_health())["ok"] is True
    ops_health.reset_caches()
