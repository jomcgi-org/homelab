"""Tests for ember public health registration and synthetic probe failures."""

from __future__ import annotations

from datetime import datetime, timezone

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
    }


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
