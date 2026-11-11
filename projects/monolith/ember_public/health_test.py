"""Tests for ember health registration and synthetic probe failures."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import importlib.util

import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine

import ember_public.health as health
import ember_public.module as ember_module
from ember_public.synthetic_models import EmberSyntheticProbe
from framework import PRIVATE_PROFILE, build_app


@pytest.fixture(params=["", "http://retired-detector.invalid/v1/health/durability"])
def module(request, monkeypatch):
    # Registration used to happen at import time. Exercise both the default and
    # an explicitly configured legacy URL before constructing a fresh module.
    with monkeypatch.context() as env:
        env.setenv("EMBER_DURABILITY_HEALTH_URL", request.param)
        yield importlib.reload(ember_module).MODULE
    importlib.reload(ember_module)


def test_module_retains_only_production_agent_probe(module):
    assert set(module.register_health) == {"ember_codex"}
    assert importlib.util.find_spec("ember_public.durability") is None


def test_module_has_no_advisory_health_components(module):
    assert module.register_health_advisory is None


def test_retired_demo_latch_cannot_fail_health(module, monkeypatch, tmp_path):
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
    response = TestClient(build_app(PRIVATE_PROFILE, [module])).get("/api/health")
    assert response.status_code == 200
    assert "ember_postgres" not in response.json()["components"]


def test_api_health_surfaces_codex_probe(module, monkeypatch, tmp_path):
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

    app = build_app(PRIVATE_PROFILE, [module])
    response = TestClient(app).get("/api/health")
    body = response.json()

    assert response.status_code == 503
    assert body["components"]["ember_codex"]["ok"] is False
    assert body["components"]["ember_codex"]["detail"] == "Codex lane unavailable"
    assert "ember_spark" not in body["components"]
