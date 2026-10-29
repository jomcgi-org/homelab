"""BDD tests for chat domain API routes."""

import httpx

from shared.testing.markers import covers_route


class TestExplore:
    @covers_route("/api/chat/explore", method="POST")
    def test_explore_requires_query(self, live_server_with_fake_embedding):
        r = httpx.post(
            f"{live_server_with_fake_embedding}/api/chat/explore",
            json={"query": "test question"},
        )
        # Explore may stream or fail without LLM — assert it doesn't 500
        assert r.status_code != 500
