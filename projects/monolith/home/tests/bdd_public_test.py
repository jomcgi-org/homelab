"""BDD tests for home domain public API functions."""

from shared.testing.markers import covers_public

import home


class TestPublicFunctions:
    @covers_public("home.get_today_events")
    def test_get_today_events_returns_list(self, session):
        # The test session is SQLite with no calendar_snapshot table, so the
        # reader degrades to []; the point here is the public surface shape.
        result = home.get_today_events(session)
        assert isinstance(result, list)
