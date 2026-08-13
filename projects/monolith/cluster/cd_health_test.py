"""Tests for the advisory chart-lag and CI signals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cluster import cd_health as mod


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _clear_cache():
    mod._cache = None
    yield
    mod._cache = None


def _freight(version: str, created_at: datetime) -> dict:
    return {
        "name": f"freight-{version}",
        "version": version,
        "created_at": _iso(created_at),
    }


def test_up_to_date_is_ok():
    now = datetime.now(timezone.utc)
    fault, note = mod._evaluate_chart_lag(
        "0.301.1", [_freight("0.301.0", now)], 7200, now
    )
    assert fault is None
    assert note == "prod on 0.301.1, up to date"


def test_behind_inside_window_does_not_fault_and_says_how_far():
    now = datetime.now(timezone.utc)
    fault, note = mod._evaluate_chart_lag(
        "0.301.1", [_freight("0.302.0", now - timedelta(minutes=30))], 7200, now
    )
    assert fault is None
    assert note is not None and "0.302.0" in note and "30m" in note


def test_behind_past_window_faults_with_versions_and_age():
    now = datetime.now(timezone.utc)
    fault, note = mod._evaluate_chart_lag(
        "0.301.1", [_freight("0.302.0", now - timedelta(hours=3))], 7200, now
    )
    assert note is None
    assert fault is not None
    assert "0.301.1" in fault and "0.302.0" in fault and "180m" in fault


def test_oldest_unpromoted_freight_owns_the_clock():
    now = datetime.now(timezone.utc)
    freight = [
        _freight("0.302.0", now - timedelta(hours=3)),
        _freight("0.303.0", now - timedelta(minutes=10)),
    ]
    fault, _ = mod._evaluate_chart_lag("0.301.1", freight, 7200, now)
    assert fault is not None and "180m" in fault


def test_unreadable_live_version_reports_rather_than_faulting():
    fault, note = mod._evaluate_chart_lag(
        "latest", [], 7200, datetime.now(timezone.utc)
    )
    assert fault is None
    assert note == "prod chart version unreadable"


def test_unparseable_freight_versions_are_ignored():
    now = datetime.now(timezone.utc)
    fault, note = mod._evaluate_chart_lag(
        "0.301.1", [_freight("main", now - timedelta(days=2))], 7200, now
    )
    assert fault is None and note == "prod on 0.301.1, up to date"


def test_freight_older_than_live_is_ignored():
    now = datetime.now(timezone.utc)
    fault, note = mod._evaluate_chart_lag(
        "0.301.1", [_freight("0.300.9", now - timedelta(days=2))], 7200, now
    )
    assert fault is None and note == "prod on 0.301.1, up to date"


# CI signal. These encode the rules that took several passes to settle.
def test_quiet_green_main_is_healthy_at_any_age():
    week_old = datetime.now(timezone.utc) - timedelta(days=7)
    assert (
        mod._evaluate_ci(
            0,
            {"state": "success", "sha": "abc123456", "updated_at": _iso(week_old)},
            3600.0,
        )
        is None
    )


def test_red_but_recent_does_not_page():
    recent = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert (
        mod._evaluate_ci(
            1,
            {"state": "failure", "sha": "abc123456", "updated_at": _iso(recent)},
            3600.0,
        )
        is None
    )


def test_red_past_the_window_pages():
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    fault = mod._evaluate_ci(
        0,
        {"state": "failure", "sha": "abc123456", "updated_at": _iso(old)},
        3600.0,
    )
    assert fault is not None and "abc123456" in fault


def test_commits_with_nothing_completed_pages_as_ci_down():
    fault = mod._evaluate_ci(3, None, 3600.0)
    assert fault is not None and "CI may be down" in fault


def test_no_commits_and_nothing_completed_is_ok():
    assert mod._evaluate_ci(0, None, 3600.0) is None


@pytest.mark.asyncio
async def test_chart_lag_check_error_reports_but_does_not_fault(monkeypatch):
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)

    async def _boom(lag_s):
        raise RuntimeError("freights.kargo.akuity.io is forbidden")

    monkeypatch.setattr(mod, "_chart_lag_fault", _boom)
    result = await mod.cd_health()
    assert result["ok"] is True
    assert "chart lag check unavailable" in result["detail"]


@pytest.mark.asyncio
async def test_result_is_cached(monkeypatch):
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)
    calls = []

    async def _counting(lag_s):
        calls.append(1)
        return None, "prod on 0.301.1, up to date"

    monkeypatch.setattr(mod, "_chart_lag_fault", _counting)
    await mod.cd_health()
    await mod.cd_health()
    assert len(calls) == 1
