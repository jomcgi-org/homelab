from __future__ import annotations

import pytest

from factory.orchestration import agent_board_poll


def test_consumer_is_default_off(monkeypatch):
    monkeypatch.delenv(agent_board_poll.POLL_ENABLED_ENV, raising=False)
    assert agent_board_poll.eligible_lanes(
        ("delivery", "advisory"),
        reader=lambda _lanes: pytest.fail("disabled poll read the board"),
    ) == ("delivery", "advisory")


def test_active_blocker_defers_only_its_lane(monkeypatch):
    monkeypatch.setenv(agent_board_poll.POLL_ENABLED_ENV, "true")
    assert agent_board_poll.eligible_lanes(
        ("delivery", "advisory"),
        reader=lambda _lanes: frozenset({"blocker:lane:delivery"}),
    ) == ("advisory",)


def test_read_outage_leaves_exclusive_queue_available(monkeypatch):
    monkeypatch.setenv(agent_board_poll.POLL_ENABLED_ENV, "true")

    def unavailable(_lanes):
        raise RuntimeError("scope_unavailable")

    assert agent_board_poll.eligible_lanes(
        ("delivery", "advisory"), reader=unavailable
    ) == ("delivery", "advisory")


def test_soft_claim_never_defers_or_replaces_exclusive_gate(monkeypatch):
    monkeypatch.setenv(agent_board_poll.POLL_ENABLED_ENV, "true")
    # The poll understands blocker:lane only. A claim remains advisory intent,
    # and the existing receipt and acquire_lock gates still decide ownership.
    assert agent_board_poll.eligible_lanes(
        ("delivery",),
        reader=lambda _lanes: frozenset({"claim:issue:jomcgi-org/homelab#5704"}),
    ) == ("delivery",)
