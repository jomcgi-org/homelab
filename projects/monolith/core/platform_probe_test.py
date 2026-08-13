"""Tests for the platform probe latch.

The behaviours worth pinning are the ones that decide whether the public
endpoint pages: fail-open on a missing row, staleness catching a dead writer,
and last_ok_at only advancing on success.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import platform_probe as mod


class _Row:
    def __init__(self, ok, detail="", checked_at=None, last_ok_at=None):
        self.ok = ok
        self.detail = detail
        self.checked_at = checked_at or datetime.now(timezone.utc)
        self.last_ok_at = last_ok_at


@pytest.mark.asyncio
async def test_missing_row_fails_open(monkeypatch):
    """Pre-migration rollout, missing grant, DB blip: all report healthy.

    A monitoring component that pages for its own storage being unavailable is
    worse than no component.
    """

    async def _none(name):
        return None

    monkeypatch.setattr(mod, "read_probe", _none)
    result = await mod.probe_health("cd", 750.0)()
    assert result["ok"] is True
    assert "no probe recorded yet" in result["detail"]


@pytest.mark.asyncio
async def test_fresh_ok_row_is_healthy(monkeypatch):
    async def _row(name):
        return _Row(ok=True, detail="cd ok")

    monkeypatch.setattr(mod, "read_probe", _row)
    assert (await mod.probe_health("cd", 750.0)())["ok"] is True


@pytest.mark.asyncio
async def test_probe_results_have_no_advisory_flag(monkeypatch):
    async def _row(name):
        return _Row(ok=True)

    monkeypatch.setattr(mod, "read_probe", _row)
    assert "advisory" not in (await mod.probe_health("cd", 750.0)())


@pytest.mark.asyncio
async def test_stale_green_row_is_a_fault(monkeypatch):
    """A dead writer leaves a stale GREEN row. Reporting that as healthy is the
    exact meta-monitoring gap this component exists to close."""

    async def _row(name):
        return _Row(ok=True, checked_at=datetime.now(timezone.utc) - timedelta(hours=3))

    monkeypatch.setattr(mod, "read_probe", _row)
    result = await mod.probe_health("cd", 750.0)()
    assert result["ok"] is False
    assert "writer may be dead" in result["detail"]


@pytest.mark.asyncio
async def test_not_ok_row_reports_downtime(monkeypatch):
    async def _row(name):
        return _Row(
            ok=False,
            detail="monolith is Unknown/Healthy for 40m",
            last_ok_at=datetime.now(timezone.utc) - timedelta(minutes=90),
        )

    monkeypatch.setattr(mod, "read_probe", _row)
    result = await mod.probe_health("cd", 750.0)()
    assert result["ok"] is False
    assert "down for 90m" in result["detail"]


@pytest.mark.asyncio
async def test_naive_timestamps_are_treated_as_utc(monkeypatch):
    """Postgres can hand back naive datetimes; subtracting one from an aware
    now() raises, which would surface as a crashing component."""

    async def _row(name):
        return _Row(ok=True, checked_at=datetime.now(timezone.utc).replace(tzinfo=None))

    monkeypatch.setattr(mod, "read_probe", _row)
    assert (await mod.probe_health("cd", 750.0)())["ok"] is True
