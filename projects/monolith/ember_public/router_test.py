"""Tests for the demo-postgres router (mounted on both public and private tiers).

These mount ONLY the router on a bare FastAPI app and stub every underlying
handler, so nothing here reaches the embervm control plane or a real Postgres.
Moved from demos/firecracker_api_test.py (paths updated from
/api/demos/firecracker/postgres/* to /api/ember/postgres/*); the destructive
reset endpoint stays private-only and its test stays in
demos/firecracker_api_test.py.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import ember_public.core as core
from chat_public import turnstile
from ember_public.models import DemoPgSavings  # noqa: F401  (registers the table)
from ember_public.router import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _pg_status_payload(**overrides):
    base = {
        "workload": "demo-postgres",
        "state": "banked",
        "generation": 7,
        "bundle_generation": 7,
        "pair_valid": True,
        "volume_bytes": 123456,
        "instance": {
            "healthy": True,
            "last_active_at": "2026-07-17T10:00:00Z",
            "created_at": "2026-07-17T09:00:00Z",
        },
    }
    base.update(overrides)
    return base


def test_postgres_status_unconfigured(monkeypatch):
    monkeypatch.delenv("DEMO_POSTGRES_DSN", raising=False)

    resp = _client().get("/api/ember/postgres/status")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False}


def test_postgres_status_shapes_control_plane_payload(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload()

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)

    resp = _client().get("/api/ember/postgres/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["state"] == "banked"
    assert body["generation"] == 7
    assert body["bundle_generation"] == 7
    assert body["pair_valid"] is True
    assert body["healthy"] is True
    assert body["last_active_at"] == "2026-07-17T10:00:00Z"


def test_postgres_status_error_is_in_band(monkeypatch):
    """A flaky control-plane poll must not 5xx: the frontend polls sub-second."""
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        raise RuntimeError("boom")

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)

    resp = _client().get("/api/ember/postgres/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert "boom" in body["error"]


def test_postgres_query_unconfigured_is_503(monkeypatch):
    monkeypatch.delenv("DEMO_POSTGRES_DSN", raising=False)

    resp = _client().post("/api/ember/postgres/query", json={})
    assert resp.status_code == 503


def test_postgres_query_returns_timings_and_classification(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="banked", pair_valid=True)

    def fake_roundtrip(dsn, mode, session_tag):
        assert dsn == "postgresql://x"
        assert mode == "insert"
        assert session_tag is None
        return {
            "connect_ms": 850.0,
            "query_ms": 12.0,
            "mode": mode,
            "statements": [
                {"sql": "CREATE TABLE IF NOT EXISTS demo_orders (...)", "ms": 1.0},
                {"sql": "INSERT INTO demo_orders (...)", "ms": 2.0},
                {"sql": "SELECT ... FROM demo_orders ORDER BY id DESC", "ms": 3.0},
                {"sql": "SELECT item, sum(qty) ... GROUP BY item", "ms": 4.0},
                {"sql": "SELECT count(*), coalesce(sum(...)), ...", "ms": 5.0},
            ],
            "inserted": {
                "id": 42,
                "item": "flat white",
                "qty": 2,
                "unit_price": 3.50,
            },
            "rows": [
                {
                    "id": 42,
                    "item": "flat white",
                    "qty": 2,
                    "unit_price": 3.50,
                    "written_at": "2026-07-17T08:00:00+00:00",
                    "postmaster_start": "2026-07-17T08:00:00+00:00",
                }
            ],
            "breakdown": [{"item": "flat white", "units": 2, "revenue": 7.0}],
            "total_orders": 42,
            "total_revenue": 7.0,
            "postmaster_start": "2026-07-17T08:00:00+00:00",
        }

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    monkeypatch.setattr(core, "demo_pg_orders_roundtrip", fake_roundtrip)

    resp = _client().post("/api/ember/postgres/query", json={"mode": "insert"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["connect_ms"] == 850.0
    assert body["query_ms"] == 12.0
    assert body["mode"] == "insert"
    assert body["classification"] == "relight"
    assert body["phase_before"] == "banked"
    assert body["generation"] == 7
    assert body["rows"][0]["id"] == 42
    assert len(body["statements"]) == 5
    assert body["breakdown"][0]["item"] == "flat white"
    assert body["total_revenue"] == 7.0
    assert body["total_ms"] >= 0


def test_postgres_query_aggregate_mode(monkeypatch):
    """Aggregate mode is read-only: it wakes the VM but writes nothing."""
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="serving")

    def fake_roundtrip(dsn, mode, session_tag):
        assert mode == "aggregate"
        return {
            "connect_ms": 1.5,
            "query_ms": 3.0,
            "mode": mode,
            "statements": [],
            "inserted": None,
            "rows": [],
            "breakdown": [],
            "total_orders": 0,
            "total_revenue": 0.0,
            "postmaster_start": "2026-07-17T08:00:00+00:00",
        }

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    monkeypatch.setattr(core, "demo_pg_orders_roundtrip", fake_roundtrip)

    resp = _client().post("/api/ember/postgres/query", json={"mode": "aggregate"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "aggregate"
    assert body["inserted"] is None


def test_postgres_query_default_mode_is_insert(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="serving")

    def fake_roundtrip(dsn, mode, session_tag):
        assert mode == "insert"
        return {
            "connect_ms": 1.5,
            "query_ms": 3.0,
            "mode": mode,
            "statements": [],
            "inserted": {"id": 1, "item": "flat white", "qty": 1, "unit_price": 3.50},
            "rows": [],
            "breakdown": [],
            "total_orders": 1,
            "total_revenue": 3.50,
            "postmaster_start": "2026-07-17T08:00:00+00:00",
        }

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    monkeypatch.setattr(core, "demo_pg_orders_roundtrip", fake_roundtrip)

    resp = _client().post("/api/ember/postgres/query", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "insert"


def test_postgres_query_connect_failure_is_in_band(monkeypatch):
    """A refused connect (wake-rate limit / failed wake) reports, not 5xxs."""
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="serving")

    def fake_roundtrip(dsn, mode, session_tag):
        raise OSError("connection refused")

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    monkeypatch.setattr(core, "demo_pg_orders_roundtrip", fake_roundtrip)

    resp = _client().post("/api/ember/postgres/query", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert "connection refused" in body["error"]
    assert body["mode"] == "insert"
    assert body["classification"] == "warm"


def test_classify_wake_paths():
    assert core.classify_wake(None) == "unknown"
    assert core.classify_wake({"state": "serving"}) == "warm"
    assert core.classify_wake({"state": "banked", "pair_valid": True}) == "relight"
    assert core.classify_wake({"state": "banked", "pair_valid": False}) == "cold"
    assert core.classify_wake({"state": None}) == "cold"
    assert core.classify_wake({"state": "relighting"}) == "transitional"


def test_postgres_session_mint_without_turnstile_secret(monkeypatch):
    monkeypatch.setattr(turnstile, "SECRET_KEY", "")

    resp = _client().post("/api/ember/postgres/session", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "existing": False}
    set_cookie = resp.headers.get("set-cookie", "")
    assert "demo_pg_session" in set_cookie
    assert "HttpOnly" in set_cookie


def test_postgres_session_requires_token_when_turnstile_configured(monkeypatch):
    monkeypatch.setattr(turnstile, "SECRET_KEY", "sekret")

    resp = _client().post("/api/ember/postgres/session", json={})
    assert resp.status_code == 403
    body = resp.json()
    assert body["detail"] == "turnstile verification failed"


def test_postgres_session_verifies_token_with_turnstile(monkeypatch):
    monkeypatch.setattr(turnstile, "SECRET_KEY", "sekret")

    captured = {}

    class FakeResponse:
        def __init__(self, success):
            self._success = success

        def raise_for_status(self):
            return None

        def json(self):
            return {"success": self._success}

    class FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            assert url == turnstile.SITEVERIFY_URL
            assert data["secret"] == "sekret"
            captured["response"] = data["response"]
            return FakeResponse(captured.get("success", False))

    monkeypatch.setattr(turnstile.httpx, "AsyncClient", FakeAsyncClient)

    captured["success"] = False
    resp = _client().post(
        "/api/ember/postgres/session",
        json={"turnstile_token": "bad-token"},
    )
    assert resp.status_code == 403
    assert captured["response"] == "bad-token"

    captured["success"] = True
    resp = _client().post(
        "/api/ember/postgres/session",
        json={"turnstile_token": "good-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "existing": False}
    assert "demo_pg_session" in resp.headers.get("set-cookie", "")


def test_postgres_session_existing_cookie_short_circuits(monkeypatch):
    monkeypatch.setattr(turnstile, "SECRET_KEY", "")

    client = _client()
    client.cookies.set("demo_pg_session", "already-here")

    resp = client.post("/api/ember/postgres/session", json={})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "existing": True}
    assert "set-cookie" not in resp.headers


def test_postgres_query_passes_session_tag_from_cookie(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="serving")

    captured = {}

    def fake_roundtrip(dsn, mode, session_tag):
        captured["session_tag"] = session_tag
        return {
            "connect_ms": 1.5,
            "query_ms": 3.0,
            "mode": mode,
            "statements": [],
            "inserted": None,
            "rows": [],
            "breakdown": [],
            "total_orders": 0,
            "total_revenue": 0.0,
            "postmaster_start": "2026-07-17T08:00:00+00:00",
        }

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    monkeypatch.setattr(core, "demo_pg_orders_roundtrip", fake_roundtrip)

    client = _client()
    client.cookies.set("demo_pg_session", "visitor-cookie-value")

    resp = client.post("/api/ember/postgres/query", json={})
    assert resp.status_code == 200
    tag = captured["session_tag"]
    assert isinstance(tag, str)
    assert len(tag) == 16
    assert re.fullmatch(r"[0-9a-f]{16}", tag)


# ---------------------------------------------------------------------------
# All-time sleep-savings counter (demo_pg_savings)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _savings_db():
    """In-memory SQLite with demo_pg_savings created, mirroring sandbox/session_test.py."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_savings_first_sample_creates_row_with_no_credit(_savings_db):
    now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    total = core.record_demo_pg_savings_core(
        _savings_db, state="banked", generation=7, now=now
    )
    assert total == 0.0


