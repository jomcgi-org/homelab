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
async def test_postgres_failure_names_preemption_cause(monkeypatch):
    async def read(_):
        return _row(
            demo="postgres",
            ok=False,
            detail="connection refused",
            last_ok_at=datetime.now(timezone.utc) - timedelta(minutes=4),
        )

    async def live_status():
        return {
            "state": "failed",
            "anchor": {
                "health": "down",
                "draining": False,
                "missing_since_ms": 1_700_000_000_000,
            },
            "recovery": "restoring",
        }

    monkeypatch.setattr(health, "read_probe", read)
    monkeypatch.setattr(health.core, "EMBERVM_URL", "http://embervm")
    monkeypatch.setattr(health.core, "cached_demo_pg_status", live_status)
    monkeypatch.setattr(health.core, "time", lambda: 1_700_000_300.0)

    result = await health.synthetic_probe_health("postgres", 750)()

    assert result["ok"] is False
    assert result["cause"] == "preemption"
    assert result["detail"].startswith(
        "brick preempted, control plane restoring, down for 5m"
    )
    assert result["detail"].endswith(": connection refused")


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
    assert health.EMBER_CODEX_STALENESS_S == 9000.0
    assert health.EMBER_SPARK_STALENESS_S == 9000.0
