"""Tests for ember health registration and synthetic probe failures."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

import ember_public.health as health
from ember_public.module import MODULE
from ember_public.synthetic_models import EmberSyntheticProbe
from framework import PRIVATE_PROFILE, build_app


def test_module_retains_only_production_agent_probe():
    assert set(MODULE.register_health) == {"ember_codex"}


def test_module_has_no_advisory_health_components():
    assert MODULE.register_health_advisory is None


def test_retired_demo_latch_cannot_fail_health(monkeypatch, tmp_path):
    async def read_probe(demo):
        if demo == "postgres":
            return EmberSyntheticProbe(
                demo=demo,
                ok=False,
                detail="demo retired",
                checked_at=datetime.now(timezone.utc),
            )
        return None

    monkeypatch.setattr(health, "read_probe", read_probe)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'health.db'}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr("core.db.get_engine", lambda: engine)
    response = TestClient(build_app(PRIVATE_PROFILE, [MODULE])).get("/api/health")
    assert response.status_code == 200
    assert "ember_postgres" not in response.json()["components"]


def test_api_health_surfaces_codex_probe(monkeypatch, tmp_path):
    row = EmberSyntheticProbe(
        demo="codex",
        ok=False,
        detail="Codex lane unavailable",
        checked_at=datetime.now(timezone.utc),
    )

    async def read_probe(demo):
        return row if demo == "codex" else None

    monkeypatch.setattr(health, "read_probe", read_probe)
    # File-backed SQLite follows the repository test rule and permits separate connections.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'health.db'}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr("core.db.get_engine", lambda: engine)

    app = build_app(PRIVATE_PROFILE, [MODULE])
    response = TestClient(app).get("/api/health")
    body = response.json()

    assert response.status_code == 503
    assert body["components"]["ember_codex"]["ok"] is False
    assert body["components"]["ember_codex"]["detail"] == "Codex lane unavailable"
    assert "ember_spark" not in body["components"]
