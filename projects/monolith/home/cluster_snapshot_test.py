"""Unit tests for home.cluster_snapshot: the background refresh (fail-soft per
section) and the read path (fresh hit, absent/stale/missing-table fallback),
plus the dashboard collectors that read it.
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
            {"firing": []},
            now,
        )
    )

    snap = cluster_snapshot.read_cluster_snapshot(session)
    assert snap is not None
    assert snap["health"]["scanned"] == 235
    assert snap["alerts"] == {"firing": []}
    assert snap["age_secs"] < 5


def test_read_parses_string_columns_from_sqlite_style_row():
    """SQLite fixtures hand JSON/timestamps back as strings; parse them."""
    now = datetime.now(timezone.utc)
    session = _fake_session_returning(
        (
            json.dumps({"healthy": False, "unhealthy": {"pods": [{"name": "x"}]}}),
            json.dumps({"firing": [{"name": "DiskFull"}]}),
            now.isoformat(),
        )
    )

    snap = cluster_snapshot.read_cluster_snapshot(session)
    assert snap is not None
    assert snap["health"]["healthy"] is False
    assert snap["alerts"]["firing"][0]["name"] == "DiskFull"


def test_read_returns_none_when_stale():
    old = datetime.now(timezone.utc) - timedelta(
        seconds=cluster_snapshot._STALE_FALLBACK_SECS + 60
    )
    session = _fake_session_returning(({"healthy": True}, {"firing": []}, old))

    # A wedged refresher must not leave the dashboard showing stale "healthy".
    assert cluster_snapshot.read_cluster_snapshot(session) is None


# ---------------------------------------------------------------------------
# refresh_cluster_snapshot: fail-soft per section
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_persists_both_sections_on_success():
    write = MagicMock()
    with (
        patch.object(
            cluster_snapshot,
            "scan_health_live",
            AsyncMock(return_value={"healthy": True, "scanned": 10, "unhealthy": {}}),
        ),
        patch.object(
            cluster_snapshot,
            "fetch_alerts_live",
            AsyncMock(return_value={"firing": []}),
        ),
        patch.object(cluster_snapshot, "_write_cluster_snapshot", write),
    ):
        await cluster_snapshot.refresh_cluster_snapshot()

    health, alerts = write.call_args.args
    assert health["scanned"] == 10
    assert alerts == {"firing": []}


@pytest.mark.asyncio
async def test_refresh_stores_error_marker_for_failing_health_but_keeps_alerts():
    write = MagicMock()
    with (
        patch.object(
            cluster_snapshot,
            "scan_health_live",
            AsyncMock(side_effect=RuntimeError("k8s down")),
        ),
        patch.object(
            cluster_snapshot,
            "fetch_alerts_live",
            AsyncMock(return_value={"firing": [{"name": "DiskFull"}]}),
        ),
        patch.object(cluster_snapshot, "_write_cluster_snapshot", write),
    ):
        await cluster_snapshot.refresh_cluster_snapshot()

    health, alerts = write.call_args.args
    assert health == {"error": "k8s down"}
    assert alerts["firing"][0]["name"] == "DiskFull"


# ---------------------------------------------------------------------------
# dashboard collectors read the snapshot, fall back to live when it is absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_health_uses_snapshot_and_adds_freshness():
    snap = {
        "health": {"healthy": True, "scanned": 42, "unhealthy": {}},
        "alerts": {"firing": []},
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


@pytest.mark.asyncio
async def test_collect_alerts_uses_snapshot_when_present():
    snap = {
        "health": {"healthy": True},
        "alerts": {"firing": [{"name": "DiskFull"}]},
        "snapshot_at": "2026-07-12T20:00:00+00:00",
        "age_secs": 3.0,
    }
    live = AsyncMock()
    with (
        patch.object(cluster_snapshot, "read_cluster_snapshot", return_value=snap),
        patch.object(cluster_snapshot, "fetch_alerts_live", live),
    ):
        alerts = await dashboard._collect_alerts(MagicMock())

    assert alerts["firing"][0]["name"] == "DiskFull"
    live.assert_not_called()


@pytest.mark.asyncio
async def test_collect_alerts_falls_back_to_live_when_no_snapshot():
    live = AsyncMock(return_value={"firing": [{"name": "Latency"}]})
    with (
        patch.object(cluster_snapshot, "read_cluster_snapshot", return_value=None),
        patch.object(cluster_snapshot, "fetch_alerts_live", live),
    ):
        alerts = await dashboard._collect_alerts(MagicMock())

    assert alerts["firing"][0]["name"] == "Latency"
    live.assert_awaited_once()
