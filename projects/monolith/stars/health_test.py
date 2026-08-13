"""Tests for the bounded stars health component."""

from datetime import datetime, timedelta, timezone

import pytest

import stars.health as health
from stars.module import MODULE


def _snapshot(**overrides):
    values = {
        "site_count": 10,
        "future_forecast_rows": 100,
        "latest_fetched_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return health.StarsHealthSnapshot(**values)


def test_module_registers_stars_health():
    assert MODULE.register_health == {"stars": health.stars_health}
    assert MODULE.register_health_advisory is None


@pytest.mark.asyncio
async def test_health_is_ok_for_fresh_forecast(monkeypatch):
    monkeypatch.setattr(health, "_read_snapshot", lambda: _snapshot())

    result = await health.stars_health()

    assert result["ok"] is True
    assert "10 sites" in result["detail"]


@pytest.mark.asyncio
async def test_health_fails_when_forecast_is_stale(monkeypatch):
    monkeypatch.setattr(
        health,
        "_read_snapshot",
        lambda: _snapshot(
            latest_fetched_at=datetime.now(timezone.utc) - timedelta(hours=7)
        ),
    )

    result = await health.stars_health()

    assert result["ok"] is False
    assert "old" in result["detail"]


@pytest.mark.asyncio
async def test_health_fails_when_forecast_is_empty(monkeypatch):
    monkeypatch.setattr(
        health, "_read_snapshot", lambda: _snapshot(future_forecast_rows=0)
    )

    result = await health.stars_health()

    assert result == {"ok": False, "detail": "no future stars forecast rows"}
