"""Tests for the demo_postgres /api/health component (ember_public/health.py).

Each test patches ember_public.core's cache/status/outcome seams directly, so
nothing here reaches the embervm control plane or a real Postgres, matching
health.py's own never-connect invariant.
"""

from __future__ import annotations

from time import monotonic

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

import ember_public.core as core
from ember_public.health import demo_postgres_health
from ember_public.module import MODULE
from framework import PUBLIC_PROFILE, build_app


@pytest.fixture(autouse=True)
def _reset_ember_public_module_state():
    """Mirror router_test.py's reset: the status cache and last-query-outcome
    are process-global, so tests must not leak state across each other."""

    def _reset():
        core._status_cache.update(
            {"at": None, "payload": None, "state": None, "state_changed_at": None}
        )
        core._last_query_outcome.update(
            {"at_monotonic": None, "ok": None, "connect_ms": None}
        )

    _reset()
    yield
    _reset()


def _pg_status_payload(**overrides):
    base = {
        "state": "banked",
        "generation": 7,
        "bundle_generation": 7,
        "pair_valid": True,
        "volume_bytes": 123456,
        "instance": {"healthy": True},
    }
    base.update(overrides)
    return base


def test_module_registers_demo_postgres_health_hook():
    assert MODULE.register_health["demo_postgres"] is demo_postgres_health
    assert set(MODULE.register_health) == {
        "demo_postgres",
        "ember_bazel",
        "ember_semgrep",
        "ember_pages",
        "ember_postgres_synthetic",
    }


@pytest.mark.asyncio
async def test_unconfigured_is_not_ok(monkeypatch):
    monkeypatch.delenv("DEMO_POSTGRES_DSN", raising=False)
    monkeypatch.setattr(core, "EMBERVM_URL", "")

    result = await demo_postgres_health()
    assert result["ok"] is False
    assert "unreachable or unconfigured" in result["detail"]


@pytest.mark.asyncio
async def test_control_plane_unreachable_is_not_ok(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)

    result = await demo_postgres_health()
    assert result["ok"] is False
    assert "connection refused" in result["detail"]


@pytest.mark.asyncio
async def test_healthy_banked_pair_valid(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="banked", pair_valid=True)

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)

    result = await demo_postgres_health()
    assert result == {"ok": True, "detail": "banked, pair valid"}


@pytest.mark.asyncio
async def test_healthy_serving(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="serving", pair_valid=None)

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)

    result = await demo_postgres_health()
    assert result == {"ok": True, "detail": "serving"}


@pytest.mark.asyncio
async def test_banked_with_invalid_pair_is_not_ok(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="banked", pair_valid=False)

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)

    result = await demo_postgres_health()
    assert result["ok"] is False
    assert "pair" in result["detail"]


@pytest.mark.asyncio
async def test_transitional_state_under_threshold_is_ok(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="cold_booting", pair_valid=None)

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    # Prime the cache so state_changed_at is set, then patch it to look recent.
    await core.cached_demo_pg_status()
    core._status_cache["state_changed_at"] = monotonic() - 10

    result = await demo_postgres_health()
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_stuck_transition_past_90s_is_not_ok(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="relighting", pair_valid=None)

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    await core.cached_demo_pg_status()
    core._status_cache["state_changed_at"] = monotonic() - 91

    result = await demo_postgres_health()
    assert result["ok"] is False
    assert "relighting" in result["detail"]
    assert "wedged" in result["detail"]


@pytest.mark.asyncio
async def test_evicted_pair_broken_past_threshold_is_not_ok(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(
            state="evicted",
            pair_valid=False,
            instance={"healthy": False, "terminal_reason": "pair_broken"},
        )

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    await core.cached_demo_pg_status()
    core._status_cache["state_changed_at"] = monotonic() - 91

    result = await demo_postgres_health()
    assert result["ok"] is False
    assert "pair_broken" in result["detail"]
    assert "broken" in result["detail"]


@pytest.mark.asyncio
async def test_evicted_pair_broken_under_threshold_is_ok(monkeypatch):
    """A brief pair_broken eviction (warmth discard) recovers on the next wake and
    must not flap the check before the stuck window elapses."""
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(
            state="evicted",
            pair_valid=False,
            instance={"healthy": False, "terminal_reason": "pair_broken"},
        )

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    await core.cached_demo_pg_status()
    core._status_cache["state_changed_at"] = monotonic() - 10

    result = await demo_postgres_health()
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_evicted_ttl_is_ok(monkeypatch):
    """A ttl eviction is benign recycling, healthy even past the stuck window."""
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(
            state="evicted",
            pair_valid=False,
            instance={"healthy": True, "terminal_reason": "ttl"},
        )

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    await core.cached_demo_pg_status()
    core._status_cache["state_changed_at"] = monotonic() - 120

    result = await demo_postgres_health()
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_recent_failed_wake_is_not_ok(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="banked", pair_valid=True)

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    core.record_query_outcome(ok=False, connect_ms=None)

    result = await demo_postgres_health()
    assert result["ok"] is False
    assert "wake attempt failed" in result["detail"]


@pytest.mark.asyncio
async def test_recent_slow_wake_is_not_ok(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="banked", pair_valid=True)

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    core.record_query_outcome(ok=True, connect_ms=65000.0)

    result = await demo_postgres_health()
    assert result["ok"] is False
    assert "65000ms" in result["detail"]


@pytest.mark.asyncio
async def test_old_failed_wake_outside_window_does_not_count(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="banked", pair_valid=True)

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    core.record_query_outcome(ok=False, connect_ms=None)
    core._last_query_outcome["at_monotonic"] = monotonic() - 601

    result = await demo_postgres_health()
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_newer_success_supersedes_older_failure(monkeypatch):
    monkeypatch.setenv("DEMO_POSTGRES_DSN", "postgresql://x")
    monkeypatch.setattr(core, "EMBERVM_URL", "http://embervm")

    async def fake_status():
        return _pg_status_payload(state="banked", pair_valid=True)

    monkeypatch.setattr(core, "fetch_demo_pg_status", fake_status)
    core.record_query_outcome(ok=False, connect_ms=None)
    core.record_query_outcome(ok=True, connect_ms=1200.0)

    result = await demo_postgres_health()
    assert result["ok"] is True


def test_public_app_api_health_503s_when_demo_postgres_unconfigured(monkeypatch):
    """End-to-end: build the real public app (via the real ember_public
    module) and confirm the demo_postgres component propagates a 503 on
    /api/health, exactly as the framework's generic component contract
    promises (see framework/core_test.py for the generic mechanism tests)."""
    monkeypatch.delenv("DEMO_POSTGRES_DSN", raising=False)
    monkeypatch.setattr(core, "EMBERVM_URL", "")

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr("core.db.get_engine", lambda: engine)

    app = build_app(PUBLIC_PROFILE, [MODULE])
    resp = TestClient(app).get("/api/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["components"]["demo_postgres"]["ok"] is False
