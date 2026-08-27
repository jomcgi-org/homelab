"""Unit tests for home.dashboard: per-section error isolation, GitHub cache
TTL, check-run reduction, and private-tier-only router registration.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from home import dashboard


# ---------------------------------------------------------------------------
# build_dashboard: per-section error isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_dashboard_isolates_a_failing_section():
    """One collector raising does not blank the other sections."""
    with (
        patch.object(
            dashboard,
            "_collect_health",
            AsyncMock(side_effect=RuntimeError("k8s down")),
        ),
        patch.object(
            dashboard,
            "_collect_github",
            AsyncMock(return_value={"open_prs": [], "recent_merges": []}),
        ),
        patch.object(
            dashboard, "_collect_today", AsyncMock(return_value={"events": []})
        ),
    ):
        result = await dashboard.build_dashboard(session=MagicMock())

    assert result["health"] == {"error": "k8s down"}
    assert result["github"] == {"open_prs": [], "recent_merges": []}
    assert result["today"] == {"events": []}
    assert "cached_at" in result


@pytest.mark.asyncio
async def test_build_dashboard_all_sections_healthy():
    with (
        patch.object(
            dashboard, "_collect_health", AsyncMock(return_value={"healthy": True})
        ),
        patch.object(
            dashboard,
            "_collect_github",
            AsyncMock(return_value={"open_prs": [], "recent_merges": []}),
        ),
        patch.object(
            dashboard, "_collect_today", AsyncMock(return_value={"events": []})
        ),
    ):
        result = await dashboard.build_dashboard(session=MagicMock())

    for section in ("health", "github", "today"):
        assert "error" not in result[section]


@pytest.mark.asyncio
async def test_build_dashboard_multiple_sections_fail_independently():
    with (
        patch.object(
            dashboard,
            "_collect_health",
            AsyncMock(side_effect=RuntimeError("k8s down")),
        ),
        patch.object(
            dashboard,
            "_collect_github",
            AsyncMock(side_effect=RuntimeError("github down")),
        ),
        patch.object(
            dashboard, "_collect_today", AsyncMock(return_value={"events": []})
        ),
    ):
        result = await dashboard.build_dashboard(session=MagicMock())

    assert "error" in result["health"]
    assert "error" in result["github"]
    assert "error" not in result["today"]


# ---------------------------------------------------------------------------
# GitHub cache TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_cache_serves_from_cache_within_ttl():
    dashboard._github_cache = None
    fetch = AsyncMock(return_value={"open_prs": [{"number": 1}], "recent_merges": []})

    with (
        patch.object(dashboard, "_fetch_github_live", fetch),
        patch.object(dashboard.time, "monotonic", return_value=1000.0),
    ):
        first = await dashboard._collect_github()
        second = await dashboard._collect_github()

    assert first == second
    fetch.assert_called_once()


@pytest.mark.asyncio
async def test_github_cache_refetches_after_ttl_expires():
    dashboard._github_cache = None
    fetch = AsyncMock(
        side_effect=[
            {"open_prs": [{"number": 1}], "recent_merges": []},
            {"open_prs": [{"number": 2}], "recent_merges": []},
        ]
    )

    with patch.object(dashboard, "_fetch_github_live", fetch):
        with patch.object(dashboard.time, "monotonic", return_value=1000.0):
            first = await dashboard._collect_github()
        with patch.object(
            dashboard.time,
            "monotonic",
            return_value=1000.0 + dashboard._GITHUB_CACHE_TTL_SECS + 1,
        ):
            second = await dashboard._collect_github()

    assert first != second
    assert fetch.call_count == 2


# ---------------------------------------------------------------------------
# Check-run reduction
# ---------------------------------------------------------------------------


def _mock_client_with_check_runs(runs: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"check_runs": runs})
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_check_run_status_all_passing():
    client = _mock_client_with_check_runs(
        [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "success"},
        ]
    )
    status = await dashboard._fetch_check_run_status(client, "abc123")
    assert status == "passing"


@pytest.mark.asyncio
async def test_check_run_status_one_failing():
    client = _mock_client_with_check_runs(
        [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "failure"},
        ]
    )
    status = await dashboard._fetch_check_run_status(client, "abc123")
    assert status == "failing"


@pytest.mark.asyncio
async def test_check_run_status_still_running_is_pending():
    client = _mock_client_with_check_runs(
        [
            {"status": "completed", "conclusion": "success"},
            {"status": "in_progress", "conclusion": None},
        ]
    )
    status = await dashboard._fetch_check_run_status(client, "abc123")
    assert status == "pending"


@pytest.mark.asyncio
async def test_check_run_status_no_runs_is_pending():
    client = _mock_client_with_check_runs([])
    status = await dashboard._fetch_check_run_status(client, "abc123")
    assert status == "pending"


@pytest.mark.asyncio
async def test_check_run_status_fetch_error_is_pending():
    client = MagicMock()
    client.get = AsyncMock(side_effect=RuntimeError("network error"))
    status = await dashboard._fetch_check_run_status(client, "abc123")
    assert status == "pending"


# ---------------------------------------------------------------------------
# Router registration: private tier only
# ---------------------------------------------------------------------------


def _iter_route_paths(routes):
    """Yield every route ``.path``, recursing into included/mounted sub-routers.

    Starlette 1.x no longer flattens ``app.include_router()`` output into
    ``app.routes``: each included router appears as a single ``_IncludedRouter``
    wrapper with no ``.path``, and its child ``APIRoute`` objects (which carry
    the full, prefix-resolved ``.path``) live on ``wrapper.original_router.routes``.
    ``Mount`` sub-apps yield their own mount path but are not descended into.
    """
    from starlette.routing import Mount

    for route in routes:
        path = getattr(route, "path", None)
        if path is not None:
            yield path
        if isinstance(route, Mount):
            continue
        sub = getattr(route, "routes", None)
        if sub is None:
            orig = getattr(route, "original_router", None)
            sub = getattr(orig, "routes", None) if orig is not None else None
        if sub:
            yield from _iter_route_paths(sub)


def test_dashboard_router_registered_on_private_app():
    from app.main import app

    paths = set(_iter_route_paths(app.routes))
    assert "/api/home/dashboard" in paths


def test_dashboard_router_not_registered_on_public_app():
    from app.main_public import app as public_app

    paths = set(_iter_route_paths(public_app.routes))
    assert "/api/home/dashboard" not in paths
