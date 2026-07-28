"""Tests for GET /api/semgrep/perf (semgrep_scan/perf_router.py).

Uses an in-memory SQLite DB seeded with real rows and a minimal FastAPI app
that mounts only the perf router, mirroring the schema-stripping +
``app.dependency_overrides[get_session]`` pattern in ``ships/router_test.py``
and the SQLite fixture in ``perf_store_test.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from semgrep_scan.perf_router import router
from semgrep_scan.perf_store import ScanPerf

T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite can't span schemas, so strip the Postgres-only schema= overrides so
    # SQLModel.metadata.create_all() lands every table in the default schema.
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_empty_db_returns_empty_comparisons(client):
    resp = client.get("/api/semgrep/perf")
    assert resp.status_code == 200
    body = resp.json()
    assert body["comparisons"] == []
    assert body["counts"] == {"homelab": 0, "managed": 0}
    assert body["window_start"] is None
    assert body["aggregates"]["pr"]["pairs"] == 0
    assert body["aggregates"]["full"]["pairs"] == 0
    assert body["coverage_note"]


def test_matched_pair_returns_one_comparison_with_both_sides(session, client):
    session.add(
        ScanPerf(
            scan_id=1,
            environment="route-b",
            is_full_scan=True,
            branch="main",
            scan_ref="refs/heads/main",
            commit_sha="abc123",
            total_time=5.0,
            findings_total=2,
            scan_completed_at=T0,
        )
    )
    session.add(
        ScanPerf(
            scan_id=2,
            environment="managed-scans",
            is_full_scan=True,
            branch="main",
            scan_ref="refs/heads/main",
            commit_sha="abc123",
            total_time=25.0,
            findings_total=1,
            scan_completed_at=T0,
        )
    )
    session.commit()

    resp = client.get("/api/semgrep/perf")
    assert resp.status_code == 200
    body = resp.json()

    assert body["counts"] == {"homelab": 1, "managed": 1}
    assert len(body["comparisons"]) == 1

    comparison = body["comparisons"][0]
    assert comparison["match_kind"] == "commit"
    assert comparison["commit_sha"] == "abc123"
    assert comparison["route_b"] is not None
    assert comparison["sms"] is not None
    assert comparison["route_b"]["total_time"] == 5.0
    assert comparison["sms"]["total_time"] == 25.0
    assert comparison["speedup"] == 5.0

    # A full-scan matched pair aggregates into the "full" bucket.
    assert body["window_start"] is not None
    assert body["aggregates"]["full"]["pairs"] == 1
    assert body["aggregates"]["full"]["speedup"] == 5.0
    assert body["aggregates"]["pr"]["pairs"] == 0


def test_pre_homelab_managed_scans_are_excluded(session, client):
    # A managed scan that predates the first homelab scan is an invalid
    # cross-era comparison and must be excluded from the window entirely.
    before = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    session.add(
        ScanPerf(
            scan_id=10,
            environment="managed-scans",
            is_full_scan=True,
            branch="main",
            scan_ref="refs/heads/main",
            commit_sha="old999",
            total_time=99.0,
            findings_total=0,
            scan_completed_at=before,
        )
    )
    session.add(
        ScanPerf(
            scan_id=11,
            environment="route-b",
            is_full_scan=True,
            branch="main",
            scan_ref="refs/heads/main",
            commit_sha="new111",
            total_time=5.0,
            findings_total=0,
            scan_completed_at=T0,
        )
    )
    session.commit()

    body = client.get("/api/semgrep/perf").json()
    # Only the homelab scan is in window; the pre-homelab managed scan is gone.
    assert body["counts"] == {"homelab": 1, "managed": 0}
    scan_ids = [
        side["scan_id"]
        for row in body["comparisons"]
        for side in (row["route_b"], row["sms"])
        if side is not None
    ]
    assert 10 not in scan_ids
