"""Tests for the cd health component (issue #4597).

The cases that matter are the ones that decide whether Joe gets paged at 3am,
so they are named for the judgement rather than the mechanics.
"""

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


# --------------------------------------------------------------------------
# ArgoCD signal
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_apps_synced_healthy_is_ok():
    apps = [
        {"name": "monolith", "sync": "Synced", "health": "Healthy", "finished_at": None}
    ]
    assert (await mod._argocd_fault_real(apps, 900.0))[0] is None


@pytest.mark.asyncio
async def test_app_stuck_unsynced_past_grace_trips():
    old = _iso(datetime.now(timezone.utc) - timedelta(minutes=40))
    apps = [
        {"name": "monolith", "sync": "Unknown", "health": "Healthy", "finished_at": old}
    ]
    fault, _note = await mod._argocd_fault_real(apps, 900.0)
    assert fault is not None
    assert "monolith is Unknown/Healthy" in fault


@pytest.mark.asyncio
async def test_app_mid_rollout_inside_grace_does_not_trip():
    recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=2))
    apps = [
        {
            "name": "monolith",
            "sync": "Synced",
            "health": "Progressing",
            "finished_at": recent,
            "health_changed_at": recent,
        }
    ]
    assert (await mod._argocd_fault_real(apps, 900.0))[0] is None


@pytest.mark.asyncio
async def test_app_with_no_finish_time_does_not_trip():
    """Cannot date the fault, so treat as in-flight rather than page.

    A genuinely stuck app acquires a finishedAt on its next attempt; guessing
    here would page on every fresh rollout.
    """
    apps = [
        {
            "name": "new-app",
            "sync": "OutOfSync",
            "health": "Missing",
            "finished_at": None,
            "health_changed_at": None,
        }
    ]
    assert (await mod._argocd_fault_real(apps, 900.0))[0] is None


@pytest.mark.asyncio
async def test_a_settled_app_going_unhealthy_gets_its_full_grace():
    """The defect that 503'd jomcgi.dev, in its exact shape.

    embervm had been Synced and untouched since 07:36. When a brick could not
    be scheduled it went Progressing, and the check dated that fault from the
    last SYNC, computed 622 minutes, and blew past a 15 minute grace on the
    first bad tick.

    The inversion is what makes it worth a test rather than a comment: the more
    settled an app is, the staler its finished_at, so the LESS grace it got.
    A healthy deployment history actively reduced the protection.
    """
    apps = [
        {
            "name": "embervm",
            "sync": "Synced",
            "health": "Progressing",
            "finished_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=622)),
            "health_changed_at": _iso(
                datetime.now(timezone.utc) - timedelta(minutes=2)
            ),
        }
    ]
    assert (await mod._argocd_fault_real(apps, 900.0))[0] is None


@pytest.mark.asyncio
async def test_a_genuinely_stuck_app_still_trips_and_reports_the_right_age():
    """The other half: the grace must still expire, and say so honestly.

    Asserting the minutes matters. The old message read "for 622m" while the
    app had been unhealthy for two, so the number was not merely early, it
    described a different event.
    """
    apps = [
        {
            "name": "embervm",
            "sync": "Synced",
            "health": "Progressing",
            "finished_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=622)),
            "health_changed_at": _iso(
                datetime.now(timezone.utc) - timedelta(minutes=40)
            ),
        }
    ]
    fault, _note = await mod._argocd_fault_real(apps, 900.0)
    assert fault is not None
    assert "embervm is Synced/Progressing for 40m" in fault, fault


@pytest.mark.asyncio
async def test_outofsync_but_healthy_is_still_dated_from_the_last_sync():
    """Unchanged, and deliberately so.

    `.status.sync` carries no transition time, so the last sync attempt is the
    only clock available. That is a weaker signal in the opposite direction,
    since an app retrying forever keeps refreshing it (#4727), which this
    change does not attempt to fix.
    """
    apps = [
        {
            "name": "kyverno",
            "sync": "OutOfSync",
            "health": "Healthy",
            "finished_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=40)),
            "health_changed_at": _iso(datetime.now(timezone.utc) - timedelta(days=3)),
        }
    ]
    fault, _note = await mod._argocd_fault_real(apps, 900.0)
    assert fault is not None
    assert "kyverno is OutOfSync/Healthy for 40m" in fault, fault


@pytest.mark.asyncio
async def test_unhealthy_app_with_no_health_transition_does_not_trip():
    """No fallback to finished_at, because that fallback IS the bug.

    ArgoCD always stamps health.lastTransitionTime, so this is defensive. If it
    is ever missing the honest move is to treat the app as undateable rather
    than reach for the timestamp that caused the incident.
    """
    apps = [
        {
            "name": "embervm",
            "sync": "Synced",
            "health": "Degraded",
            "finished_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=622)),
            "health_changed_at": None,
        }
    ]
    assert (await mod._argocd_fault_real(apps, 900.0))[0] is None


# --------------------------------------------------------------------------
# CI signal. These encode the rules that took several passes to settle.
# --------------------------------------------------------------------------


def test_quiet_green_main_is_healthy_at_any_age():
    """No commits does NOT mean no signal: the last completed status stands.

    Treating silence as staleness would page every night and every weekend.
    """
    week_old = datetime.now(timezone.utc) - timedelta(days=7)
    fault = mod._evaluate_ci(
        recent_commit_count=0,
        newest_completed={
            "state": "success",
            "sha": "abc123456",
            "updated_at": _iso(week_old),
        },
        red_window_s=3600.0,
    )
    assert fault is None


