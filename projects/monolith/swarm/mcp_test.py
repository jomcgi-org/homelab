"""The factory MCP tools: registration, the operator gate, and the trim."""

from __future__ import annotations

import asyncio
import importlib

import pytest

from auth.dependencies import reset_current_principal, set_current_principal
from auth.principal import Authority, Principal, PrincipalKind
from swarm import mcp


def _principal(
    *,
    authority: Authority = Authority.STANDING,
    kind: PrincipalKind = PrincipalKind.HUMAN,
    groups: tuple[str, ...] = ("operators",),
) -> Principal:
    return Principal(
        subject="joe",
        actor=(),
        scope=(),
        groups=groups,
        email=None,
        kind=kind,
        authority=authority,
    )


def _as(principal: Principal, coro_factory):
    token = set_current_principal(principal)
    try:
        return asyncio.run(coro_factory())
    finally:
        reset_current_principal(token)


@pytest.mark.asyncio
async def test_factory_tools_are_registered():
    importlib.import_module("swarm.mcp")
    from core.mcp_app import mcp as shared

    registered = {tool.name for tool in await shared.list_tools()}
    assert {"factory_status", "factory_escalations"} <= registered, (
        f"factory tools not registered; got: {sorted(registered)}"
    )


@pytest.mark.parametrize(
    "principal",
    [
        _principal(authority=Authority.ANONYMOUS, groups=()),
        _principal(authority=Authority.DELEGATED),
        _principal(kind=PrincipalKind.WORKLOAD),
        _principal(groups=()),
        _principal(groups=("friends",)),
    ],
)
@pytest.mark.parametrize("tool", [mcp.factory_status, mcp.factory_escalations])
def test_tools_refuse_callers_below_the_operator_floor(principal, tool, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("refused caller must not reach the database")

    monkeypatch.setattr(mcp, "_status_payload", explode)
    monkeypatch.setattr(mcp, "_escalations_payload", explode)

    result = _as(principal, tool)
    assert result["ok"] is False
    assert result["error"] == "standing operator authority is required"
    # The refusal says what was presented so a missing groups claim is
    # distinguishable from a caller who is simply not an operator.
    assert set(result["presented"]) == {"authority", "kind", "operators_group"}


def test_status_reaches_the_composer_for_an_operator(monkeypatch):
    monkeypatch.setattr(
        mcp, "_status_payload", lambda include_recent, session=None: {"ok": True}
    )
    assert _as(_principal(), mcp.factory_status) == {"ok": True}


def test_escalations_reach_the_composer_for_an_operator(monkeypatch):
    monkeypatch.setattr(
        mcp, "_escalations_payload", lambda include_resolved, session=None: {"ok": True}
    )
    assert _as(_principal(), mcp.factory_escalations) == {"ok": True}


def test_task_row_keeps_only_tripped_limits():
    row = mcp._task_row(
        {
            "issue_number": 7,
            "title": "fix it",
            "state": "admitted",
            "turns_used": 9,
            "allowance": {"turns": 10},
            "policy": {"task_budget_usd": 4.0},
            "unresolved_starts": 2,
            "limits": {
                "deadline_expired": False,
                "turn_limit_reached": True,
                "planner_turn_limit_reached": False,
                "budget_limit_reached": True,
            },
        }
    )
    assert row["limits_tripped"] == ["budget_limit_reached", "turn_limit_reached"]
    assert row["allowance_turns"] == 10
    assert row["task_budget_usd"] == 4.0
    assert row["unresolved_starts"] == 2
    assert row["escalation_open"] is False


def test_task_row_reports_an_unresolved_escalation_as_open():
    assert mcp._task_row({"escalation": {"question": "which?"}})["escalation_open"]
    resolved = mcp._task_row({"escalation": {"resolved": {"effect": "close"}}})
    assert resolved["escalation_open"] is False


def test_progress_counts_node_states_and_names_the_running_node():
    progress = mcp._progress(
        [
            {"node_key": "implement", "state": "running"},
            {"node_key": "plan", "state": "done"},
            {"node_key": "review", "state": "done"},
            {"node_key": "merge"},
        ]
    )
    assert progress["counts"] == {"running": 1, "done": 2, "pending": 1}
    assert progress["running"] == ["implement"]


def test_status_payload_trims_the_board_and_stamps_coverage(monkeypatch):
    board = {
        "ok": True,
        "state": "enabled",
        "generated_at": "2026-09-12T10:00:00+00:00",
        "version": 3,
        "actor": "joe",
        "admitted_count": 11,
        "policy": {"task_budget_usd": 4.0},
        "intake": {"admitted_today": 2, "max_per_day": 3},
        "lanes": {"delivery": 1},
        "review_routing": {"model": "opus"},
        "active": [{"issue_number": 1, "title": "a", "limits": {}}],
        "queued": [{"issue_number": 2, "title": "b"}],
        "recent": [{"issue_number": 3, "title": "c"}],
        "escalations": [
            {"issue_number": 4, "title": "d", "question": "which?", "open": True},
            {"issue_number": 5, "title": "e", "question": "gone?", "open": False},
        ],
    }
    monkeypatch.setattr(
        "agent_sessions.factory_view.build_factory_view", lambda session=None: board
    )

    payload = mcp._status_payload(include_recent=False)

    assert payload["observed_at"] == "2026-09-12T10:00:00+00:00"
    assert [task["issue_number"] for task in payload["active"]] == [1]
    assert [task["issue_number"] for task in payload["queued"]] == [2]
    # Recent is opt-in, and only the open escalation is owed a decision.
    assert "recent" not in payload
    assert [item["issue_number"] for item in payload["needs_operator"]] == [4]
    # An empty active list must never read as an idle fleet.
    assert payload["coverage"]["cloud_sessions"] == "not_indexed"


def test_status_payload_includes_recent_on_request(monkeypatch):
    board = {
        "ok": True,
        "state": "enabled",
        "active": [],
        "queued": [],
        "recent": [{"issue_number": 3, "title": "c"}],
        "escalations": [],
    }
    monkeypatch.setattr(
        "agent_sessions.factory_view.build_factory_view", lambda session=None: board
    )

    payload = mcp._status_payload(include_recent=True)
    assert [task["issue_number"] for task in payload["recent"]] == [3]


def test_status_payload_passes_through_an_uninitialised_factory(monkeypatch):
    monkeypatch.setattr(
        "agent_sessions.factory_view.build_factory_view",
        lambda session=None: {
            "ok": False,
            "reason": "not_initialized",
            "state": "disabled",
        },
    )

    payload = mcp._status_payload(include_recent=False)
    assert payload["ok"] is False
    assert payload["reason"] == "not_initialized"
    assert payload["coverage"]["cloud_sessions"] == "not_indexed"


def test_escalations_payload_hides_resolved_cards_by_default(monkeypatch):
    cards = [
        {"issue_number": 9, "open": True},
        {"issue_number": 8, "open": False},
    ]
    monkeypatch.setattr(
        "swarm.factory_controls.status",
        lambda session=None: {"ok": True, "receipts": ["r"]},
    )
    monkeypatch.setattr("swarm.factory_controls.escalations", lambda receipts: cards)

    default = mcp._escalations_payload(include_resolved=False)
    assert [card["issue_number"] for card in default["escalations"]] == [9]
    assert default["open_count"] == 1

    everything = mcp._escalations_payload(include_resolved=True)
    assert [card["issue_number"] for card in everything["escalations"]] == [9, 8]
    assert everything["open_count"] == 1


def test_escalations_payload_passes_through_an_uninitialised_factory(monkeypatch):
    monkeypatch.setattr(
        "swarm.factory_controls.status",
        lambda session=None: {"ok": False, "reason": "not_initialized"},
    )

    payload = mcp._escalations_payload(include_resolved=False)
    assert payload["ok"] is False
    assert payload["escalations"] == []
