"""Tests for the observability read endpoints.

After ADR 004 Layer 4 the endpoints no longer call external metrics services: they read a
precomputed snapshot row from Postgres. These tests override the DB session so
they assert the read-and-return behaviour without a database. The metrics
build logic is covered by stats_test / slo_test, and the write + grant
round-trip by observability_snapshot_grants_test (real Postgres).
"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from core.db import get_session
import dataclasses
import home.module
from framework import PRIVATE_PROFILE, build_app

# Compose only the home domain instead of the whole monolith: the
# same framework wiring the production app gets, without depending on
# the app composition root, which imports every other domain.
app = build_app(
    dataclasses.replace(PRIVATE_PROFILE, otel_enabled=False),
    (home.module.MODULE,),
)


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
        assert (
            resp.headers["cache-control"]
            == "public, s-maxage=60, stale-while-revalidate=86400, "
            "stale-if-error=31536000"
        )
        assert "set-cookie" not in resp.headers
    finally:
        _clear()


def test_stats_empty_when_no_snapshot():
    _with_session(None)
    try:
        resp = TestClient(app).get("/api/home/observability/stats")
        assert resp.status_code == 200
        assert resp.json() == {}
        assert (
            resp.headers["cache-control"]
            == "public, s-maxage=60, stale-while-revalidate=86400, "
            "stale-if-error=31536000"
        )
    finally:
        _clear()


def test_stats_failure_is_not_publicly_cacheable():
    session = MagicMock()
    session.execute.side_effect = RuntimeError("snapshot unavailable")
    app.dependency_overrides[get_session] = lambda: session
    try:
        resp = TestClient(app, raise_server_exceptions=False).get(
            "/api/home/observability/stats"
        )
        assert resp.status_code == 500
        assert "cache-control" not in resp.headers
    finally:
        _clear()
