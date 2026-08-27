"""BDD tests for home domain API routes."""

import httpx

from shared.testing.markers import covers_route


class TestScheduleAPI:
    @covers_route("/api/home/schedule/today")
    def test_returns_list_of_events(self, live_server):
        r = httpx.get(f"{live_server}/api/home/schedule/today")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


class TestDashboardAPI:
    @covers_route("/api/home/dashboard")
    def test_returns_dashboard_sections(self, live_server):
        r = httpx.get(f"{live_server}/api/home/dashboard")
        assert r.status_code == 200
        data = r.json()
        for section in ("health", "github", "today"):
            assert section in data
        assert "cached_at" in data


class TestObservabilityAPI:
    @covers_route("/api/home/observability/stats")
    def test_returns_stats(self, live_server):
        r = httpx.get(f"{live_server}/api/home/observability/stats")
        assert r.status_code == 200
