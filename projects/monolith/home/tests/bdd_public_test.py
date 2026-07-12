"""BDD tests for home domain public API functions."""

from unittest.mock import patch

from shared.testing.markers import covers_public

import home


class TestPublicFunctions:
    @covers_public("home.get_today_events")
    def test_get_today_events_returns_list(self, session):
        # The test session is SQLite with no calendar_snapshot table, so the
        # reader degrades to []; the point here is the public surface shape.
        result = home.get_today_events(session)
        assert isinstance(result, list)

    @covers_public("home.on_startup_jobs")
    def test_on_startup_jobs_registers_jobs(self, session):
        with patch("scheduler.api.register_job") as mock_register:
            home.on_startup_jobs(session)
        names = {call.kwargs["name"] for call in mock_register.call_args_list}
        assert names == {"home.calendar_poll", "home.cluster_snapshot_refresh"}
