from datetime import datetime, timedelta, timezone

import pytest

import ember_public.health as health
from ember_public.synthetic_models import EmberSyntheticProbe


def _row(**kwargs):
    values = {
        "demo": "codex",
        "ok": True,
        "detail": "completed, destroyed",
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
    assert await health.synthetic_probe_health(
        "codex", health.EMBER_CODEX_STALENESS_S
    )() == {
        "ok": True,
        "detail": "no probe recorded yet",
        "trace_id": None,
    }


@pytest.mark.asyncio
async def test_failed_probe_stays_down(monkeypatch):
    async def read(_):
        return _row(ok=False, detail="connection refused", trace_id="a" * 32)

    monkeypatch.setattr(health, "read_probe", read)
    result = await health.synthetic_probe_health(
        "codex", health.EMBER_CODEX_STALENESS_S
    )()
    assert result["ok"] is False
    assert "connection refused" in result["detail"]
    assert result["trace_id"] == "a" * 32


@pytest.mark.asyncio
async def test_success_with_legacy_null_trace_serializes_null(monkeypatch):
    async def read(_):
        return _row(trace_id=None)

    monkeypatch.setattr(health, "read_probe", read)
    result = await health.synthetic_probe_health(
        "codex", health.EMBER_CODEX_STALENESS_S
    )()

    assert result["ok"] is True
    assert result["trace_id"] is None


@pytest.mark.asyncio
async def test_stale_success_is_down(monkeypatch):
    async def read(_):
        return _row(checked_at=datetime.now(timezone.utc) - timedelta(seconds=9001))

    monkeypatch.setattr(health, "read_probe", read)
    result = await health.synthetic_probe_health(
        "codex", health.EMBER_CODEX_STALENESS_S
    )()
    assert result["ok"] is False
    assert "prober may be dead" in result["detail"]


@pytest.mark.asyncio
async def test_hour_old_success_is_ok_until_next_scheduled_probe(monkeypatch):
    async def read(_):
        return _row(checked_at=datetime.now(timezone.utc) - timedelta(hours=1))

    monkeypatch.setattr(health, "read_probe", read)
    assert (
        await health.synthetic_probe_health("codex", health.EMBER_CODEX_STALENESS_S)()
    )["ok"] is True


def test_staleness_threshold_matches_cron_cadence():
    # 2.5x the hourly ember-codex-session-synthetic schedule.
    assert health.EMBER_CODEX_STALENESS_S == 9000.0