def test_savings_banked_to_banked_same_generation_credits_elapsed(_savings_db):
    t0 = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    core.record_demo_pg_savings_core(_savings_db, state="banked", generation=7, now=t0)

    t1 = t0 + timedelta(seconds=10)
    total = core.record_demo_pg_savings_core(
        _savings_db, state="banked", generation=7, now=t1
    )
    assert total == 10 * 512.0


def test_savings_banked_to_banked_generation_change_credits_nothing(_savings_db):
    t0 = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    core.record_demo_pg_savings_core(_savings_db, state="banked", generation=7, now=t0)

    t1 = t0 + timedelta(seconds=10)
    total = core.record_demo_pg_savings_core(
        _savings_db, state="banked", generation=8, now=t1
    )
    assert total == 0.0


def test_savings_serving_to_banked_credits_nothing(_savings_db):
    t0 = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    core.record_demo_pg_savings_core(_savings_db, state="serving", generation=7, now=t0)

    t1 = t0 + timedelta(seconds=10)
    total = core.record_demo_pg_savings_core(
        _savings_db, state="banked", generation=7, now=t1
    )
    assert total == 0.0


def test_savings_sub_throttle_unchanged_sample_does_not_move_last_sample_at(
    _savings_db,
):
    t0 = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    core.record_demo_pg_savings_core(_savings_db, state="banked", generation=7, now=t0)

    t1 = t0 + timedelta(seconds=2)
    core.record_demo_pg_savings_core(_savings_db, state="banked", generation=7, now=t1)

    row = _savings_db.get(core.DemoPgSavings, 1)
    last_sample_at = row.last_sample_at
    last_sample_at = (
        last_sample_at
        if last_sample_at.tzinfo
        else last_sample_at.replace(tzinfo=timezone.utc)
    )
    assert last_sample_at == t0
    assert row.total_mib_seconds == 0.0

    # Past the throttle window, the deferred gap (t0 -> t2) is credited in full.
    t2 = t0 + timedelta(seconds=6)
    total = core.record_demo_pg_savings_core(
        _savings_db, state="banked", generation=7, now=t2
    )
    assert total == 6 * 512.0


