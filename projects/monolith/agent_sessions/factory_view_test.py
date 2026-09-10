"""Pure shaping tests for the private agents page's factory board."""

from __future__ import annotations

from agent_sessions.factory_view import (
    first_line,
    node_label,
    shape_node,
    shape_receipt,
)


def _node(key: str, **overrides) -> dict:
    node = {
        "node_key": key,
        "kind": "worker",
        "model": "sonnet",
        "deps": [],
        "max_cost_usd": 4.0,
        "created_in_version": 1,
        "discarded_in_version": None,
        "cancelled_in_version": None,
    }
    node.update(overrides)
    return node


def _run(key: str, attempt: int, status: str, session_id: int | None = None) -> dict:
    return {
        "node_key": key,
        "attempt": attempt,
        "status": status,
        "cost_usd": 1.5 if status == "succeeded" else None,
        "accounted_cost_usd": 1.5,
        "session_id": session_id,
        "created_at": "2026-09-10T05:00:00+00:00",
        "finished_at": None,
    }


def test_node_label_splits_kind_from_the_rest():
    assert node_label("implement_fix_probe_worker_destroy") == (
        "implement · fix probe worker destroy"
    )
    assert node_label("conductor_1") == "conductor · 1"
    assert node_label("review") == "review"


def test_first_line_skips_blank_lines_and_clips():
    assert first_line("\n\n  Chose add_node investigate\nmore") == (
        "Chose add_node investigate"
    )
    assert first_line("x" * 500, limit=10) == "x" * 10
    assert first_line(None) == ""


def test_node_state_follows_the_latest_attempt():
    sessions = {7: {"id": 7, "model": "sonnet", "status": "running"}}
    node = shape_node(
        _node("implement_fix"),
        [
            _run("implement_fix", 1, "failed", 5),
            _run("implement_fix", 2, "admitted", 7),
        ],
        sessions,
    )
    assert node["state"] == "running"
    assert [a["attempt"] for a in node["attempts"]] == [1, 2]
    assert node["session"] == sessions[7]


def test_node_without_runs_is_pending_and_retired_wins():
    assert shape_node(_node("review_x"), [], {})["state"] == "pending"
    retired = shape_node(
        _node("review_x", discarded_in_version=3),
        [_run("review_x", 1, "succeeded")],
        {},
    )
    assert retired["state"] == "retired"


def test_shape_receipt_joins_nodes_runs_and_sessions():
    receipt = {
        "id": 11,
        "issue_number": 5980,
        "generation": 3,
        "title": "probes park their guest",
        "url": "https://github.com/x/y/issues/5980",
        "state": "admitted",
        "task_id": "t-1",
        "task_paused": False,
        "cancellation_requested": False,
        "policy": {
            "conductor_model": "spark",
            "worker_model": "sonnet",
            "reviewer_model": "opus",
            "max_turns_per_task": 9,
            "task_budget_usd": 36.0,
            "max_attempts": 2,
            "repo": "x/y",
        },
        "starts": [
            {
                "start_key": "factory-node:t-1:conductor_1:1",
                "model": "spark",
                "status": "succeeded",
                "cost_usd": None,
                "session_id": 3,
                "actor": "factory:reconciler",
            }
        ],
        "turns_used": 1,
        "committed_cost_usd": 4.0,
        "unresolved_starts": 0,
        "limits": {"deadline_expired": False},
        "evidence": None,
    }
    nodes = [
        _node("conductor_1", kind="conductor", model="spark"),
        _node("implement_fix", deps=["conductor_1"]),
    ]
    runs = [_run("conductor_1", 1, "succeeded", 3)]
    shaped = shape_receipt(receipt, nodes, runs, {3: {"id": 3, "model": "spark"}})
    assert shaped["policy"] == {
        "conductor_model": "spark",
        "worker_model": "sonnet",
        "reviewer_model": "opus",
        "max_turns_per_task": 9,
        "task_budget_usd": 36.0,
        "max_attempts": 2,
    }
    assert "repo" not in shaped["policy"]
    assert shaped["starts"][0]["session_id"] == 3
    assert "actor" not in shaped["starts"][0]
    states = {n["node_key"]: n["state"] for n in shaped["nodes"]}
    assert states == {"conductor_1": "done", "implement_fix": "pending"}
    assert shaped["nodes"][1]["deps"] == ["conductor_1"]


def test_shape_receipt_without_a_plan_carries_no_nodes():
    shaped = shape_receipt({"id": 1, "state": "queued", "policy": None})
    assert shaped["nodes"] == []
    assert shaped["starts"] == []
    assert shaped["policy"]["conductor_model"] is None
