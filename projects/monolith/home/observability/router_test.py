"""Tests for the observability read endpoints.

After ADR 004 Layer 4 the endpoints no longer call ClickHouse: they read a
precomputed snapshot row from Postgres. These tests override the DB session so
they assert the read-and-return behaviour without a database. The ClickHouse
build logic is covered by stats_test / slo_test, and the write + grant
round-trip by observability_snapshot_grants_test (real Postgres).
"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from core.db import get_session
from app.main import app


def _session_returning(row):
    """Fake session whose snapshot SELECT yields ``row`` (a 1-tuple or None)."""
    session = MagicMock()
    result = MagicMock()
    result.first.return_value = row
    session.execute.return_value = result
    return session


def _with_session(row):
    app.dependency_overrides[get_session] = lambda: _session_returning(row)


def _clear():
    app.dependency_overrides.pop(get_session, None)


def test_stats_returns_snapshot_payload():
    payload = {"cluster": {"nodes": 4}, "gpu": {"utilization_pct": 50.0}}
    _with_session((payload,))
    try:
        resp = TestClient(app).get("/api/home/observability/stats")
        assert resp.status_code == 200
        assert resp.json() == payload
    finally:
        _clear()


def test_stats_empty_when_no_snapshot():
    _with_session(None)
    try:
        resp = TestClient(app).get("/api/home/observability/stats")
        assert resp.status_code == 200
        assert resp.json() == {}
    finally:
        _clear()
