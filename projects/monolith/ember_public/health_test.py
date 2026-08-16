"""Tests for ember public health registration and synthetic probe failures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

import ember_public.health as health
import ember_public.synthetic_probe as probe
from ember_public.module import MODULE
from ember_public.synthetic_models import EmberSyntheticProbe
from framework import PUBLIC_PROFILE, build_app


def test_module_registers_synthetic_postgres_health_hook():
    assert "ember_postgres" in MODULE.register_health
    assert set(MODULE.register_health) == {
        "ember_bazel",
        "ember_semgrep",
        "ember_pages",
        "ember_postgres",
        "ember_qwen",
    }


def test_qwen_staleness_matches_its_own_hourly_cadence():
    """The qwen probe must NOT reuse the 5-minute probes' staleness window.

    ember-qwen-session-synthetic is hourly, so at the 750s window an entirely
    healthy hourly latch reads as stale 12.5 minutes into every hour and the
    component reports "prober may be dead" for most of the time it is working.
    Its window has to exceed one hour, and it is the same 2.5x rule applied to
    the cadence this probe actually has.
    """
    assert health.EMBER_SYNTHETIC_STALENESS_S == 750.0
    assert health.EMBER_QWEN_STALENESS_S > 3600.0
    assert health.EMBER_QWEN_STALENESS_S == 9000.0


def test_module_has_no_advisory_health_components():
    assert MODULE.register_health_advisory is None


@pytest.mark.asyncio
async def test_probe_postgres_unconfigured_is_not_ok(monkeypatch):
    monkeypatch.delenv("DEMO_POSTGRES_DSN", raising=False)

    result = await probe.probe_postgres()

    assert result == {
        "ok": False,
        "detail": "DEMO_POSTGRES_DSN not configured",
        "latency_ms": None,
    }


def test_public_app_api_health_surfaces_synthetic_probe_failure(monkeypatch):
    """The public health component reports the probe latch, not a passive DB check."""
    monkeypatch.delenv("DEMO_POSTGRES_DSN", raising=False)

    row = EmberSyntheticProbe(
        demo="postgres",
        ok=False,
        detail="DEMO_POSTGRES_DSN not configured",
        checked_at=datetime.now(timezone.utc),
    )

    async def read_probe(_):
        return row

    monkeypatch.setattr(health, "read_probe", read_probe)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr("core.db.get_engine", lambda: engine)

    app = build_app(PUBLIC_PROFILE, [MODULE])
    resp = TestClient(app).get("/api/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["components"]["ember_postgres"]["ok"] is False
    assert "demo_postgres" not in body["components"]


def test_hourly_qwen_latch_is_not_stale(monkeypatch):
    """A healthy hourly qwen latch must read OK, not "prober may be dead".

    This is the end-to-end form of the staleness guard: a latch written 50
    minutes ago is entirely normal for an hourly prober, and would be marked
    stale by the 5-minute probes' 750s window.
    """
    row = EmberSyntheticProbe(
        demo="qwen",
        ok=True,
        detail="completed, destroyed",
        latency_ms=1800.0,
        checked_at=datetime.now(timezone.utc) - timedelta(minutes=50),
    )

    async def read_probe(demo):
        return row if demo == "qwen" else None

    monkeypatch.setattr(health, "read_probe", read_probe)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr("core.db.get_engine", lambda: engine)

    app = build_app(PUBLIC_PROFILE, [MODULE])
    body = TestClient(app).get("/api/health").json()

    assert body["components"]["ember_qwen"]["ok"] is True
    assert "prober may be dead" not in body["components"]["ember_qwen"]["detail"]
