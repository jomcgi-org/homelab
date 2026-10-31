"""Pure shaping tests for the private agents page's factory board."""

from __future__ import annotations

from factory.private_view import (
    first_line,
    node_label,
    shape_node,
    shape_policy,
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


def test_node_view_distinguishes_planned_and_dispatched_effort():
    run = {
        **_run("implement_fix", 2, "admitted"),
        "pin": {"model": "astra", "effort": "xhigh"},
    }
    view = shape_node(_node("implement_fix", model="astra", effort="medium"), [run], {})
    assert view["effort"] == "medium"
    assert view["attempts"][0]["model"] == "astra"
    assert view["attempts"][0]["effort"] == "xhigh"


def test_shape_receipt_joins_nodes_runs_and_sessions():
    receipt = {
        "id": 11,
        "repo": "x/y",
        "issue_number": 5980,
        "generation": 3,
        "title": "probes park their guest",
        "url": "https://github.com/x/y/issues/5980",
        "state": "admitted",
        "task_class": "docs",
        "task_id": "t-1",
        "task_paused": False,
        "cancellation_requested": False,
        "policy": {
            "conductor_model": "spark",
            "worker_model": "sonnet",
            "reviewer_model": "opus",
            "max_task_turns_hard": 9,
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
        "planner_turns_used": 2,
        "committed_cost_usd": 4.0,
        "unresolved_starts": 0,
        "limits": {"deadline_expired": False},
        "evidence": None,
        "direction": {"option_key": "retry", "note": "operator free text"},
    }
    nodes = [
        _node("conductor_1", kind="conductor", model="spark"),
        _node("implement_fix", deps=["conductor_1"]),
    ]
    runs = [_run("conductor_1", 1, "succeeded", 3)]
    shaped = shape_receipt(receipt, nodes, runs, {3: {"id": 3, "model": "spark"}})
    assert shaped["repo"] == "x/y"
    assert shaped["policy"] == {
        "conductor_model": "spark",
        "worker_model": "sonnet",
        "reviewer_model": "opus",
        "max_task_turns_hard": 9,
        "max_parallel_nodes": None,
        "task_budget_usd": 36.0,
        "max_attempts": 2,
    }
    assert "repo" not in shaped["policy"]
    assert shaped["turns_used"] == 1 and shaped["planner_turns_used"] == 2
    assert shaped["task_class"] == "docs"
    assert "direction" not in shaped
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


def test_shape_policy_keeps_the_board_keys_only():
    from factory.private_view import shape_policy

    shaped = shape_policy(
        {
            "generation": 4,
            "max_tasks": 1,
            "conductor_model": "astra",
            "worker_model": "sol",
            "reviewer_model": "opus",
            "max_task_turns_hard": 9,
            "task_budget_usd": 36.0,
            "max_attempts": 2,
            "repo": "x/y",
            "issue_numbers": [5983],
            "model_pools": {"worker": ["sol"]},
        }
    )
    assert shaped == {
        "generation": 4,
        "max_tasks": 1,
        "conductor_model": "astra",
        "worker_model": "sol",
        "reviewer_model": "opus",
        "max_task_turns_hard": 9,
        "max_parallel_nodes": None,
        "task_budget_usd": 36.0,
        "max_attempts": 2,
        "model_pools": {"worker": ["sol"]},
        "role_effort": None,
    }
    assert shape_policy(None) is None


def test_build_factory_view_reads_a_real_control_row(tmp_path):
    """Drive the glue through sqlite so the status call signature and the
    board keys are exercised, not just the pure shaping."""
    import json

    from sqlalchemy import event
    from sqlmodel import Session, SQLModel, create_engine

    from factory.private_view import build_factory_view
    from factory.orchestration.factory_models import (
        FactoryAudit,
        FactoryControl,
        FactoryReceipt,
        FactoryStart,
        WorkItem,
        WorkItemEdge,
        WorkItemEvent,
    )
    from factory.orchestration.models import SwarmTask

    engine = create_engine(
        f"sqlite:///{tmp_path / 'view.db'}",
        connect_args={"check_same_thread": False},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def _fk(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                SwarmTask,
                FactoryControl,
                FactoryReceipt,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
                FactoryStart,
                FactoryAudit,
            )
        ],
    )
    with Session(engine) as db:
        unavailable = build_factory_view(session=db)
        assert unavailable["ok"] is False and unavailable["lanes"] is None
        assert unavailable["review_routing"] is None
        db.add(
            FactoryControl(
                id="factory",
                state="enabled",
                policy_json=(
                    '{"repo": "x/y", "generation": 4, "max_tasks": 1, '
                    '"conductor_model": "astra", "worker_model": "sol", '
                    '"reviewer_model": "opus", "max_task_turns_hard": 9, '
                    '"task_budget_usd": 36.0, "max_attempts": 2}'
                ),
                actor="test",
            )
        )
        # A queued receipt the way factory_intake.receive_issue writes it; the
        # intake module lives in the swarm package this test does not link.
        db.add(
            FactoryReceipt(
                repo="x/y",
                issue_number=5983,
                generation=4,
                title="held jobs",
                body="body",
                url="https://github.com/x/y/issues/5983",
                actor="test",
                state="queued",
            )
        )
        db.commit()
        view = build_factory_view(session=db)
        assert view["intake_backlog"]["available"] is False
        assert view["intake_backlog"]["issues"] == []
        public_input = build_factory_view(session=db, include_backlog=False)
        assert "intake_backlog" not in public_input
        control = db.get(FactoryControl, "factory")
        policy = json.loads(control.policy_json)
        policy["issue_numbers"] = [5983]
        control.policy_json = json.dumps(policy)
        db.commit()
        eligible_view = build_factory_view(session=db)

        # This Bazel target deliberately lacks the decision executor. Read-only
        # pause visibility must work for real cards and pending requests here.
        from factory.orchestration import factory_controls as controls, factory_pause

        task_id = "paused-status-only"
        db.add(SwarmTask(id=task_id, task_text="paused", conductor_model="astra"))
        db.flush()
        receipt = db.get(FactoryReceipt, view["queued"][0]["id"])
        receipt.task_id, receipt.state, receipt.task_paused = task_id, "admitted", True
        receipt.policy_json = json.dumps({**policy, "task_timeout_seconds": 3600})
        receipt.escalation_json = json.dumps(
            {
                "kind": "automatic_pause",
                "task_id": task_id,
                "options": [factory_pause.STOP],
            }
        )
        db.commit()
        visible = controls.escalation_view(controls._snapshot(db, receipt))
        assert visible["open"] and visible["options"]
        controls._audit(
            db,
            "operator",
            "decision_request",
            request={"receipt_id": receipt.id, "request_key": "unknown"},
            result={"state": "outcome_unknown"},
        )
        db.commit()
        blocked = controls.escalation_view(controls._snapshot(db, receipt))
        assert not blocked["open"] and blocked["options"] == []
        assert blocked["pause_warning"] == "active_paused_without_actionable_question"
    assert view["ok"] is True
    assert view["state"] == "enabled"
    assert view["policy"]["conductor_model"] == "astra"
    assert "repo" not in view["policy"]
    assert [r["issue_number"] for r in view["queued"]] == [5983]
    assert view["queued"][0]["nodes"] == []
    assert view["active"] == [] and view["recent"] == []
    assert view["intake"]["policy"]["enabled"] is False
    assert view["problem_issues"]["status"] == "off"
    assert view["problem_issues"]["enabled_sources"] == []
    # The control row carries a bare integer max_tasks, so it is the delivery
    # lane and the advisory lane is shut.
    assert view["review_routing"]["action"] is None
    assert view["review_routing"]["pause_percent"] == 85
    # Ineligible manual work remains visible but cannot reserve execution capacity.
    assert view["lanes"] == {
        "delivery": {"limit": 1, "active": 0, "queued": 0},
        "advisory": {"limit": 0, "active": 0, "queued": 0},
    }

    assert eligible_view["lanes"]["delivery"]["queued"] == 1


def test_a_policy_pinned_before_the_envelope_still_shows_its_turn_bound():
    """The live control row carries only the old fixed cap. It is the envelope."""
    legacy = {
        "generation": 4,
        "max_tasks": 1,
        "conductor_model": "astra",
        "worker_model": "sol",
        "reviewer_model": "opus",
        "max_turns_per_task": 9,
        "task_budget_usd": 36.0,
        "max_attempts": 2,
    }
    assert shape_policy(legacy)["max_task_turns_hard"] == 9
    assert (
        shape_policy({**legacy, "max_task_turns_hard": 40})["max_task_turns_hard"] == 40
    )
    shaped = shape_receipt({"policy": legacy, "id": 1, "issue_number": 7})
    assert shaped["policy"]["max_task_turns_hard"] == 9


def test_unknown_run_and_terminal_parent_never_claim_running():
    assert (
        shape_node(_node("review"), [_run("review", 1, "unrecognized")], {})["state"]
        == "uncertain"
    )
    assert (
        shape_node(
            _node("review"),
            [_run("review", 1, "admitted")],
            {},
            receipt_state="escalated",
        )["state"]
        == "uncertain"
    )


def test_execution_requires_fresh_exact_dispatch_without_result_or_cleanup():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from factory.private_view import current_execution

    now = datetime.now(timezone.utc)
    row = SimpleNamespace(
        id=7,
        local_session_id="local",
        status="running",
        ember_session_id="guest",
        result_receipt_fence_id=None,
        guest_cleanup_id=None,
    )
    pending = SimpleNamespace(
        seq=2, dispatch_count=1, claimed_by_replica="owner", claimed_at=now
    )
    receipt = SimpleNamespace(
        session_id=7,
        local_session_id="local",
        seq=2,
        dispatch_count=1,
        claim_owner="owner",
        guest_id="guest",
        superseded_at=None,
        received_at=None,
        response_observed_at=None,
        response_observer_released_at=None,
    )
    turn = SimpleNamespace(seq=1)
    assert current_execution(row, pending, receipt, turn, now)
    assert not current_execution(row, None, receipt, turn, now)
    assert not current_execution(row, pending, None, turn, now)
    for obj, key, value in [
        (pending, "claimed_at", now - timedelta(seconds=31)),
        (pending, "claimed_by_replica", "different"),
        (pending, "dispatch_count", 2),
        (receipt, "guest_id", "old-guest"),
        (receipt, "seq", 1),
        (receipt, "received_at", now),
        (receipt, "response_observer_released_at", now),
        (receipt, "superseded_at", now),
        (row, "result_receipt_fence_id", "fence"),
        (row, "guest_cleanup_id", "cleanup"),
        (turn, "seq", 2),
    ]:
        original = getattr(obj, key)
        setattr(obj, key, value)
        assert not current_execution(row, pending, receipt, turn, now), key
        setattr(obj, key, original)