def test_red_but_recent_does_not_page():
    """Someone is probably mid-fix. Red only matters once it persists."""
    ten_min = datetime.now(timezone.utc) - timedelta(minutes=10)
    fault = mod._evaluate_ci(
        recent_commit_count=1,
        newest_completed={
            "state": "failure",
            "sha": "abc123456",
            "updated_at": _iso(ten_min),
        },
        red_window_s=3600.0,
    )
    assert fault is None


def test_red_past_the_window_pages():
    two_h = datetime.now(timezone.utc) - timedelta(hours=2)
    fault = mod._evaluate_ci(
        recent_commit_count=0,
        newest_completed={
            "state": "failure",
            "sha": "abc123456",
            "updated_at": _iso(two_h),
        },
        red_window_s=3600.0,
    )
    assert fault is not None
    assert "abc123456" in fault


def test_commits_with_nothing_completed_pages_as_ci_down():
    """The meta-monitoring gap: if CI stops reporting, "is the latest red"
    reports green forever."""
    fault = mod._evaluate_ci(
        recent_commit_count=3, newest_completed=None, red_window_s=3600.0
    )
    assert fault is not None
    assert "CI may be down" in fault


def test_no_commits_and_nothing_completed_is_ok():
    """Nothing to assert. A repo with no completed statuses in range is not a
    platform fault."""
    assert (
        mod._evaluate_ci(
            recent_commit_count=0, newest_completed=None, red_window_s=3600.0
        )
        is None
    )


# --------------------------------------------------------------------------
# Component wiring
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_github_token_fails_open_but_says_so(monkeypatch):
    """A config gap is not a platform outage and must not page, but it must not
    be silent either."""
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)

    async def _no_argocd_fault(grace_s):
        return (None, None)

    monkeypatch.setattr(mod, "_argocd_fault", _no_argocd_fault)
    result = await mod.cd_health()
    assert result["ok"] is True
    assert "no GITHUB_API_TOKEN" in result["detail"]


@pytest.mark.asyncio
async def test_argocd_check_error_reports_but_does_not_page(monkeypatch):
    """Includes the Forbidden an RBAC gap produces. A broken checker must not
    masquerade as a broken platform."""
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)

    async def _boom(grace_s):
        raise RuntimeError("applications.argoproj.io is forbidden")

    monkeypatch.setattr(mod, "_argocd_fault", _boom)
    result = await mod.cd_health()
    assert result["ok"] is True
    assert "argocd check unavailable" in result["detail"]


@pytest.mark.asyncio
async def test_argocd_fault_makes_the_component_not_ok(monkeypatch):
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)

    async def _fault(grace_s):
        return ("monolith is Unknown/Healthy for 40m", None)

    monkeypatch.setattr(mod, "_argocd_fault", _fault)
    result = await mod.cd_health()
    assert result["ok"] is False
    assert "40m" in result["detail"]


@pytest.mark.asyncio
async def test_result_is_cached(monkeypatch):
    """UptimeRobot polls frequently; neither upstream should be hit per request."""
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)
    calls = []

    async def _counting(grace_s):
        calls.append(1)
        return (None, None)

    monkeypatch.setattr(mod, "_argocd_fault", _counting)
    await mod.cd_health()
    await mod.cd_health()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_non_prod_app_is_reported_but_does_not_fault():
    """A dev environment must not 503 the public health endpoint.

    This is the regression test for a real outage: monolith-dev sat
    Synced/Degraded on a stuck Atlas migration and took jomcgi.dev/health to
    503 while production was entirely healthy.
    """
    old = _iso(datetime.now(timezone.utc) - timedelta(minutes=40))
    apps = [
        {
            "name": "monolith-dev",
            "sync": "Synced",
            "health": "Degraded",
            "finished_at": old,
            "health_changed_at": old,
        }
    ]
    fault, note = await mod._argocd_fault_real(
        apps, 900.0, non_prod=frozenset({"monolith-dev"})
    )
    assert fault is None, "a non-production app must not fault the component"
    assert note is not None and "monolith-dev is Synced/Degraded" in note, (
        "it must still be REPORTED: a dev app failing to sync is an early "
        "signal, since it runs the chart production is about to run"
    )


@pytest.mark.asyncio
async def test_production_still_faults_alongside_a_non_prod_note():
    """Scoping must not swallow the signal it was narrowed around."""
    old = _iso(datetime.now(timezone.utc) - timedelta(minutes=40))
    apps = [
        {
            "name": "monolith",
            "sync": "Unknown",
            "health": "Healthy",
            "finished_at": old,
        },
        {
            "name": "monolith-dev",
            "sync": "Synced",
            "health": "Degraded",
            "finished_at": old,
            "health_changed_at": old,
        },
    ]
    fault, note = await mod._argocd_fault_real(
        apps, 900.0, non_prod=frozenset({"monolith-dev"})
    )
    assert fault is not None and "monolith is Unknown/Healthy" in fault
    assert "monolith-dev" not in (fault or ""), "dev must not leak into the fault"
    assert note is not None and "monolith-dev" in note


@pytest.mark.asyncio
async def test_non_prod_list_is_configurable_and_defaults_to_dev(monkeypatch):
    monkeypatch.delenv("CD_HEALTH_NON_PROD_APPS", raising=False)
    assert "monolith-dev" in mod._non_prod_apps()
    monkeypatch.setenv("CD_HEALTH_NON_PROD_APPS", "a , b,")
    assert mod._non_prod_apps() == frozenset({"a", "b"})
