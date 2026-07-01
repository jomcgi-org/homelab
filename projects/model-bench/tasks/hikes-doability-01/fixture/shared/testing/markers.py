"""Pytest markers for BDD coverage tracking.

Usage:
    from shared.testing.markers import covers_route, covers_page, covers_public

    @covers_route("/api/home/schedule/today")
    def test_schedule_returns_events(live_server): ...

    @covers_page("/private")
    def test_dashboard_loads(page, sveltekit_server): ...

    @covers_public("knowledge.search_notes")
    def test_search_returns_results(session): ...
"""

import pytest


def covers_route(path: str, method: str = "GET"):
    """Mark a test as covering a specific API route."""
    return pytest.mark.covers_route(path=path, method=method)


def covers_page(path: str):
    """Mark a test as covering a frontend page (requires Playwright)."""
    return pytest.mark.covers_page(path=path)


def covers_public(qualified_name: str):
    """Mark a test as covering a domain public function."""
    return pytest.mark.covers_public(name=qualified_name)
