"""Unit tests for semgrep_scan.perf_harvest.

_normalize_env, _parse_dt, and scan_to_row are pure and need no DB or network.
harvest_scans is covered against the SQLite schema-strip fixture (mirroring
perf_store_test.py), with fetch_finding_scan_ids/fetch_scan monkeypatched so no
network call is ever made.
"""

from datetime import timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from semgrep_scan import perf_harvest
from semgrep_scan.perf_harvest import (
    _normalize_env,
    _parse_dt,
    harvest_scans,
    scan_to_row,
)
from semgrep_scan.perf_store import ScanPerf


def test_normalize_env_managed_scans():
    assert _normalize_env("SCAN_ENVIRONMENT_MANAGED_SCANS") == "managed-scans"


def test_normalize_env_unspecified_and_other():
    assert _normalize_env("SCAN_ENVIRONMENT_UNSPECIFIED") == ""
    assert _normalize_env("") == ""
    assert _normalize_env("SOMETHING_ELSE") == ""


def test_parse_dt_z_suffix():
    dt = _parse_dt("2026-07-10T12:34:56Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.astimezone(timezone.utc).hour == 12


def test_parse_dt_none_and_empty():
    assert _parse_dt(None) is None
    assert _parse_dt("") is None


def _managed_scan(**overrides) -> dict:
    scan = {
        "id": 42,
        "environment": "SCAN_ENVIRONMENT_MANAGED_SCANS",
        "isFullScan": True,
        "branch": "main",
        "commit": "abc123",
        "totalTime": 12.75,
        "findingsCounts": {"total": 3},
        "cliVersion": "1.90.0",
        "startedAt": "2026-07-10T10:00:00Z",
        "completedAt": "2026-07-10T10:00:13Z",
    }
    scan.update(overrides)
    return scan


def test_scan_to_row_managed_scan():
    row = scan_to_row(_managed_scan())
    assert row is not None
    assert row.scan_id == 42
    assert row.environment == "managed-scans"
    assert row.raw_environment == "SCAN_ENVIRONMENT_MANAGED_SCANS"
    assert row.is_full_scan is True
    assert row.branch == "main"
    assert row.scan_ref == "main"
    assert row.commit_sha == "abc123"
    # total_time is the startedAt->completedAt WALL (10:00:00 -> 10:00:13 = 13s),
    # the aligned comparison basis, not Semgrep's engine-only totalTime (12.75).
    assert row.total_time == 13.0
    assert row.findings_total == 3
    assert row.cli_version == "1.90.0"
    assert row.scan_started_at is not None
    assert row.scan_completed_at is not None


def test_scan_to_row_unspecified_returns_none():
    scan = _managed_scan(environment="SCAN_ENVIRONMENT_UNSPECIFIED")
    assert scan_to_row(scan) is None


def test_scan_to_row_missing_findings_counts_and_total_time():
    # With no timestamps AND no totalTime, total_time falls back to 0.0. The wall
    # is preferred when both timestamps are present (see test above), so drop them
    # here to exercise the fallback path.
    scan = _managed_scan()
    scan.pop("findingsCounts")
    scan.pop("startedAt")
    scan.pop("completedAt")
    scan["totalTime"] = None
    row = scan_to_row(scan)
    assert row is not None
    assert row.findings_total == 0
    assert row.total_time == 0.0


def test_scan_to_row_falls_back_to_total_time_without_timestamps():
    # No wall available (missing completedAt) -> use Semgrep's engine totalTime.
    scan = _managed_scan()
    scan.pop("completedAt")
    row = scan_to_row(scan)
    assert row is not None
    assert row.total_time == 12.75


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


def test_harvest_scans_skips_existing_and_non_managed(session, monkeypatch):
    # scan_id 1 is already stored; harvest should not re-fetch it.
    session.add(ScanPerf(scan_id=1, environment="managed-scans", total_time=1.0))
    session.commit()

    monkeypatch.setattr(
        perf_harvest, "fetch_finding_scan_ids", lambda repo, token: {1, 2, 3}
    )

    fetched_ids = []

    def fake_fetch_scan(scan_id, token):
        fetched_ids.append(scan_id)
        if scan_id == 2:
            return _managed_scan(id=2)
        if scan_id == 3:
            # Non-managed scan: should be skipped (scan_to_row returns None).
            return _managed_scan(id=3, environment="SCAN_ENVIRONMENT_UNSPECIFIED")
        raise AssertionError(f"unexpected fetch for scan_id={scan_id}")

    monkeypatch.setattr(perf_harvest, "fetch_scan", fake_fetch_scan)

    summary = harvest_scans(session, repo="jomcgi/homelab")

    assert 1 not in fetched_ids
    assert set(fetched_ids) == {2, 3}
    assert summary == {"harvested": 1, "candidates": 3, "skipped_existing": 1}

    rows = session.exec(select(ScanPerf)).all()
    scan_ids = {r.scan_id for r in rows}
    assert scan_ids == {1, 2}
