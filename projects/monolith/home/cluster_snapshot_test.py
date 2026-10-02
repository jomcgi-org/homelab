"""Unit tests for home.cluster_snapshot: the fail-soft background refresh,
the read path (fresh hit, absent/stale/missing-table fallback), and the
dashboard health collector.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from home import cluster_snapshot, dashboard


def _fake_session_returning(row):
    """A session whose execute(...).first() returns the given row tuple/None."""
    session = MagicMock()
    session.execute.return_value.first.return_value = row
    return session


def _empty_resources():
    return {kind: [] for kind in cluster_snapshot._HEALTH_KINDS}


# ---------------------------------------------------------------------------
# read_cluster_snapshot
# ---------------------------------------------------------------------------


def test_read_returns_none_when_table_missing():
    session = MagicMock()
    session.execute.side_effect = OperationalError(
        "SELECT ...", {}, Exception("no such table: home.cluster_snapshot")
    )

    assert cluster_snapshot.read_cluster_snapshot(session) is None
    session.rollback.assert_called_once()


def test_read_returns_none_when_row_absent():
    session = _fake_session_returning(None)
    assert cluster_snapshot.read_cluster_snapshot(session) is None


def test_read_returns_parsed_snapshot_when_fresh():
    now = datetime.now(timezone.utc)
    session = _fake_session_returning(
        (
            {"healthy": True, "scanned": 235, "unhealthy": {}},
            {},
            now,
        )
    )

    snap = cluster_snapshot.read_cluster_snapshot(session)
    assert snap is not None
    assert snap["health"]["scanned"] == 235
    assert snap["alerts"] == {}
    assert snap["age_secs"] < 5


def test_read_parses_string_columns_from_sqlite_style_row():
    """SQLite fixtures hand JSON/timestamps back as strings; parse them."""
    now = datetime.now(timezone.utc)
    session = _fake_session_returning(
        (
            json.dumps({"healthy": False, "unhealthy": {"pods": [{"name": "x"}]}}),
            json.dumps({}),
            now.isoformat(),
        )
    )

    snap = cluster_snapshot.read_cluster_snapshot(session)
    assert snap is not None
    assert snap["health"]["healthy"] is False
    assert snap["alerts"] == {}


def test_read_returns_none_when_stale():
    old = datetime.now(timezone.utc) - timedelta(
        seconds=cluster_snapshot._STALE_FALLBACK_SECS + 60
    )
    session = _fake_session_returning(({"healthy": True}, {}, old))

    # A wedged refresher must not leave the dashboard showing stale "healthy".
    assert cluster_snapshot.read_cluster_snapshot(session) is None


# ---------------------------------------------------------------------------
# refresh_cluster_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_persists_health_and_empty_alerts_on_success():
    write = MagicMock()
    agent_write = MagicMock()
    resources = _empty_resources()
    resources["pods"] = [{"metadata": {"name": str(i)}} for i in range(10)]
    with (
        patch.object(
            cluster_snapshot,
            "scan_cluster_resources_live",
            AsyncMock(return_value=(resources, {})),
        ),
        patch.object(cluster_snapshot, "_write_cluster_snapshot", write),
        patch.object(cluster_snapshot, "_write_agent_cluster_snapshot", agent_write),
    ):
        await cluster_snapshot.refresh_cluster_snapshot()

    health, alerts = write.call_args.args
    assert health["scanned"] == 10
    assert alerts == {}
    assert agent_write.call_args.args[0]["complete"] is True
    assert agent_write.call_args.args[0]["scanned"]["pods"] == 10


@pytest.mark.asyncio
async def test_refresh_stores_error_marker_for_failing_health():
    write = MagicMock()
    agent_write = MagicMock()
    with (
        patch.object(
            cluster_snapshot,
            "scan_cluster_resources_live",
            AsyncMock(side_effect=RuntimeError("k8s down")),
        ),
        patch.object(cluster_snapshot, "_write_cluster_snapshot", write),
        patch.object(cluster_snapshot, "_write_agent_cluster_snapshot", agent_write),
    ):
        await cluster_snapshot.refresh_cluster_snapshot()

    health, alerts = write.call_args.args
    assert health == {"error": "k8s down"}
    assert alerts == {}
    assert agent_write.call_args.args[0] == {
        "schema_version": 1,
        "complete": False,
        "errors": {"scan": "RuntimeError: k8s down"},
        "scanned": {},
        "applications": [],
        "unhealthy": {},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_writer", ["dashboard", "agent"])
async def test_refresh_attempts_both_writes_independently(failing_writer):
    dashboard_write = MagicMock(
        side_effect=RuntimeError("dashboard failed")
        if failing_writer == "dashboard"
        else None
    )
    agent_write = MagicMock(
        side_effect=RuntimeError("agent failed") if failing_writer == "agent" else None
    )
    scan = AsyncMock(return_value=(_empty_resources(), {}))
    with (
        patch.object(cluster_snapshot, "scan_cluster_resources_live", scan),
        patch.object(cluster_snapshot, "_write_cluster_snapshot", dashboard_write),
        patch.object(cluster_snapshot, "_write_agent_cluster_snapshot", agent_write),
    ):
        await cluster_snapshot.refresh_cluster_snapshot()
    scan.assert_awaited_once()
    dashboard_write.assert_called_once()
    agent_write.assert_called_once()


@pytest.mark.asyncio
async def test_scan_records_failure_and_keeps_dashboard_fail_soft():
    client = MagicMock()

    async def list_resources(kind):
        if kind == "pods":
            raise RuntimeError("unavailable " + "x" * 300)
        return []

    client.list_resources = AsyncMock(side_effect=list_resources)
    client.close = AsyncMock()
    with patch("cluster.api.KubernetesClient", return_value=client):
        resources, errors = await cluster_snapshot.scan_cluster_resources_live()
    assert client.list_resources.await_count == 5
    assert [call.args[0] for call in client.list_resources.call_args_list] == list(
        cluster_snapshot._HEALTH_KINDS
    )
    client.close.assert_awaited_once()
    assert resources["pods"] == []
    assert errors["pods"].startswith("RuntimeError: unavailable")
    assert len(errors["pods"]) == 200
    with patch.object(
        cluster_snapshot,
        "scan_cluster_resources_live",
        AsyncMock(return_value=(resources, errors)),
    ):
        assert await cluster_snapshot.scan_health_live() == {
            "healthy": True,
            "scanned": 0,
            "unhealthy": {},
        }
    payload = cluster_snapshot.build_agent_cluster_summary(resources, errors)
    assert payload["complete"] is False
    assert payload["errors"] == errors
    assert "pods" not in payload["scanned"]
    assert "pods" not in payload["unhealthy"]


def test_agent_summary_complete_for_clean_scan():
    assert cluster_snapshot.build_agent_cluster_summary(_empty_resources(), {}) == {
        "schema_version": 1,
        "complete": True,
        "errors": {},
        "scanned": {kind: 0 for kind in cluster_snapshot._HEALTH_KINDS},
        "applications": [],
        "unhealthy": {},
    }


def test_agent_summary_does_not_claim_missing_kinds_were_scanned():
    assert cluster_snapshot.build_agent_cluster_summary({}, {})["complete"] is False


@pytest.mark.parametrize(
    ("spec", "sync", "revision", "target"),
    [
        (
            {"sources": [{"targetRevision": "desired"}]},
            {"revisions": ["live"]},
            "live",
            "desired",
        ),
        (
            {"source": {"targetRevision": "desired"}},
            {"revision": "live"},
            "live",
            "desired",
        ),
        (
            {
                "source": {"targetRevision": "single"},
                "sources": [{"targetRevision": "multi"}],
            },
            {"revision": "single-live", "revisions": ["multi-live"]},
            "single-live",
            "single",
        ),
        ({"source": {"targetRevision": "desired"}}, {}, None, "desired"),
        ({}, {}, None, None),
    ],
)
def test_agent_summary_includes_healthy_applications(spec, sync, revision, target):
    resources = _empty_resources()
    resources["applications"] = [
        {
            "metadata": {"name": "app", "namespace": "argocd"},
            "spec": spec,
            "status": {
                "sync": {"status": "Synced", **sync},
                "health": {"status": "Healthy"},
            },
        }
    ]
    payload = cluster_snapshot.build_agent_cluster_summary(resources, {})
    assert payload["applications"] == [
        {
            "name": "app",
            "namespace": "argocd",
            "sync": "Synced",
            "health": "Healthy",
            "revision": revision,
            "target_revision": target,
        }
    ]
    assert payload["unhealthy"] == {}


def test_agent_summary_caps_sorted_applications_and_unhealthy_rows():
    resources = _empty_resources()
    resources["applications"] = [
        {
            "metadata": {"name": f"app-{i:04d}"},
            "status": {"health": {"status": "Degraded"}},
        }
        for i in reversed(range(503))
    ]
    resources["pods"] = [
        {"metadata": {"name": f"pod-{i}"}, "status": {"phase": "Failed"}}
        for i in range(102)
    ]
    payload = cluster_snapshot.build_agent_cluster_summary(resources, {})
    assert len(payload["applications"]) == 500
    assert payload["applications"][0]["name"] == "app-0000"
    assert payload["applications"][-1]["name"] == "app-0499"
    assert payload["applications_truncated"] == 3
    assert len(payload["unhealthy"]["applications"]) == 100
    assert len(payload["unhealthy"]["pods"]) == 100
    assert payload["unhealthy_truncated"] == {"applications": 403, "pods": 2}
    assert payload["scanned"]["applications"] == 503
    assert payload["scanned"]["pods"] == 102


def test_agent_summary_does_not_leak_manifests():
    resources = _empty_resources()
    metadata = {
        "name": "workload",
        "namespace": "monolith",
        "annotations": {"secret": "annotation-secret"},
        "labels": {"secret": "label-secret"},
    }
    env = [{"name": "SECRET", "value": "env-secret"}]
    resources["pods"] = [
        {
            "metadata": metadata,
            "spec": {"containers": [{"env": env}]},
            "status": {"phase": "Failed"},
        }
    ]
    resources["deployments"] = [
        {
            "metadata": metadata,
            "spec": {
                "replicas": 1,
                "template": {"spec": {"containers": [{"env": env}]}},
            },
            "status": {"readyReplicas": 0},
        }
    ]
    resources["secrets"] = [{"data": {"private": "secret-value"}}]
    payload = cluster_snapshot.build_agent_cluster_summary(resources, {})
    assert payload["unhealthy"]["pods"]
    assert payload["unhealthy"]["deployments"]
    serialized = json.dumps(payload)
    for forbidden in ("env", "annotations", "labels", "spec", "secret-value", "secret"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_refresh_excludes_failed_kind_from_agent_summary_only():
    resources = _empty_resources()
    dashboard_write, agent_write = MagicMock(), MagicMock()
    with (
        patch.object(
            cluster_snapshot,
            "scan_cluster_resources_live",
            AsyncMock(return_value=(resources, {"pods": "RuntimeError: unavailable"})),
        ),
        patch.object(cluster_snapshot, "_write_cluster_snapshot", dashboard_write),
        patch.object(cluster_snapshot, "_write_agent_cluster_snapshot", agent_write),
    ):
        await cluster_snapshot.refresh_cluster_snapshot()
    assert dashboard_write.call_args.args[0]["healthy"] is True
    assert agent_write.call_args.args[0]["complete"] is False
    assert "pods" not in agent_write.call_args.args[0]["scanned"]


# ---------------------------------------------------------------------------
# dashboard collectors read the snapshot, fall back to live when it is absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_health_uses_snapshot_and_adds_freshness():
    snap = {
        "health": {"healthy": True, "scanned": 42, "unhealthy": {}},
        "alerts": {},
        "snapshot_at": "2026-07-12T20:00:00+00:00",
        "age_secs": 3.0,
    }
    live = AsyncMock()
    with (
        patch.object(cluster_snapshot, "read_cluster_snapshot", return_value=snap),
        patch.object(cluster_snapshot, "scan_health_live", live),
    ):
        health = await dashboard._collect_health(MagicMock())

    assert health["scanned"] == 42
    assert health["snapshot_at"] == "2026-07-12T20:00:00+00:00"
    live.assert_not_called()  # never touches the live scan on a fresh hit


@pytest.mark.asyncio
async def test_collect_health_falls_back_to_live_scan_when_no_snapshot():
    live = AsyncMock(return_value={"healthy": True, "scanned": 7, "unhealthy": {}})
    with (
        patch.object(cluster_snapshot, "read_cluster_snapshot", return_value=None),
        patch.object(cluster_snapshot, "scan_health_live", live),
    ):
        health = await dashboard._collect_health(MagicMock())

    assert health["scanned"] == 7
    live.assert_awaited_once()
