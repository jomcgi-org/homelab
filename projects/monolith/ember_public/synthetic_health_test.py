from datetime import datetime, timedelta, timezone

import pytest

import ember_public.health as health
from ember_public.synthetic_models import EmberSyntheticProbe


def _row(**kwargs):
    values = {
        "demo": "bazel",
        "ok": True,
        "detail": "warm",
        "latency_ms": 12.0,
        "checked_at": datetime.now(timezone.utc),
        "last_ok_at": datetime.now(timezone.utc),
    }
    values.update(kwargs)
    return EmberSyntheticProbe(**values)


@pytest.mark.asyncio
async def test_missing_probe_is_fail_open(monkeypatch):
    async def read(_):
        return None

    monkeypatch.setattr(health, "read_probe", read)
    assert await health.synthetic_probe_health("bazel", 750)() == {
        "ok": True,
        "detail": "no probe recorded yet",
    }


@pytest.mark.asyncio
async def test_failed_probe_stays_down(monkeypatch):
    async def read(_):
        return _row(ok=False, detail="connection refused")

    monkeypatch.setattr(health, "read_probe", read)
    result = await health.synthetic_probe_health("bazel", 750)()
    assert result["ok"] is False
    assert "connection refused" in result["detail"]


@pytest.mark.asyncio
async def test_stale_success_is_down(monkeypatch):
    async def read(_):
        return _row(checked_at=datetime.now(timezone.utc) - timedelta(seconds=751))

    monkeypatch.setattr(health, "read_probe", read)
    result = await health.synthetic_probe_health("bazel", 750)()
    assert result["ok"] is False
    assert "prober may be dead" in result["detail"]


@pytest.mark.asyncio
async def test_fresh_success_is_ok(monkeypatch):
    async def read(_):
        return _row()

    monkeypatch.setattr(health, "read_probe", read)
    assert (await health.synthetic_probe_health("bazel", 750)())["ok"] is True


def test_staleness_thresholds_match_cron_cadences():
    assert health.EMBER_SYNTHETIC_STALENESS_S == 750.0
