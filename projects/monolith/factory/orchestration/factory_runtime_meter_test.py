"""Native callback replays, objective history, and exact-invocation fencing."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from factory.execution import admission, result_receipts, store, runtime_meter_evidence
from factory.execution.models import (
    AgentSession,
    AgentTurn,
    AgentResultReceipt,
    PendingMessage,
    AgentCapacityPool,
    AgentCapacityReservation,
)
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_runtime_meter as meter
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
)
from factory.orchestration.models import (
    SwarmTask,
    SwarmNodeRun,
    SwarmPlanVersion,
    SwarmPlanNode,
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'meter.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={
            "schema_translate_map": {"swarm": None, "agent_sessions": None}
        },
    )
    models = (
        AgentSession,
        AgentTurn,
        AgentResultReceipt,
        PendingMessage,
        AgentCapacityPool,
        AgentCapacityReservation,
        FactoryAudit,
        FactoryControl,
        FactoryReceipt,
        FactoryStart,
        SwarmTask,
        SwarmNodeRun,
        SwarmPlanVersion,
        SwarmPlanNode,
    )
    SQLModel.metadata.create_all(engine, tables=[m.__table__ for m in models])
    for module in (admission, result_receipts, store, controls):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setenv("FACTORY_RUNTIME_METER_ENABLED", "true")
    monkeypatch.setenv("FACTORY_RUNTIME_METER_TASK_IDS", "task")
    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "true")
    policy = {
        "repo": "org/repo",
        "issue_numbers": [7001],
        "generation": 0,
        "max_tasks": 1,
        "max_turns_per_task": 9,
        "task_budget_usd": 50,
        "turn_budget_usd": 4,
        "allowed_models": ["astra"],
        "conductor_model": "astra",
        "worker_model": "astra",
        "base_branch": "main",
        "max_attempts": 2,
        "task_timeout_seconds": 7200,
        "turn_timeout_seconds": 3600,
        "auto_merge": False,
    }
    with Session(engine) as db, db.begin():
        db.add_all(
            [
                FactoryControl(
                    id="factory",
                    state="enabled",
                    actor="operator",
                    policy_json=json.dumps(policy),
                ),
                SwarmTask(
                    id="task", task_text="fix", conductor_model="astra", budget_usd=50
                ),
                FactoryReceipt(
                    id=838,
                    task_id="task",
                    repo="org/repo",
                    issue_number=7001,
                    title="fix",
                    body="fix",
                    url="https://example.invalid",
                    actor="operator",
                    state="admitted",
                    policy_json=json.dumps(policy),
                ),
                FactoryStart(
                    task_id="task",
                    start_key="factory-node:task:implement:1",
                    actor="factory",
                    model="astra",
                    max_cost_usd=4,
                    status="reserved",
                    session_id=1,
                ),
                AgentSession(
                    id=1,
                    local_session_id="factory:task:implement:1",
                    workspace="guest",
                    branch="factory/task",
                    ember_session_id="guest",
                    status="running",
                    model="astra",
                    workflow_id="factory-node:task:implement:1",
                    node_key="implement",
                    node_attempt=1,
                ),
                PendingMessage(
                    session_id=1,
                    seq=1,
                    message_text="fix",
                    claimed_by_replica="owner",
                    claimed_at=datetime.now(timezone.utc),
                    dispatch_count=1,
                ),
                AgentCapacityReservation(
                    session_id=1,
                    local_session_id="factory:task:implement:1",
                    pending_seq=1,
                    tier="project",
                    state="running",
                    owner="owner",
                ),
            ]
        )
    yield engine
    engine.dispose()


def prepare():
    return result_receipts.prepare_receipt(
        1, "owner", 1, "guest", b'{"model":"astra","dispatch_id":"exact-dispatch"}'
    )


def update(receipt, sequence, output=0, coverage="parent"):
    return meter.ingest(
        receipt["id"],
        receipt["token"],
        {
            "sequence": sequence,
            "usage": {
                "input_tokens": 0,
                "output_tokens": output,
                "cached_input_tokens": 0,
            },
            "coverage": coverage,
        },
    )


def test_native_binding_and_acknowledged_duplicates_are_durable(database):
    receipt = prepare()
    assert receipt["runtime_meter"] is True
    assert not update(receipt, 0)["stop"]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: update(receipt, 1, 100000), range(2)))
    assert all(not result["stop"] for result in results)
    with Session(database) as db:
        owner = db.get(FactoryReceipt, 838)
        totals = meter.objective_totals(db, owner)
        assert totals["known_list_usd"] == 5
        assert not totals["hard_ceiling_enforced"]
        assert len(meter._records_for_receipt(db, receipt["id"], meter.UPDATE)) == 2
        assert owner.task_paused is False


@pytest.mark.parametrize("kind", ["limit", "gap", "conflict", "regression", "child"])
def test_incomplete_or_exhausted_usage_pauses_and_uses_existing_stop_owner(
    database, monkeypatch, kind
):
    receipt = prepare()
    stopped = []
    monkeypatch.setattr(meter, "stop_bound_attempt", stopped.append)
    update(receipt, 0)
    update(receipt, 1, 100)
    args = {
        "limit": (2, 10000000, "parent"),
        "gap": (3, 101, "parent"),
        "conflict": (1, 101, "parent"),
        "regression": (2, 99, "parent"),
        "child": (2, 100, "unknown"),
    }[kind]
    result = update(receipt, *args)
    assert result["stop"]
    assert stopped == [receipt["id"]]
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).task_paused
        start = db.exec(select(FactoryStart)).one()
        assert start.status == "reserved" and start.max_cost_usd == 4
    meter.reconcile("task")
    assert stopped == [receipt["id"], receipt["id"]]


def test_late_usage_charges_original_dispatch_but_cannot_stop_replacement(
    database, monkeypatch
):
    first = prepare()
    update(first, 0)
    second = prepare()
    assert second["id"] != first["id"]
    assert update(first, 1, 100000)["stop"]
    with pytest.raises(ValueError, match="meter_not_tripped"):
        meter.stop_bound_attempt(first["id"])
    with Session(database) as db:
        assert (
            meter.objective_totals(db, db.get(FactoryReceipt, 838))["known_list_usd"]
            == 5
        )
        assert not db.get(FactoryReceipt, 838).task_paused


def test_final_turn_reconciles_provisional_usage_without_double_charge(database):
    receipt = prepare()
    update(receipt, 0)
    update(receipt, 1, 100000)
    with Session(database) as db, db.begin():
        db.add(
            AgentTurn(
                session_id=1,
                seq=1,
                list_cost_usd=6,
                cost_usd=123,
                prompt="fix",
                result_text="done",
                terminal_reason="completed",
            )
        )
    with Session(database) as db:
        totals = meter.objective_totals(db, db.get(FactoryReceipt, 838))
        assert totals["known_list_usd"] == 6
        assert totals["unknown_or_reserved_exposure_usd"] == 4


def test_readmission_keeps_native_and_unlinked_session_costs(database):
    receipt = prepare()
    update(receipt, 0)
    update(receipt, 1, 100000)
    with Session(database) as db, db.begin():
        owner = db.get(FactoryReceipt, 838)
        owner.task_id = "replacement"
        db.add_all(
            [
                SwarmTask(
                    id="replacement",
                    task_text="same objective",
                    conductor_model="astra",
                    budget_usd=50,
                ),
                FactoryAudit(
                    task_id="task",
                    actor="operator",
                    action="admit_next",
                    detail_json='{"receipt_id":838}',
                ),
                AgentSession(
                    id=2,
                    local_session_id="factory:task:lost:1",
                    workspace="guest",
                    branch="factory/task",
                ),
                AgentTurn(
                    session_id=2,
                    seq=1,
                    prompt="fix",
                    result_text="done",
                    list_cost_usd=995,
                ),
            ]
        )
    with Session(database) as db:
        totals = meter.objective_totals(db, db.get(FactoryReceipt, 838))
        assert totals["known_list_usd"] == 1000


def test_wrong_credential_and_unsupported_adapter_never_bind(database):
    receipt = prepare()
    with pytest.raises(ValueError, match="invalid_receipt_token"):
        update({**receipt, "token": "wrong"}, 0)
    with Session(database) as db, db.begin():
        db.get(AgentSession, 1).model = "opus"
    assert "runtime_meter" not in prepare()


def paused(database):
    with Session(database) as db, db.begin():
        db.get(FactoryReceipt, 838).task_paused = True
        start = db.exec(select(FactoryStart)).one()
        start.status = "failed"
        start.cost_usd = 4
        db.delete(db.exec(select(PendingMessage)).one())


def test_exact_policy_amendment_is_reviewable_idempotent_and_stays_paused(database):
    from factory.orchestration import factory_policy_amendment as amendments
    from factory.orchestration.factory_funding_limits import effective_policy, pending

    paused(database)
    with Session(database) as db, db.begin():
        controls._audit(
            db,
            "factory",
            "funding_review_requested",
            task_id="task",
            node_key="stale_review",
            start_key="stale",
            deadline_at="2099-01-01T00:00:00+00:00",
        )
    overlay = {
        "task_budget_usd": 65,
        "max_task_turns_hard": 12,
        "max_planner_turns": 10,
    }
    direction = "Replace the retired chain atomically with implementation and independent review."
    proposal = amendments.preview(838, overlay, direction)
    assert proposal["remaining_task_allocation_usd"] == 61
    with Session(database) as db:
        assert db.get(SwarmTask, "task").budget_usd == 50
    args = dict(
        actor="operator",
        request_key="one",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    applied = amendments.apply(838, overlay, direction, **args)
    assert applied["stays_paused"]
    assert amendments.apply(838, overlay, direction, **args) == applied
    with Session(database) as db:
        owner = db.get(FactoryReceipt, 838)
        policy = effective_policy(db, owner)
        assert owner.task_paused and owner.task_id == "task"
        assert policy["task_budget_usd"] == 65
        assert policy["max_task_turns_hard"] == 12
        assert policy["max_planner_turns"] == 10
        assert policy["auto_merge"] is False
        assert policy["allowed_models"] == ["astra"]
        assert pending(db, "task") is None
        assert db.exec(select(FactoryStart)).one().cost_usd == 4
    with pytest.raises(ValueError, match="conflicting_policy_amendment_request"):
        amendments.apply(838, {"task_budget_usd": 66}, direction, **args)


@pytest.fixture
def expired_recovery(database, monkeypatch):
    from factory.orchestration import factory_conductor as conductor

    paused(database)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(controls, "_now", lambda: now)
    monkeypatch.setattr(conductor, "get_engine", lambda: database)
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        row.state = "escalated"
        row.escalation_json = json.dumps({"question": "Expired", "resolved": None})
        row.direction_json = json.dumps({"conductor_gates": [{"scope": "draft only"}]})
        task = db.get(SwarmTask, "task")
        task.created_at = now - timedelta(hours=3)
        task.settled_at = now - timedelta(minutes=5)
        task.start_state = "settled"
    issue = {
        "number": 7001,
        "html_url": "https://github.com/org/repo/issues/7001",
        "state": "open",
        "labels": [],
        "body": "fix",
    }
    monkeypatch.setattr(conductor, "github_get", lambda *_: issue)
    return (
        {
            "receipt_id": 838,
            "deadline_at": (now + timedelta(hours=1)).isoformat(),
            "max_work_turns": 9,
            "direction": "Replace the failed preflight with a complete reviewed draft plan.",
        },
        issue,
        now,
    )


def test_expired_recovery_preserves_identity_policy_history_and_stays_paused(
    database, expired_recovery
):
    from factory.orchestration import factory_task_recovery as recovery

    args, _, now = expired_recovery
    proposal = recovery.preview(**args)
    assert proposal["planner_turn_ceiling"] == 2
    assert proposal["accounting"]["committed_cost_usd"] == 4
    with Session(database) as db:
        original = db.get(FactoryReceipt, 838)
        policy, created = original.policy_json, original.created_at
    apply_args = {
        "actor": "operator",
        "request_key": "recover-once",
        "expected_preview_sha256": proposal["preview_sha256"],
    }
    result = recovery.apply(**args, **apply_args)
    assert recovery.apply(**args, **apply_args) == result
    assert result["stays_paused"] and result["task_id"] == "task"
    snapshot = controls.task_snapshot("task")
    assert snapshot["deadline_at"] == args["deadline_at"]
    assert snapshot["deadline_reason"] == "operator_recovery_deadline"
    with Session(database) as db:
        row = db.get(FactoryReceipt, 838)
        task = db.get(SwarmTask, "task")
        assert row.task_id == "task" and row.state == "admitted" and row.task_paused
        assert row.policy_json == policy and row.created_at == created
        assert task.created_at == (now - timedelta(hours=3)).replace(tzinfo=None)
        assert task.settled_at is None and task.budget_usd == 50
        direction = json.loads(row.direction_json)
        assert direction["landing_hold"] and direction["nonrenewable_recovery"]
        assert direction["conductor_gates"] == [{"scope": "draft only"}]
        saved = recovery.grant(db, "task")
        assert saved == proposal
        assert saved["prior_escalation"]["resolved"] is None
        assert saved["prior_direction"] == {
            "conductor_gates": [{"scope": "draft only"}]
        }
        assert db.exec(select(FactoryStart)).one().cost_usd == 4
    with pytest.raises(ValueError, match="conflicting_task_recovery_request"):
        recovery.apply(**{**args, "direction": "different"}, **apply_args)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("human", "recovery_issue_unavailable"),
        ("closed", "recovery_issue_unavailable"),
        ("unknown", "recovery_unresolved"),
        ("queued", "recovery_pending_execution"),
        ("claimed", "recovery_pending_execution"),
        ("policy", "task_recovery_preview_changed"),
        ("body", "task_recovery_preview_changed"),
        ("issue_body", "task_recovery_preview_changed"),
        ("control", "task_recovery_preview_changed"),
        ("owner", "recovery_work_owned_elsewhere"),
        ("capacity", "recovery_capacity_unavailable"),
    ],
)
def test_expired_recovery_rechecks_fresh_authority(
    database, expired_recovery, change, reason
):
    from factory.orchestration import factory_task_recovery as recovery

    args, issue, _ = expired_recovery
    proposal = recovery.preview(**args)
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        if change == "human":
            issue["labels"] = [{"name": "human"}]
        elif change == "closed":
            issue["state"] = "closed"
        elif change == "unknown":
            db.exec(select(FactoryStart)).one().status = "uncertain"
        elif change in {"queued", "claimed"}:
            db.add(
                PendingMessage(
                    session_id=1,
                    seq=2,
                    message_text="old work",
                    claimed_by_replica="owner" if change == "claimed" else None,
                )
            )
        elif change == "policy":
            policy = json.loads(row.policy_json)
            policy["turn_budget_usd"] = 3
            row.policy_json = json.dumps(policy)
        elif change == "body":
            row.body = "changed"
        elif change == "issue_body":
            issue["body"] = "changed"
        elif change == "control":
            db.get(FactoryControl, "factory").version += 1
        else:
            db.add(
                FactoryReceipt(
                    repo="org/repo",
                    issue_number=7001 if change == "owner" else 7002,
                    generation=1,
                    title="other",
                    body="other",
                    url="https://example.invalid",
                    actor="operator",
                    state="admitted",
                    task_id="other",
                )
            )
    with pytest.raises(ValueError, match=reason):
        recovery.apply(
            **args,
            actor="operator",
            request_key="recover",
            expected_preview_sha256=proposal["preview_sha256"],
        )
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).state == "escalated"


def test_recovery_fences_old_retry_until_new_plan_and_enforces_deadline(
    database, expired_recovery, monkeypatch
):
    from factory.orchestration import factory_task_recovery as recovery

    args, _, now = expired_recovery
    proposal = recovery.preview(**args)
    recovery.apply(
        **args,
        actor="operator",
        request_key="recover",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    assert controls.set_control("resume_task", "operator", task_id="task")["ok"]
    assert (
        controls.can_start("task", start_key="factory-node:task:implement:2")["reason"]
        == "operator_recovery_requires_plan"
    )
    planner_key = f"factory-node:task:{proposal['planner_node_key']}:1"
    assert controls.can_start("task", start_key=planner_key)["ok"]
    assert (
        controls.can_start("task", start_key=planner_key[:-1] + "2")["reason"]
        == "operator_recovery_requires_plan"
    )
    assert controls.authorize_start(
        "task", planner_key, "factory", model="astra", max_cost_usd=4
    )["ok"]
    with Session(database) as db, db.begin():
        start = db.exec(
            select(FactoryStart).where(FactoryStart.start_key == planner_key)
        ).one()
        start.status, start.cost_usd = "succeeded", 1
        db.add(
            SwarmPlanVersion(
                task_id="task",
                version=1,
                op="apply_edits",
                author_kind="conductor",
                author="astra",
                change_json="{}",
                cause_kind="factory_conductor",
                cause_ref=f"factory-decision:{proposal['planner_node_key']}:1",
            )
        )
    assert controls.can_start("task", start_key="factory-node:task:implement:2")["ok"]
    monkeypatch.setattr(controls, "_now", lambda: now + timedelta(hours=2))
    assert controls.task_snapshot("task")["limits"]["deadline_expired"]
    assert (
        controls.can_start("task", start_key=planner_key)["reason"] == "task_deadline"
    )


@pytest.mark.parametrize("planner", [True, False])
def test_recovery_cumulative_caps_cannot_be_bypassed(
    database, expired_recovery, planner
):
    from factory.orchestration import factory_task_recovery as recovery

    args, _, _ = expired_recovery
    proposal = recovery.preview(**args)
    recovery.apply(
        **args,
        actor="operator",
        request_key="recover",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    assert controls.set_control("resume_task", "operator", task_id="task")["ok"]
    with Session(database) as db, db.begin():
        db.add(
            SwarmPlanVersion(
                task_id="task",
                version=1,
                op="apply_edits",
                author_kind="conductor",
                author="astra",
                change_json="{}",
                cause_kind="factory_conductor",
                cause_ref=f"factory-decision:{proposal['planner_node_key']}:1",
            )
        )
        starts = [
            FactoryStart(
                task_id="task",
                start_key=f"factory-node:task:{'conductor' if planner else 'implement'}_{i}:1",
                actor="factory",
                model="astra",
                max_cost_usd=4,
                cost_usd=1,
                status="succeeded",
            )
            for i in range(2 if planner else 8)
        ]
        db.add_all(starts)
    key = f"factory-node:task:{'conductor' if planner else 'implement'}_next:1"
    result = controls.authorize_start(
        "task", key, "factory", model="astra", max_cost_usd=4
    )
    assert result == {"ok": False, "reason": "operator_recovery_turn_limit"}


def test_expired_recovery_cannot_renew_after_second_settlement(
    database, expired_recovery, monkeypatch
):
    from factory.orchestration import factory_task_recovery as recovery

    args, _, now = expired_recovery
    proposal = recovery.preview(**args)
    recovery.apply(
        **args,
        actor="operator",
        request_key="recover",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    later = now + timedelta(hours=2)
    monkeypatch.setattr(controls, "_now", lambda: later)
    with Session(database) as db, db.begin():
        db.get(FactoryReceipt, 838).state = "escalated"
        db.get(SwarmTask, "task").settled_at = later
    with pytest.raises(ValueError, match="recovery_already_authorized"):
        recovery.preview(
            **{**args, "deadline_at": (later + timedelta(hours=1)).isoformat()}
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_work_turns", 10),
        ("max_work_turns", 1),
        ("deadline_at", "2099-01-01T00:00:00+00:00"),
        ("deadline_at", "2026-01-01T00:00:00"),
    ],
)
def test_recovery_refuses_unbounded_request(database, expired_recovery, field, value):
    from factory.orchestration import factory_task_recovery as recovery

    args, _, _ = expired_recovery
    with pytest.raises(
        ValueError, match="recovery_(turn_bounds_unavailable|deadline_outside_window)"
    ):
        recovery.preview(**{**args, field: value})


@pytest.mark.parametrize("change", ["control", "graph", "cost", "live", "unknown"])
def test_policy_preview_changes_or_unresolved_work_prevent_application(
    database, change
):
    from factory.orchestration import factory_policy_amendment as amendments

    paused(database)
    proposal = amendments.preview(838, {"task_budget_usd": 65}, "continue")
    with Session(database) as db, db.begin():
        if change == "control":
            db.get(FactoryControl, "factory").version += 1
        elif change == "graph":
            db.add(
                SwarmPlanVersion(
                    task_id="task",
                    version=1,
                    author_kind="conductor",
                    author="astra",
                    op="add",
                    change_json="{}",
                    cause_kind="condition",
                    cause_ref="new evidence",
                    stated_reason="changed",
                )
            )
        else:
            start = db.exec(select(FactoryStart)).one()
            if change == "cost":
                start.cost_usd = 5
            else:
                start.status = "reserved" if change == "live" else "uncertain"
    with pytest.raises(ValueError, match="preview_changed|unresolved_execution"):
        amendments.apply(
            838,
            {"task_budget_usd": 65},
            "continue",
            actor="operator",
            request_key="amend",
            expected_preview_sha256=proposal["preview_sha256"],
        )


@pytest.mark.parametrize(
    "overlay",
    [
        {"auto_merge": True},
        {"allowed_models": ["opus"]},
        {"task_budget_usd": 3},
    ],
)
def test_amendment_does_not_expand_provider_authority(database, overlay):
    from factory.orchestration import factory_policy_amendment as amendments

    paused(database)
    with pytest.raises(ValueError):
        amendments.preview(838, overlay, "continue")


def test_amendment_past_the_old_objective_ceiling_is_recorded(database):
    from factory.orchestration import factory_policy_amendment as amendments

    paused(database)
    # Dollars are recorded but never refuse an amendment (#7161).
    proposal = amendments.preview(838, {"task_budget_usd": 201}, "continue")
    assert proposal["policy_overlay"]["task_budget_usd"] == 201
    assert proposal["remaining_task_allocation_usd"] == (
        201 - proposal["committed_cost_usd"]
    )


def test_single_field_amendment_retains_graph_funding_contract(database):
    from factory.orchestration import factory_policy_amendment as amendments
    from factory.orchestration.factory_funding_limits import graph_budget

    paused(database)
    proposal = amendments.preview(838, {"max_planner_turns": 10}, "continue")
    amendments.apply(
        838,
        {"max_planner_turns": 10},
        "continue",
        actor="operator",
        request_key="planner",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    with Session(database) as db:
        assert graph_budget(db, db.get(SwarmTask, "task")) == 50


@pytest.fixture
def held_delivery(database, monkeypatch):
    from factory.orchestration import factory_conductor as conductor

    paused(database)
    direction = {
        "landing_hold": {"actor": "factory:conductor", "reason": "stage only"},
        "conductor_gates": [{"live_checks": ["Observe the first scheduled run"]}],
        "nonrenewable_recovery": {"actor": "operator"},
    }
    with Session(database) as db, db.begin():
        db.get(FactoryReceipt, 838).direction_json = json.dumps(direction)
    pull = {
        "number": 7170,
        "state": "open",
        "draft": True,
        "merged": False,
        "head": {
            "sha": "a" * 40,
            "ref": "factory/task",
            "repo": {"full_name": "org/repo"},
        },
        "base": {"ref": "main", "repo": {"full_name": "org/repo"}},
    }
    monkeypatch.setattr(conductor, "github_get", lambda *_: pull)
    return pull, direction


@pytest.mark.parametrize("merged", [False, True])
def test_landing_release_only_removes_hold_and_is_idempotent(
    database, held_delivery, merged
):
    from factory.orchestration import factory_landing_release as releases

    pull, direction = held_delivery
    if merged:
        pull.update(state="closed", merged=True, merge_commit_sha="b" * 40)
    proposal = releases.preview(838, 7170, "a" * 40, "Operator approved landing")
    with Session(database) as db:
        assert controls.landing_held(db.get(FactoryReceipt, 838))
    args = dict(
        actor="operator",
        request_key="release",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    result = releases.apply(838, 7170, "a" * 40, "Operator approved landing", **args)
    assert result["ok"] and result["stays_paused"]
    assert result["delivery_accepted"] is False
    assert (
        releases.apply(838, 7170, "a" * 40, "Operator approved landing", **args)
        == result
    )
    with pytest.raises(ValueError, match="conflicting_landing_release_request"):
        releases.apply(838, 7171, "a" * 40, "Operator approved landing", **args)
    with Session(database) as db:
        row = db.get(FactoryReceipt, 838)
        remaining = json.loads(row.direction_json)
        assert not controls.landing_held(row)
        assert row.state == "admitted" and row.task_id == "task" and row.task_paused
        assert remaining["conductor_gates"] == direction["conductor_gates"]
        assert remaining["nonrenewable_recovery"] == direction["nonrenewable_recovery"]
        assert remaining["landing_authorization"]["head_sha"] == "a" * 40
        assert db.get(SwarmTask, "task").budget_usd == 50
        assert db.exec(select(FactoryStart)).one().cost_usd == 4
        events = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == releases.ACTION)
        ).all()
        assert len(events) == 1
        assert (
            json.loads(events[0].detail_json)["prior_hold"] == direction["landing_hold"]
        )
        assert (
            db.exec(
                select(FactoryAudit).where(FactoryAudit.action == "finish_task")
            ).first()
            is None
        )


@pytest.mark.parametrize(
    "change",
    [
        "control",
        "graph",
        "direction",
        "pause",
        "state",
        "head",
        "merge",
        "branch",
        "repo",
        "base",
        "pr",
        "cancel",
        "stop",
        "unknown",
        "claim",
        "review_only",
        "live_run",
        "decision",
        "owner",
        "no_hold",
        "closed_unmerged",
        "body",
        "pr_body",
        "pr_draft",
    ],
)
def test_landing_release_rechecks_preview_and_execution(
    database, held_delivery, change, monkeypatch
):
    from factory.orchestration import factory_landing_release as releases

    pull, _ = held_delivery
    proposal = releases.preview(838, 7170, "a" * 40, "approved")
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        if change == "control":
            db.get(FactoryControl, "factory").version += 1
        elif change == "graph":
            db.add(
                SwarmPlanVersion(
                    task_id="task",
                    version=1,
                    author_kind="conductor",
                    author="astra",
                    op="add",
                    change_json="{}",
                    cause_kind="condition",
                    cause_ref="new",
                    stated_reason="changed",
                )
            )
        elif change == "direction":
            row.direction_json = json.dumps({"landing_hold": {"reason": "new hold"}})
        elif change == "pause":
            row.task_paused = False
        elif change == "state":
            row.state = "escalated"
        elif change == "cancel":
            row.cancellation_requested = True
        elif change == "stop":
            db.get(FactoryControl, "factory").state = "stopped"
        elif change == "unknown":
            db.exec(select(FactoryStart)).one().status = "uncertain"
        elif change == "claim":
            db.add(
                PendingMessage(
                    session_id=1,
                    seq=2,
                    message_text="pending",
                    claimed_by_replica="owner",
                )
            )
        elif change == "review_only":
            controls._audit(
                db, "operator", "review_recovery_authorized", task_id="task"
            )
        elif change == "live_run":
            db.add(
                SwarmNodeRun(
                    task_id="task", node_key="work", attempt=1, status="dispatched"
                )
            )
        elif change == "owner":
            db.add(
                SwarmTask(
                    id="other",
                    task_text="other",
                    conductor_model="astra",
                    budget_usd=50,
                )
            )
            db.add(
                FactoryReceipt(
                    id=839,
                    task_id="other",
                    repo="org/repo",
                    issue_number=7002,
                    title="other",
                    body="other",
                    url="https://example.invalid",
                    actor="operator",
                    state="admitted",
                    direction_json=json.dumps(
                        {"delivery_branch": "factory/task", "delivery_pr_number": 7170}
                    ),
                )
            )
        elif change == "no_hold":
            row.direction_json = "{}"
        elif change == "body":
            row.body = "changed acceptance"
    if change == "head":
        pull["head"]["sha"] = "c" * 40
    elif change == "merge":
        pull.update(state="closed", merged=True, merge_commit_sha="b" * 40)
    elif change == "branch":
        pull["head"]["ref"] = "factory/other"
    elif change == "repo":
        pull["head"]["repo"]["full_name"] = "other/repo"
    elif change == "base":
        pull["base"]["ref"] = "other"
    elif change == "pr":
        pull["number"] = 7171
    elif change == "closed_unmerged":
        pull["state"] = "closed"
    elif change == "pr_body":
        pull["body"] = "changed acceptance"
    elif change == "pr_draft":
        pull["draft"] = False
    elif change == "decision":
        monkeypatch.setattr(
            "factory.orchestration.factory_decisions.decision_in_flight",
            lambda *_: True,
        )
    with pytest.raises(ValueError):
        releases.apply(
            838,
            7170,
            "a" * 40,
            "approved",
            actor="operator",
            request_key="one",
            expected_preview_sha256=proposal["preview_sha256"],
        )
    with Session(database) as db:
        assert controls.landing_held(db.get(FactoryReceipt, 838)) is (
            change != "no_hold"
        )
        assert (
            db.exec(
                select(FactoryAudit).where(FactoryAudit.action == releases.ACTION)
            ).first()
            is None
        )


def test_late_update_after_receipt_readmission_keeps_original_objective_cost(database):
    receipt = prepare()
    update(receipt, 0)
    with Session(database) as db, db.begin():
        db.get(FactoryReceipt, 838).task_id = "replacement"
        db.add_all(
            [
                SwarmTask(
                    id="replacement",
                    task_text="same issue",
                    conductor_model="astra",
                    budget_usd=50,
                ),
                FactoryAudit(
                    task_id="task",
                    actor="operator",
                    action="admit_next",
                    detail_json='{"receipt_id":838}',
                ),
            ]
        )
    update(receipt, 1, 100000)
    with Session(database) as db:
        assert (
            meter.objective_totals(db, db.get(FactoryReceipt, 838))["known_list_usd"]
            == 5
        )


def test_meter_enrollment_retains_legacy_drain_prefix(database, monkeypatch):
    with Session(database) as db, db.begin():
        db.add(
            AgentTurn(
                session_id=1,
                seq=1,
                prompt="fix",
                result_text="partial",
                terminal_reason="interrupted_for_drain",
                list_cost_usd=499,
            )
        )
    receipt = prepare()
    monkeypatch.setattr(meter, "stop_bound_attempt", lambda _receipt: None)
    assert not update(receipt, 0)["stop"]
    assert update(receipt, 1, 100000)["stop"]
    with Session(database) as db:
        assert (
            meter.objective_totals(db, db.get(FactoryReceipt, 838))["known_list_usd"]
            == 504
        )


def test_late_prefix_update_does_not_double_charge_continuation(database):
    first = prepare()
    update(first, 0)
    update(first, 1, 100000)
    with Session(database) as db, db.begin():
        db.add(
            AgentTurn(
                session_id=1,
                seq=1,
                prompt="fix",
                result_text="partial",
                terminal_reason="interrupted_for_drain",
                list_cost_usd=6,
            )
        )
    second = prepare()
    update(second, 0)
    update(first, 2, 120000)
    update(second, 1, 60000)
    with Session(database) as db:
        assert (
            meter.objective_totals(db, db.get(FactoryReceipt, 838))["known_list_usd"]
            == 9
        )


@pytest.mark.parametrize("output,paused", [(10000000, False), (20000000, True)])
def test_old_session_trip_cannot_pause_replacement_but_objective_limit_can(
    database, output, paused
):
    receipt = prepare()
    update(receipt, 0)
    with Session(database) as db, db.begin():
        db.get(FactoryReceipt, 838).task_id = "replacement"
        db.add_all(
            [
                SwarmTask(
                    id="replacement",
                    task_text="same issue",
                    conductor_model="astra",
                    budget_usd=50,
                ),
                FactoryAudit(
                    task_id="task",
                    actor="operator",
                    action="admit_next",
                    detail_json='{"receipt_id":838}',
                ),
            ]
        )
    assert update(receipt, 1, output)["stop"]
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).task_paused is paused
        assert (
            meter.objective_totals(db, db.get(FactoryReceipt, 838))["known_list_usd"]
            == output * 50 / 1000000
        )


@pytest.mark.parametrize("sample", ["none", "zero", "early", "duplicates"])
def test_missing_or_stale_native_usage_trips_even_after_enrollment_is_disabled(
    database, monkeypatch, sample
):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(controls, "_now", lambda: now)
    receipt = prepare()
    stopped = []
    monkeypatch.setattr(meter, "stop_bound_attempt", stopped.append)
    if sample != "none":
        update(receipt, 0)
    if sample in {"early", "duplicates"}:
        update(receipt, 1, 100)
    now += timedelta(seconds=meter.MAX_USAGE_AGE_SECONDS - 1)
    if sample == "duplicates":
        update(receipt, 2, 100)
    meter.reconcile("task")
    assert stopped == []
    monkeypatch.setenv("FACTORY_RUNTIME_METER_ENABLED", "false")
    now += timedelta(seconds=1)
    meter.reconcile("task")
    assert stopped == [receipt["id"]]
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).task_paused
        assert (
            meter._records_for_receipt(db, receipt["id"], meter.TRIP)[0]["reason"]
            == "meter_usage_stale"
        )
        assert db.exec(select(FactoryStart)).one().status == "reserved"


@pytest.mark.parametrize(
    "proof", ["none", "zero", "early", "sealed", "mismatch", "trip"]
)
def test_success_requires_exact_durable_terminal_usage(database, monkeypatch, proof):
    receipt = prepare()
    monkeypatch.setattr(meter, "stop_bound_attempt", lambda *_: None)
    if proof != "none":
        update(receipt, 0)
    if proof not in {"none", "zero"}:
        update(receipt, 1, 100)
    if proof in {"sealed", "mismatch", "trip"}:
        assert not update(receipt, 2, 100, "complete")["stop"]
    if proof == "trip":
        assert update(receipt, 3, 100, "parent")["stop"]
    value = {
        "result": "complete",
        "terminal_reason": "completed",
        "usage": {
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 101 if proof == "mismatch" else 100,
        },
    }
    # The body remains evidence even if no valid terminal seal arrived.
    result_receipts.capture_result(
        receipt["id"], receipt["token"], json.dumps(value).encode()
    )
    monkeypatch.setenv("FACTORY_RUNTIME_METER_ENABLED", "false")
    with Session(database) as db:
        row = result_receipts._receipt_metadata(db, receipt["id"])
        if proof == "sealed":
            assert result_receipts._result(db, row)
            runtime_meter_evidence.validate_completion(db, receipt["id"], value)
        else:
            with pytest.raises(
                result_receipts.ReceiptRejected, match="meter_terminal_usage_unverified"
            ):
                result_receipts._result(db, row)
            with pytest.raises(
                result_receipts.ReceiptRejected, match="meter_terminal_usage_unverified"
            ):
                runtime_meter_evidence.validate_completion(db, receipt["id"], value)
        runtime_meter_evidence.validate_completion(
            db, receipt["id"], {"terminal_reason": "error", "usage": {}}
        )


def test_late_new_usage_cannot_erase_freshness_failure(database, monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(controls, "_now", lambda: now)
    monkeypatch.setattr(meter, "stop_bound_attempt", lambda *_: None)
    receipt = prepare()
    update(receipt, 0)
    now += timedelta(seconds=meter.MAX_USAGE_AGE_SECONDS)
    assert update(receipt, 1, 100)["stop"]


@pytest.mark.parametrize(
    "model,scope",
    [("opus", "task"), ("sol", "task"), ("astra", "another"), ("astra", "")],
)
def test_unenrolled_factory_adapters_keep_existing_receipt_behavior(
    database, monkeypatch, model, scope
):
    monkeypatch.setenv("FACTORY_RUNTIME_METER_TASK_IDS", scope)
    with Session(database) as db, db.begin():
        db.get(AgentSession, 1).model = model
    receipt = prepare()
    assert "runtime_meter" not in receipt
    with Session(database) as db:
        assert meter._records_for_receipt(db, receipt["id"], meter.BOUND) == []


@pytest.mark.parametrize("hold", ["landing_hold", "nonrenewable_recovery"])
def test_receipt_hold_is_sticky_without_changing_budget_or_other_direction(
    database,
    hold,
):
    from factory.orchestration import factory_policy_amendment as amendments
    from factory.orchestration import factory_decisions as decisions

    paused(database)
    direction = {
        "conductor_gates": [{"id": "live-validation"}],
        "delivery_target_checked": True,
    }
    with Session(database) as db, db.begin():
        db.get(FactoryReceipt, 838).direction_json = json.dumps(direction)
    for key, overlay in (
        ("hold", {hold: True}),
        ("later", {"max_planner_turns": 10}),
    ):
        proposal = amendments.preview(
            838, overlay, "Leave the reviewed PR unmerged with auto-merge off."
        )
        assert proposal[hold] is True
        amendments.apply(
            838,
            overlay,
            proposal["direction"],
            actor="operator",
            request_key=key,
            expected_preview_sha256=proposal["preview_sha256"],
        )
    with Session(database) as db:
        row = db.get(FactoryReceipt, 838)
        stored = json.loads(row.direction_json)
        assert all(stored[key] == value for key, value in direction.items())
        assert stored[hold]["request_key"] == "hold"
        assert row.task_paused
        assert getattr(controls, "landing_held" if hold == "landing_hold" else hold)(
            row
        )
        assert db.get(SwarmTask, "task").budget_usd == 50
        renewed = decisions._direction(
            row,
            {},
            {"key": "chat", "label": "continue", "effect": "chat"},
            "operator",
            "continue",
        )
        assert renewed[hold] == stored[hold]
    with pytest.raises(ValueError, match=f"{hold}_can_only_be_added"):
        amendments.preview(838, {hold: False}, "remove hold")


def test_delayed_terminal_replay_keeps_completed_proof_and_conflicts_still_trip(
    database, monkeypatch
):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(controls, "_now", lambda: now)
    monkeypatch.setattr(meter, "stop_bound_attempt", lambda *_: None)
    receipt = prepare()
    update(receipt, 0)
    update(receipt, 1, 100)
    assert not update(receipt, 2, 100, "complete")["stop"]
    now += timedelta(seconds=meter.MAX_USAGE_AGE_SECONDS * 2)
    assert not update(receipt, 2, 100, "complete")["stop"]
    meter.reconcile("task")
    with Session(database) as db:
        assert not db.get(FactoryReceipt, 838).task_paused
        assert len(meter._records_for_receipt(db, receipt["id"], meter.UPDATE)) == 3
    assert update(receipt, 2, 101, "complete")["stop"]
