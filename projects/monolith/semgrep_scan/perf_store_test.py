"""Unit tests for semgrep_scan.perf_store.

_merge_decision is pure and needs no DB. upsert_scan_perf is additionally
covered on SQLite (SQLModel.metadata.create_all, no migrations), mirroring
ships/models_test.py's schema-strip fixture since SQLite has no schemas.
"""

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from semgrep_scan.perf_store import ScanPerf, _merge_decision, upsert_scan_perf


def test_merge_decision_no_existing_row_inserts():
    assert _merge_decision(None, "route-b") == "insert"
    assert _merge_decision(None, "managed-scans") == "insert"


def test_merge_decision_route_b_not_clobbered_by_managed():
    assert _merge_decision("route-b", "managed-scans") == "skip"


def test_merge_decision_route_b_vs_route_b_updates():
    assert _merge_decision("route-b", "route-b") == "update"


def test_merge_decision_managed_vs_anything_updates():
    assert _merge_decision("managed-scans", "managed-scans") == "update"
    assert _merge_decision("managed-scans", "route-b") == "update"
    assert _merge_decision("", "route-b") == "update"


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


def test_upsert_inserts_new_row(session):
    upsert_scan_perf(
        session,
        ScanPerf(scan_id=1, environment="route-b", total_time=12.5, findings_total=3),
    )

    rows = session.exec(select(ScanPerf)).all()
    assert len(rows) == 1
    assert rows[0].scan_id == 1
    assert rows[0].environment == "route-b"
    assert rows[0].total_time == 12.5


def test_upsert_updates_existing_row_in_place(session):
    upsert_scan_perf(
        session,
        ScanPerf(
            scan_id=2, environment="managed-scans", total_time=20.0, findings_total=1
        ),
    )
    upsert_scan_perf(
        session,
        ScanPerf(
            scan_id=2, environment="managed-scans", total_time=18.0, findings_total=2
        ),
    )

    rows = session.exec(select(ScanPerf).where(ScanPerf.scan_id == 2)).all()
    assert len(rows) == 1
    assert rows[0].total_time == 18.0
    assert rows[0].findings_total == 2


def test_upsert_does_not_clobber_route_b_with_managed(session):
    upsert_scan_perf(
        session,
        ScanPerf(scan_id=3, environment="route-b", total_time=9.9, findings_total=5),
    )
    upsert_scan_perf(
        session,
        ScanPerf(
            scan_id=3, environment="managed-scans", total_time=99.9, findings_total=0
        ),
    )

    rows = session.exec(select(ScanPerf).where(ScanPerf.scan_id == 3)).all()
    assert len(rows) == 1
    assert rows[0].environment == "route-b"
    assert rows[0].total_time == 9.9
    assert rows[0].findings_total == 5