def test_postgres_status_includes_total_saved_mib_s(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload()

    async def fake_record(state, generation):
        assert state == "banked"
        assert generation == 7
        return 1234.0

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    monkeypatch.setattr(core, "record_demo_pg_savings", fake_record)

    resp = _client().get("/api/ember/postgres/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_saved_mib_s"] == 1234.0


def test_postgres_status_omits_total_saved_mib_s_when_none(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload()

    async def fake_record(state, generation):
        return None

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    monkeypatch.setattr(core, "record_demo_pg_savings", fake_record)

    resp = _client().get("/api/ember/postgres/status")
    assert resp.status_code == 200
    assert "total_saved_mib_s" not in resp.json()


# ---------------------------------------------------------------------------
# Public-app composition: served on the public tier, demos is NOT.
# ---------------------------------------------------------------------------


def test_public_app_serves_ember_postgres_status():
    """The public app mounts ember_public: /api/ember/postgres/status responds."""
    from app.main_public import app as public_app

    client = TestClient(public_app)
    resp = client.get("/api/ember/postgres/status")
    # DEMO_POSTGRES_DSN/EMBERVM_URL are unset in the test environment, so this
    # is the in-band "unconfigured" shape, not a 404: proves the route is
    # mounted, not that the demo is live.
    assert resp.status_code == 200
    assert resp.json() == {"configured": False}


def test_public_app_serves_no_demos_route():
    """The public app must not mount any /api/demos route (demos is private-only)."""
    from app.main_public import app as public_app

    paths = {getattr(r, "path", None) for r in public_app.routes}
    assert not any(p and p.startswith("/api/demos") for p in paths)
