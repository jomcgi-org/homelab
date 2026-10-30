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
        snapshot = controls._snapshot(db, db.get(FactoryReceipt, 838))
        card = controls.escalation_view(snapshot)
        assert card["open"] and not card["briefing"] and card["options"]
        assert "factory:runtime-meter" in card["question"]
        assert (
            card["reason"]
            == meter._records_for_receipt(db, receipt["id"], meter.TRIP)[0]["reason"]
        )
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


def test_old_attempt_session_trip_still_pauses_same_task(database):
    first = prepare()
    update(first, 0)
    second = prepare()
    assert first["id"] != second["id"]

    assert update(first, 1, 10000000)["stop"]

    with Session(database) as db:
        owner = db.get(FactoryReceipt, 838)
        assert owner.task_paused
        snapshot = controls._snapshot(db, owner)
        assert snapshot["pause_warning"] is None
        assert snapshot["escalation"]["actor"] == "factory:runtime-meter"
        assert snapshot["escalation"]["reason"] == "session_list_backstop"
        assert meter._records_for_receipt(db, first["id"], meter.TRIP)
        assert not meter._records_for_receipt(db, second["id"], meter.TRIP)
    with pytest.raises(ValueError, match="executor_ownership_changed"):
        meter.stop_bound_attempt(first["id"])


@pytest.mark.parametrize("path", ["ingest", "reconcile"])
@pytest.mark.parametrize("state", ["accepted", "outcome_unknown", "legacy_claim"])
def test_meter_pauses_unpaused_task_while_decision_is_in_flight(
    database, monkeypatch, path, state
):
    from factory.orchestration import factory_decisions as decisions, factory_pause

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(controls, "_now", lambda: now)
    receipt = prepare()
    update(receipt, 0)
    factory_pause.pause("task", "test:original", "original reason")
    before = controls.task_snapshot("task")
    with controls._locked_session() as (db, _control):
        if state == "legacy_claim":
            decisions._claim(838, "stop", "operator", session=db)
        else:
            decisions._request_event(
                db,
                "operator",
                {"receipt_id": 838, "request_key": "pending"},
                {"state": state},
            )
    assert controls.set_control("resume_task", "operator", task_id="task")["ok"]
    assert not controls.task_snapshot("task")["task_paused"]
    stopped = []
    monkeypatch.setattr(meter, "stop_bound_attempt", stopped.append)

    if path == "ingest":
        assert update(receipt, 1, 10000000)["stop"]
    else:
        now += timedelta(seconds=meter.MAX_USAGE_AGE_SECONDS)
        meter.reconcile("task")

    assert stopped == [receipt["id"]]
    after = controls.task_snapshot("task")
    assert after["task_paused"]
    assert after["escalation"] == before["escalation"]
    assert after["pause_warning"] == "active_paused_without_actionable_question"
    assert not after["paused_card_actionable"]
    with Session(database) as db:
        assert meter._records_for_receipt(db, receipt["id"], meter.TRIP)
        assert db.exec(select(FactoryStart)).one().status == "reserved"


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


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("start_state", [None, "succeeded", "reserved", "uncertain"])
def test_expired_recovery_uses_released_same_repo_capacity(
    database, expired_recovery, held, start_state
):
    from factory.orchestration import factory_task_recovery as recovery

    args, _issue, _now = expired_recovery
    with Session(database) as db, db.begin():
        db.add_all(
            [
                SwarmTask(
                    id="other",
                    task_text="other",
                    conductor_model="astra",
                    budget_usd=50,
                ),
                FactoryReceipt(
                    task_id="other",
                    repo="org/repo",
                    issue_number=7002,
                    generation=0,
                    title="other",
                    body="",
                    url="",
                    actor="operator",
                    state="admitted",
                    task_paused=not held,
                    direction_json=json.dumps({"landing_hold": None} if held else {}),
                ),
            ]
        )
        db.flush()
        if start_state:
            db.add(
                FactoryStart(
                    task_id="other",
                    start_key="other",
                    actor="test",
                    model="astra",
                    max_cost_usd=1,
                    status=start_state,
                )
            )
    if start_state in ("reserved", "uncertain"):
        with pytest.raises(ValueError, match="recovery_capacity_unavailable"):
            recovery.preview(**args)
    else:
        proposal = recovery.preview(**args)
        assert proposal["receipt_id"] == 838


def test_expired_recovery_preserves_identity_policy_history_and_stays_paused(
    database, expired_recovery, monkeypatch
):
    from factory.orchestration import factory_conductor as conductor, graph
    from factory.orchestration import factory_task_recovery as recovery

    args, _, now = expired_recovery
    monkeypatch.setattr(graph, "get_engine", lambda: database)
    with Session(database) as db, db.begin():
        db.add(
            FactoryAudit(
                actor=conductor.ACTOR,
                action="pause_task",
                task_id="task",
                created_at=now - timedelta(hours=3),
                detail_json=json.dumps({"ok": True, "reason": "old conductor pause"}),
            )
        )
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
    conductor.reconcile_task("task", snapshot["policy"], object())
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
        pauses = db.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action == "pause_task")
            .order_by(FactoryAudit.id)
        ).all()
        assert [pause.actor for pause in pauses][-2:] == [conductor.ACTOR, "operator"]
        assert (
            json.loads(pauses[-1].detail_json)["recovery_request_key"] == "recover-once"
        )
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
        ("landing_deadline", "recovery_exceeds_landing_deadline"),
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
        elif change == "landing_deadline":
            controls._audit(
                db,
                "factory",
                "landing_recovery_requested",
                task_id="task",
                deadline_at=(
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
            )
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


@pytest.mark.parametrize("constraint", ["lane", "chart", "repo"])
@pytest.mark.parametrize("intent_shape", ["hold", "resume", "combined"])
@pytest.mark.parametrize("pause_history", ["unpaused", "before", "after"])
@pytest.mark.parametrize(
    "outcome", ["succeeded", "escalated", "failed", "cancelled", "human", "retry"]
)
def test_capacity_release_intent_matrix(
    database,
    held_delivery,
    monkeypatch,
    constraint,
    intent_shape,
    pause_history,
    outcome,
):
    from factory.orchestration import factory_landing_release as releases

    monkeypatch.setenv(
        "FACTORY_MAX_CONCURRENT_TASKS", "1" if constraint == "chart" else "2"
    )
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        row.task_paused = False
        control = db.get(FactoryControl, "factory")
        policy = json.loads(control.policy_json)
        if constraint != "lane":
            policy["max_tasks"] = 2
        if constraint == "repo":
            policy["repos"] = {"org/repo": {"max_tasks": 1}}
        control.policy_json = json.dumps(policy)
        db.add(
            SwarmTask(
                id="other", task_text="other", conductor_model="astra", budget_usd=50
            )
        )
        db.flush()
        db.add(
            FactoryReceipt(
                task_id="other",
                repo="org/repo",
                issue_number=7002,
                title="other",
                body="",
                url="",
                actor="test",
                state="admitted",
                policy_json=json.dumps(policy),
            )
        )
    has_hold = intent_shape != "resume"
    has_resume = intent_shape != "hold"
    if pause_history == "before":
        assert controls.set_control("pause_task", "operator", task_id="task")["ok"]
    resume_args = dict(
        task_id="task",
        request_key="resume-waiting",
        expected_version=controls.status()["version"],
    )
    if has_resume:
        resumed = controls.request_control("resume_task", "operator", **resume_args)
        assert resumed["ok"] and resumed["pending"]
    if has_hold:
        proposal = releases.preview(838, 7170, "a" * 40, "Approved exact delivery")
        args = dict(
            actor="operator",
            request_key="release-waiting",
            expected_preview_sha256=proposal["preview_sha256"],
        )
        result = releases.apply(838, 7170, "a" * 40, "Approved exact delivery", **args)
        assert result["ok"]
        # A hold-only release on an already paused receipt needs no slot.
        assert result["pending"] == (has_resume or pause_history != "before")
        assert (
            releases.apply(838, 7170, "a" * 40, "Approved exact delivery", **args)
            == result
        )
    with Session(database) as db:
        intent = json.loads(db.get(FactoryReceipt, 838).direction_json).get(
            "pending_capacity_release"
        )
    if pause_history == "after":
        assert controls.set_control("pause_task", "operator", task_id="task")["ok"]
    assert controls.retry_capacity_releases() == 0
    refused_success = outcome == "succeeded" and pause_history != "unpaused"
    settles_hold = (
        has_hold
        and intent is not None
        and pause_history != "after"
        and outcome in ("succeeded", "escalated", "failed")
        and not refused_success
    )
    retry_applies = (
        outcome == "retry" and intent is not None and pause_history != "after"
    )
    immediate = has_hold and intent is None
    if outcome == "retry":
        with Session(database) as db, db.begin():
            controls._receipt(db, "other").state = "failed"
        assert controls.retry_capacity_releases() == int(retry_applies)
    else:
        evidence = {"state": "ready_for_review"}
        settled_outcome = outcome
        if outcome == "human":
            assert controls.human_handoff("task", "operator", observed=True)
            evidence = controls.HUMAN_HANDOFF_EVIDENCE
            settled_outcome = "cancelled"
        finished = controls.finish_task(
            "task", settled_outcome, "test", evidence=evidence
        )
        if refused_success:
            # The matrix must preserve the separate paused-success proof gate.
            assert finished == {"ok": False, "reason": "task_paused"}
        else:
            assert finished == {"ok": True, "state": settled_outcome}
            assert controls.finish_task(
                "task", settled_outcome, "test", evidence=evidence
            )["ok"]
    hold_applied = immediate or settles_hold or (retry_applies and has_hold)
    assert controls.retry_capacity_releases() == 0
    with Session(database) as db:
        audit_count = len(db.exec(select(FactoryAudit)).all())
        row = db.get(FactoryReceipt, 838)
        assert controls.landing_held(row) == (not hold_applied)
        assert row.task_paused == (
            pause_history != "unpaused" and not (retry_applies and has_resume)
        )
        pending = json.loads(row.direction_json).get("pending_capacity_release")
        assert pending == (
            intent if refused_success and pause_history == "before" else None
        )
        assert controls._receipt(db, "other").state == (
            "failed" if outcome == "retry" else "admitted"
        )
        events = db.exec(
            select(FactoryAudit)
            .where(
                FactoryAudit.task_id == "task",
                FactoryAudit.action.in_(
                    [
                        "settled_landing_hold_released",
                        "execution_capacity_reacquired",
                        "execution_capacity_release_revoked",
                    ]
                ),
            )
            .order_by(FactoryAudit.id)
        ).all()
        applied = [
            e for e in events if e.action != "execution_capacity_release_revoked"
        ]
        revoked = [
            e for e in events if e.action == "execution_capacity_release_revoked"
        ]
        assert len(applied) == int(immediate or settles_hold or retry_applies)
        if applied:
            assert applied[0].action == (
                "settled_landing_hold_released"
                if settles_hold
                else "execution_capacity_reacquired"
            )
            detail = json.loads(applied[0].detail_json)
            assert applied[0].actor == "operator"
            if intent is not None:
                assert detail["intent"] == intent
            if has_hold:
                assert (
                    detail["landing_authorization"]["request_key"]
                    == args["request_key"]
                )
                assert (
                    detail["intent"]["landing_hold"] == held_delivery[1]["landing_hold"]
                )
        must_revoke = intent is not None and (
            pause_history == "after"
            or (
                outcome != "retry"
                and not refused_success
                and (not settles_hold or has_resume)
            )
        )
        assert len(revoked) == int(must_revoke)
        if revoked:
            assert json.loads(revoked[0].detail_json) == {
                "reason": "pause_task" if pause_history == "after" else "task_settled",
                "intent": {k: v for k, v in intent.items() if k != "landing_hold"}
                if settles_hold
                else intent,
            }
    if has_hold:
        # Replays acknowledge application through either path. Revoked requests
        # keep their original acknowledgement and never recreate an intent.
        expected = (
            {**result, "pending": False, "reason": None} if hold_applied else result
        )
        assert (
            releases.apply(838, 7170, "a" * 40, "Approved exact delivery", **args)
            == expected
        )
    if has_resume:
        assert (
            controls.request_control("resume_task", "operator", **resume_args)
            == resumed
        )
    with Session(database) as db:
        assert (
            json.loads(db.get(FactoryReceipt, 838).direction_json).get(
                "pending_capacity_release"
            )
            == pending
        )
        assert len(db.exec(select(FactoryAudit)).all()) == audit_count


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


def released_delivery(database, pull):
    from factory.orchestration import factory_landing_release as releases

    pull["head"]["sha"] = "b" * 40
    proposal = releases.preview(838, 7170, "b" * 40, "initial release")
    result = releases.apply(
        838,
        7170,
        "b" * 40,
        "initial release",
        actor="operator",
        request_key="initial",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    with Session(database) as db, db.begin():
        db.get(FactoryReceipt, 838).state = "escalated"
    pull["head"]["sha"] = "a" * 40
    return result


@pytest.mark.parametrize("receipt_state", ["admitted", "escalated"])
def test_landing_authorization_refresh_preserves_state_and_replays(
    database, held_delivery, receipt_state, monkeypatch
):
    from factory.orchestration import factory_landing_release as releases

    pull, original = held_delivery
    released_delivery(database, pull)
    with Session(database) as db, db.begin():
        db.get(FactoryReceipt, 838).state = receipt_state

    def no_reacquisition(*args, **kwargs):
        pytest.fail("authorization refresh must not request execution capacity")

    monkeypatch.setattr(controls, "request_capacity_release", no_reacquisition)
    with Session(database) as db:
        before = json.loads(db.get(FactoryReceipt, 838).direction_json)
    proposal = releases.preview(838, 7170, "a" * 40, "reviewed rebase")
    assert proposal["operation"] == "refresh"
    args = dict(
        actor="operator",
        request_key="refresh",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    result = releases.apply(838, 7170, "a" * 40, "reviewed rebase", **args)
    assert result["operation"] == "refresh" and not result["delivery_accepted"]
    assert releases.apply(838, 7170, "a" * 40, "reviewed rebase", **args) == result
    with pytest.raises(ValueError, match="conflicting_landing_release_request"):
        releases.apply(838, 7170, "c" * 40, "reviewed rebase", **args)
    with Session(database) as db:
        row = db.get(FactoryReceipt, 838)
        after = json.loads(row.direction_json)
        assert row.state == receipt_state and row.task_paused and row.task_id == "task"
        assert not controls.landing_held(row)
        assert after["landing_authorization"]["head_sha"] == "a" * 40
        assert after["conductor_gates"] == original["conductor_gates"]
        assert after["nonrenewable_recovery"] == original["nonrenewable_recovery"]
        assert {k: v for k, v in after.items() if k != "landing_authorization"} == {
            k: v for k, v in before.items() if k != "landing_authorization"
        }
        assert db.get(SwarmTask, "task").budget_usd == 50
        events = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == releases.ACTION)
        ).all()
        assert len(events) == 2
        event = next(
            json.loads(e.detail_json)
            for e in events
            if json.loads(e.detail_json)["request"]["request_key"] == "refresh"
        )
        assert event["prior_authorization"] == before["landing_authorization"]
        assert event["prior_hold"] is None
    # A second rebase uses the refresh audit as its predecessor.
    pull["head"]["sha"] = "c" * 40
    assert (
        releases.preview(838, 7170, "c" * 40, "next rebase")["operation"] == "refresh"
    )


def test_landing_refresh_competing_previews_cannot_overwrite_authorization(
    database, held_delivery
):
    from factory.orchestration import factory_landing_release as releases

    pull, _ = held_delivery
    released_delivery(database, pull)
    first = releases.preview(838, 7170, "a" * 40, "refresh")
    second = releases.preview(838, 7170, "a" * 40, "refresh")
    assert first == second
    result = releases.apply(
        838,
        7170,
        "a" * 40,
        "refresh",
        actor="operator",
        request_key="first",
        expected_preview_sha256=first["preview_sha256"],
    )
    with pytest.raises(ValueError):
        releases.apply(
            838,
            7170,
            "a" * 40,
            "refresh",
            actor="operator",
            request_key="second",
            expected_preview_sha256=second["preview_sha256"],
        )
    with Session(database) as db:
        authorization = json.loads(db.get(FactoryReceipt, 838).direction_json)[
            "landing_authorization"
        ]
        assert authorization["request_key"] == "first"
        assert db.get(FactoryControl, "factory").version == result["version"]
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(FactoryAudit.action == releases.ACTION)
                ).all()
            )
            == 2
        )


@pytest.mark.parametrize(
    "change", ["same_head", "wrong_pr", "wrong_receipt", "missing_audit", "actor"]
)
def test_landing_refresh_requires_matching_release_history(
    database, held_delivery, change
):
    from factory.orchestration import factory_landing_release as releases

    pull, _ = held_delivery
    released_delivery(database, pull)
    number, head = 7170, "a" * 40
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        direction = json.loads(row.direction_json)
        event = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == releases.ACTION)
        ).one()
        if change == "same_head":
            head = pull["head"]["sha"] = "b" * 40
        elif change == "wrong_pr":
            number = pull["number"] = 7171
        elif change == "wrong_receipt":
            detail = json.loads(event.detail_json)
            detail["request"]["receipt_id"] = 839
            event.detail_json = json.dumps(detail)
        elif change == "missing_audit":
            db.delete(event)
        elif change == "actor":
            direction["landing_authorization"]["actor"] = "other"
            row.direction_json = json.dumps(direction)
    with pytest.raises(ValueError, match="landing_release_requires_held_receipt"):
        releases.preview(838, number, head, "refresh")


@pytest.mark.parametrize("refresh", [False, True])
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
    database, held_delivery, change, monkeypatch, refresh
):
    from factory.orchestration import factory_landing_release as releases

    pull, _ = held_delivery
    if refresh:
        released_delivery(database, pull)
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
            row.state = "admitted" if refresh else "escalated"
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
            change == "direction" or (not refresh and change != "no_hold")
        )
        assert len(
            db.exec(
                select(FactoryAudit).where(FactoryAudit.action == releases.ACTION)
            ).all()
        ) == int(refresh)


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
        if paused:
            card = controls.escalation_view(
                controls._snapshot(db, db.get(FactoryReceipt, 838))
            )
            assert (
                card["open"]
                and not card["briefing"]
                and card["reason"] == "objective_list_backstop"
            )
        else:
            assert db.get(FactoryReceipt, 838).escalation_json is None
        assert (
            meter.objective_totals(db, db.get(FactoryReceipt, 838))["known_list_usd"]
            == output * 50 / 1000000
        )


def test_objective_backstop_revokes_pending_resume(database):
    receipt = prepare()
    update(receipt, 0)
    with Session(database) as db, db.begin():
        owner = db.get(FactoryReceipt, 838)
        owner.task_paused = True
        owner.direction_json = json.dumps(
            {
                "landing_hold": {"actor": "operator"},
                "pending_capacity_release": {
                    "task_id": "task",
                    "actor": "operator",
                    "resume": True,
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        )
    assert update(receipt, 1, 20000000)["stop"]
    assert controls.retry_capacity_releases() == 0
    with Session(database) as db:
        owner = db.get(FactoryReceipt, 838)
        assert owner.task_paused
        assert json.loads(owner.direction_json) == {
            "landing_hold": {"actor": "operator"}
        }
        event = db.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "execution_capacity_release_revoked"
            )
        ).one()
        assert json.loads(event.detail_json)["reason"] == "runtime_meter_pause"


@pytest.mark.parametrize("sample", ["none", "zero", "early", "duplicates"])
@pytest.mark.parametrize("intent_shape", ["hold", "resume", "combined"])
def test_missing_or_stale_native_usage_trips_even_after_enrollment_is_disabled(
    database, monkeypatch, sample, intent_shape
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
    intent = {"task_id": "task", "actor": "operator", "requested_at": now.isoformat()}
    if intent_shape != "hold":
        intent["resume"] = True
    if intent_shape != "resume":
        intent["landing_hold"] = {"actor": "operator"}
    # A stale-meter pause must fence intent even on an already paused owner.
    with Session(database) as db, db.begin():
        owner = db.get(FactoryReceipt, 838)
        owner.task_paused = True
        owner.direction_json = json.dumps(
            {
                "landing_hold": {"actor": "operator"},
                "pending_capacity_release": intent,
            }
        )
    monkeypatch.setenv("FACTORY_RUNTIME_METER_ENABLED", "false")
    now += timedelta(seconds=1)
    meter.reconcile("task")
    assert stopped == [receipt["id"]]
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).task_paused
        card = controls.escalation_view(
            controls._snapshot(db, db.get(FactoryReceipt, 838))
        )
        assert card["open"] and not card["briefing"] and card["options"]
        assert card["reason"] == "meter_usage_stale"
        owner = db.get(FactoryReceipt, 838)
        assert owner.task_paused and controls.landing_held(owner)
        assert "pending_capacity_release" not in json.loads(owner.direction_json)
        revoked = db.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "execution_capacity_release_revoked"
            )
        ).one()
        assert revoked.actor == "factory:runtime-meter"
        assert json.loads(revoked.detail_json) == {
            "reason": "runtime_meter_pause",
            "intent": intent,
        }
        assert (
            meter._records_for_receipt(db, receipt["id"], meter.TRIP)[0]["reason"]
            == "meter_usage_stale"
        )
        assert db.exec(select(FactoryStart)).one().status == "reserved"
    assert controls.retry_capacity_releases() == 0


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


@pytest.fixture
def validation_ready(database, monkeypatch):
    from factory.orchestration import factory_validation as validation

    paused(database)
    monkeypatch.setattr(validation, "_available_repo", lambda repo: repo)
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        row.state = "escalated"
        row.direction_json = json.dumps(
            {
                "landing_hold": {"reason": "review first"},
                "review_only_hold": {"owner": "operator"},
            }
        )
        control = db.get(FactoryControl, "factory")
        control.state = "paused"
        policy = json.loads(control.policy_json)
        policy.update(
            auto_merge=True,
            issue_numbers=[7001, 7002],
            intake={"enabled": True, "refine_enabled": True, "close_enabled": True},
            problem_issues={"enabled": True},
            repos={
                "org/repo": {"auto_merge": True},
                "org/other": {"enabled": True, "auto_merge": True},
            },
        )
        control.policy_json = json.dumps(policy)
    return validation


def test_validation_preparation_is_narrow_audited_and_idempotent(
    database, validation_ready
):
    validation = validation_ready
    proposal = validation.preview(838, "bounded check")
    before, after = proposal["policy_before"], proposal["policy_after"]
    assert before["auto_merge"] and not after["auto_merge"]
    assert after["issue_numbers"] == [7001]
    assert not any(
        after["intake"][k] for k in ("enabled", "refine_enabled", "close_enabled")
    )
    assert not after["problem_issues"]["enabled"]
    assert not after["repos"]["org/repo"]["auto_merge"]
    assert after["repos"]["org/other"]["paused"]
    for key in before.keys() - {
        "auto_merge",
        "issue_numbers",
        "intake",
        "problem_issues",
        "repos",
    }:
        assert before[key] == after[key]
    with Session(database) as db:
        receipt_before = db.get(FactoryReceipt, 838).model_dump()
    args = dict(
        actor="operator",
        request_key="prepare",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    result = validation.apply(838, "bounded check", **args)
    assert result["state"] == "paused" and not result["execution_authorized"]
    assert validation.apply(838, "bounded check", **args) == result
    with pytest.raises(ValueError, match="conflicting_validation_request"):
        validation.apply(838, "different", **args)
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).model_dump() == receipt_before
        control = db.get(FactoryControl, "factory")
        assert control.state == "paused" and json.loads(control.policy_json) == after
        assert db.get(SwarmTask, "task").budget_usd == 50
        events = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == validation.ACTION)
        ).all()
        assert len(events) == 1
        assert json.loads(events[0].detail_json)["policy_before"] == before


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "policy",
        "direction",
        "new_receipt",
        "enabled",
        "active",
        "landing",
        "unknown_start",
        "generation",
        "cancel",
    ],
)
def test_validation_rechecks_concurrent_state(database, validation_ready, change):
    validation = validation_ready
    proposal = validation.preview(838, "bounded check")
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        control = db.get(FactoryControl, "factory")
        if change == "version":
            control.version += 1
        elif change == "policy":
            policy = json.loads(control.policy_json)
            policy["task_budget_usd"] = 60
            control.policy_json = json.dumps(policy)
        elif change == "direction":
            row.direction_json = json.dumps({"landing_hold": {"reason": "new review"}})
        elif change == "new_receipt":
            db.add(
                FactoryReceipt(
                    repo="org/repo",
                    issue_number=7002,
                    title="other",
                    body="other",
                    url="https://example.invalid",
                    actor="operator",
                    state="queued",
                )
            )
        elif change == "enabled":
            control.state = "enabled"
        elif change in ("active", "landing"):
            row.state = "admitted" if change == "active" else "landing"
        elif change == "unknown_start":
            db.exec(select(FactoryStart)).one().status = "uncertain"
        elif change == "generation":
            row.generation += 1
        elif change == "cancel":
            row.cancellation_requested = True
    with pytest.raises(ValueError):
        validation.apply(
            838,
            "bounded check",
            actor="operator",
            request_key="stale",
            expected_preview_sha256=proposal["preview_sha256"],
        )
    with Session(database) as db:
        assert json.loads(db.get(FactoryControl, "factory").policy_json)["auto_merge"]
        assert not db.exec(
            select(FactoryAudit).where(FactoryAudit.action == validation.ACTION)
        ).all()


def test_validation_competing_applications_are_serialized(database, validation_ready):
    validation = validation_ready
    proposal = validation.preview(838, "bounded check")

    def apply(key):
        try:
            return validation.apply(
                838,
                "bounded check",
                actor="operator",
                request_key=key,
                expected_preview_sha256=proposal["preview_sha256"],
            )
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, ["first", "second"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "validation_preview_changed" in results


def test_validation_preserves_implicit_primary_repository(database, validation_ready):
    with Session(database) as db, db.begin():
        control = db.get(FactoryControl, "factory")
        policy = json.loads(control.policy_json)
        del policy["repos"]
        control.policy_json = json.dumps(policy)
    proposal = validation_ready.preview(838, "implicit primary")
    assert "repos" not in proposal["policy_after"]
    result = validation_ready.apply(
        838,
        "implicit primary",
        actor="operator",
        request_key="implicit-primary",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    assert result["ok"] and result["state"] == "paused"
    with Session(database) as db:
        policy = json.loads(db.get(FactoryControl, "factory").policy_json)
        assert controls.enabled_repos(policy) == ["org/repo"]
        assert not policy["auto_merge"]


def test_fresh_issue_preparation_preserves_paused_history(database, validation_ready):
    validation = validation_ready
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        row.state = "escalated"
        row.task_paused = True
    with Session(database) as db:
        before = db.get(FactoryReceipt, 838).model_dump()
    proposal = validation.preview(None, "selected work", issue_number=7275)
    assert proposal["policy_after"]["issue_numbers"] == [7275]
    assert not proposal["policy_after"]["auto_merge"]
    assert proposal["receipt_id"] is None
    args = dict(
        actor="operator",
        request_key="fresh",
        issue_number=7275,
        expected_preview_sha256=proposal["preview_sha256"],
    )
    result = validation.apply(None, "selected work", **args)
    assert validation.apply(None, "selected work", **args) == result
    assert result["state"] == "paused"
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).model_dump() == before
        assert len(db.exec(select(FactoryReceipt)).all()) == 1
    with pytest.raises(ValueError, match="conflicting_validation_request"):
        validation.apply(None, "selected work", **{**args, "issue_number": 7274})


@pytest.mark.parametrize(
    "change", ["admitted_paused", "start", "queued", "landing", "direction", "version"]
)
def test_fresh_issue_rechecks_inventory(database, validation_ready, change):
    validation = validation_ready
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        row.state = "escalated"
        row.task_paused = True
    proposal = validation.preview(None, "selected work", issue_number=7275)
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        if change == "admitted_paused":
            row.state = "admitted"
        elif change == "start":
            db.exec(select(FactoryStart)).one().status = "uncertain"
        elif change in ("queued", "landing"):
            row.state = change
        elif change == "direction":
            row.direction_json = "{}"
        else:
            db.get(FactoryControl, "factory").version += 1
    with pytest.raises(ValueError):
        validation.apply(
            None,
            "selected work",
            issue_number=7275,
            actor="operator",
            request_key="stale-fresh",
            expected_preview_sha256=proposal["preview_sha256"],
        )


@pytest.mark.parametrize(
    "receipt_id,issue_number",
    [(None, None), (838, 7275), (None, 7001), (None, True), (None, 0)],
)
def test_fresh_issue_selector_refusals(validation_ready, receipt_id, issue_number):
    with pytest.raises(ValueError):
        validation_ready.preview(receipt_id, "selected work", issue_number=issue_number)


@pytest.fixture
def completed_refinement(database, validation_ready, monkeypatch):
    from factory.orchestration import factory_conductor as conductor

    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        row.direction_json = "{}"
        row.task_class = "refine"
        row.routing_tier = "advisory"
        row.state = "succeeded"
        task = db.get(SwarmTask, "task")
        task.start_state = "succeeded"
        task.settled_at = datetime.now(timezone.utc)
        controls._audit(
            db,
            "factory:refine",
            "finish_task",
            task_id="task",
            outcome="succeeded",
            evidence={"state": "refine_agent_ready"},
        )
    issue = {
        "number": 7001,
        "html_url": "https://github.com/org/repo/issues/7001",
        "state": "open",
        "labels": [{"name": "agent-ready"}],
        "assignees": [],
        "updated_at": "2026-10-23T18:00:00Z",
    }
    monkeypatch.setattr(conductor, "github_get", lambda *args: dict(issue))
    monkeypatch.setattr(conductor, "github_list", lambda *args: [])
    return validation_ready, issue


def test_completed_refinement_prepares_delivery_without_reviving_receipt(
    database, completed_refinement
):
    validation, _ = completed_refinement
    with Session(database) as db:
        before = db.get(FactoryReceipt, 838).model_dump()
        task_before = db.get(SwarmTask, "task").model_dump()
    preview = validation.preview(838, "delivery after verified refinement")
    assert preview["selection_kind"] == "refined_issue"
    result = validation.apply(
        838,
        "delivery after verified refinement",
        actor="operator",
        request_key="refined-issue",
        expected_preview_sha256=preview["preview_sha256"],
    )
    assert result["state"] == "paused" and not result["execution_authorized"]
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).model_dump() == before
        assert db.get(SwarmTask, "task").model_dump() == task_before
        assert len(db.exec(select(FactoryReceipt)).all()) == 1
        policy = json.loads(db.get(FactoryControl, "factory").policy_json)
        assert policy["issue_numbers"] == [7001]
        assert not policy["auto_merge"]
        for key in ("generation", "task_budget_usd", "max_turns_per_task"):
            assert policy[key] == preview["policy_before"][key]


@pytest.mark.parametrize(
    "change",
    [
        {"state": "closed"},
        {"number": 7002},
        {"html_url": "https://example.invalid/7001"},
        {"labels": []},
        {"labels": ["agent-ready", "human"]},
        {"labels": ["agent-ready", "needs-human"]},
        {"assignees": [{"login": "owner"}]},
        {"assignees": None},
        {"pull_request": {}},
    ],
)
def test_refinement_requires_current_issue_eligibility(completed_refinement, change):
    validation, issue = completed_refinement
    issue.update(change)
    with pytest.raises(ValueError, match="validation_refinement_issue_ineligible"):
        validation.preview(838, "prepare")


@pytest.mark.parametrize(
    "outcome",
    [
        "refine_needs_human",
        "refine_deferred",
        "refine_rejected",
        "refine_stale",
        "refine_failed",
    ],
)
def test_refinement_success_is_not_itself_readiness(
    database, completed_refinement, outcome
):
    validation, _ = completed_refinement
    with Session(database) as db, db.begin():
        audit = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == "finish_task")
        ).one()
        audit.detail_json = json.dumps(
            {"outcome": "succeeded", "evidence": {"state": outcome}}
        )
    with pytest.raises(ValueError, match="validation_refinement_unverified"):
        validation.preview(838, "prepare")


@pytest.mark.parametrize(
    "change",
    [
        "missing_audit",
        "duplicate_audit",
        "wrong_actor",
        "unsettled",
        "cancelled",
        "pending",
        "uncertain",
    ],
)
def test_refinement_requires_authoritative_quiescent_result(
    database, completed_refinement, change
):
    validation, _ = completed_refinement
    with Session(database) as db, db.begin():
        audit = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == "finish_task")
        ).one()
        if change == "missing_audit":
            db.delete(audit)
        elif change == "duplicate_audit":
            controls._audit(
                db,
                "factory:refine",
                "finish_task",
                task_id="task",
                outcome="succeeded",
                evidence={"state": "refine_agent_ready"},
            )
        elif change == "wrong_actor":
            audit.actor = "worker"
        elif change == "unsettled":
            db.get(SwarmTask, "task").settled_at = None
        elif change == "cancelled":
            db.get(FactoryReceipt, 838).cancellation_requested = True
        elif change == "pending":
            db.add(PendingMessage(session_id=1, seq=2, message_text="pending"))
        else:
            start = db.exec(select(FactoryStart)).one()
            start.status = "uncertain"
    with pytest.raises(ValueError, match="validation_refinement_"):
        validation.preview(838, "prepare")


@pytest.mark.parametrize(
    "state", ["queued", "admitted", "escalated", "succeeded", "failed", "cancelled"]
)
def test_refinement_rejects_any_other_delivery_history(
    database, completed_refinement, state
):
    validation, _ = completed_refinement
    with Session(database) as db, db.begin():
        source = db.get(FactoryReceipt, 838).model_dump()
        source.update(
            id=839,
            task_id=None,
            task_class="bugfix",
            routing_tier="delivery",
            state=state,
        )
        db.add(FactoryReceipt(**source))
    with pytest.raises(ValueError, match="validation_refinement_history_ambiguous"):
        validation.preview(838, "prepare")


@pytest.mark.parametrize("kind", ["open", "closed", "truncated", "malformed"])
def test_refinement_rejects_linked_pr_or_incomplete_lookup(
    completed_refinement, monkeypatch, kind
):
    from factory.orchestration import factory_conductor as conductor

    validation, _ = completed_refinement

    def lookup(repo, endpoint):
        if kind == "open" and endpoint.startswith("pulls"):
            return [{"title": "Implement #7001", "body": ""}]
        if kind == "closed" and endpoint.startswith("issues"):
            return [
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "state": "closed",
                            "pull_request": {
                                "url": "https://github.com/org/repo/pull/42"
                            },
                        }
                    },
                }
            ]
        if kind == "truncated":
            return [{}] * 100
        if kind == "malformed":
            return {"message": "unavailable"}
        return []

    monkeypatch.setattr(conductor, "github_list", lookup)
    with pytest.raises(
        ValueError,
        match="validation_refinement_(pr_reference_ambiguous|pr_lookup_incomplete)",
    ):
        validation.preview(838, "prepare")


@pytest.mark.parametrize(
    "change", ["issue", "audit", "receipt", "new_delivery", "control"]
)
def test_refinement_apply_rejects_changed_state(database, completed_refinement, change):
    validation, issue = completed_refinement
    proposal = validation.preview(838, "prepare")
    with Session(database) as db, db.begin():
        if change == "issue":
            issue["updated_at"] = "2026-10-23T19:00:00Z"
        elif change == "audit":
            audit = db.exec(
                select(FactoryAudit).where(FactoryAudit.action == "finish_task")
            ).one()
            detail = json.loads(audit.detail_json)
            detail["evidence"]["reason"] = "changed"
            audit.detail_json = json.dumps(detail)
        elif change == "receipt":
            db.get(FactoryReceipt, 838).body = "changed scope"
        elif change == "new_delivery":
            source = db.get(FactoryReceipt, 838).model_dump()
            source.update(
                id=839,
                task_id=None,
                task_class="bugfix",
                routing_tier="delivery",
                state="queued",
            )
            db.add(FactoryReceipt(**source))
        else:
            db.get(FactoryControl, "factory").version += 1
    with Session(database) as db:
        control_before = db.get(FactoryControl, "factory").model_dump()
    with pytest.raises(
        ValueError, match="validation_(preview_changed|refinement_history_ambiguous)"
    ):
        validation.apply(
            838,
            "prepare",
            actor="operator",
            request_key="changed",
            expected_preview_sha256=proposal["preview_sha256"],
        )
    with Session(database) as db:
        assert db.get(FactoryControl, "factory").model_dump() == control_before
        assert (
            db.exec(
                select(FactoryAudit).where(FactoryAudit.action == validation.ACTION)
            ).first()
            is None
        )


def test_refinement_replay_does_not_read_github(completed_refinement, monkeypatch):
    from factory.orchestration import factory_conductor as conductor

    validation, _ = completed_refinement
    proposal = validation.preview(838, "prepare")
    args = dict(
        actor="operator",
        request_key="once",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    result = validation.apply(838, "prepare", **args)

    def forbidden(*args):
        raise AssertionError("replay must not fetch")

    monkeypatch.setattr(conductor, "github_get", forbidden)
    assert validation.apply(838, "prepare", **args) == result
    with pytest.raises(ValueError, match="conflicting_validation_request"):
        validation.apply(838, "different", **args)


def test_refinement_state_rechecked_after_github_read(
    database, completed_refinement, monkeypatch
):
    from factory.orchestration import factory_conductor as conductor

    validation, issue = completed_refinement

    def github_get(*args):
        with Session(database) as db, db.begin():
            db.get(FactoryReceipt, 838).cancellation_requested = True
        return dict(issue)

    monkeypatch.setattr(conductor, "github_get", github_get)
    with pytest.raises(ValueError, match="validation_receipt_unavailable"):
        validation.preview(838, "prepare")


@pytest.mark.parametrize(
    "hold",
    ["landing_hold", "review_only_hold", "nonrenewable_recovery", "review_audit"],
)
def test_refinement_cannot_bypass_old_holds(database, completed_refinement, hold):
    validation, _ = completed_refinement
    with Session(database) as db, db.begin():
        if hold == "review_audit":
            controls._audit(
                db, "operator", "review_recovery_authorized", task_id="task"
            )
        else:
            db.get(FactoryReceipt, 838).direction_json = json.dumps(
                {hold: {"reason": "preserve"}}
            )
    with Session(database) as db:
        before = db.get(FactoryReceipt, 838).model_dump()
    with pytest.raises(ValueError, match="validation_refinement_held"):
        validation.preview(838, "prepare")
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).model_dump() == before


@pytest.mark.parametrize(
    "state",
    [
        "open",
        "ready",
        "active",
        "needs_human",
        "deferred",
        "done",
        "closed",
        "human",
        "blocked",
    ],
)
def test_refinement_respects_work_item_readiness(database, completed_refinement, state):
    from factory.orchestration.factory_models import WorkItem, WorkItemEdge

    validation, _ = completed_refinement
    SQLModel.metadata.create_all(
        database, tables=[WorkItem.__table__, WorkItemEdge.__table__]
    )
    with Session(database) as db, db.begin():
        db.add(
            WorkItem(
                id=1,
                title="refined",
                state=state if state not in ("human", "blocked") else "ready",
                labels=["human"] if state == "human" else [],
                source_kind="github",
                trust="trusted",
                close_reason="completed" if state == "closed" else None,
            )
        )
        db.get(FactoryReceipt, 838).work_item_id = 1
        if state == "blocked":
            db.add(
                WorkItem(
                    id=2,
                    title="dependency",
                    state="open",
                    source_kind="github",
                    trust="trusted",
                )
            )
            db.add(WorkItemEdge(from_id=2, to_id=1, kind="blocks"))
    if state in ("open", "ready"):
        assert validation.preview(838, "prepare")["selection_kind"] == "refined_issue"
    else:
        with pytest.raises(
            ValueError, match="validation_refinement_work_(unavailable|blocked)"
        ):
            validation.preview(838, "prepare")


@pytest.mark.parametrize(
    "reference",
    ["other/repo#7001", "https://github.com/other/repo/issues/7001", "#70010"],
)
def test_refinement_ignores_other_issue_references(
    completed_refinement, monkeypatch, reference
):
    from factory.orchestration import factory_conductor as conductor

    validation, _ = completed_refinement
    monkeypatch.setattr(
        conductor,
        "github_list",
        lambda repo, path: (
            [{"number": 42, "title": "Documentation", "body": reference}]
            if path.startswith("pulls")
            else []
        ),
    )
    assert validation.preview(838, "prepare")["selection_kind"] == "refined_issue"


@pytest.mark.parametrize(
    "reference", ["#7001", "https://github.com/org/repo/issues/7001"]
)
def test_refinement_reports_reference_ambiguity_with_pr_identity(
    completed_refinement, monkeypatch, reference
):
    from factory.orchestration import factory_conductor as conductor

    validation, _ = completed_refinement
    monkeypatch.setattr(
        conductor,
        "github_list",
        lambda repo, path: (
            [{"number": 42, "title": "Documentation", "body": reference}]
            if path.startswith("pulls")
            else []
        ),
    )
    with pytest.raises(
        ValueError, match="validation_refinement_pr_reference_ambiguous:42"
    ):
        validation.preview(838, "prepare")


@pytest.mark.parametrize("change", ["generation", "failed", "delivery_tier", "new_pr"])
def test_refinement_late_changes_refuse_apply(
    database, completed_refinement, monkeypatch, change
):
    from factory.orchestration import factory_conductor as conductor

    validation, _ = completed_refinement
    proposal = validation.preview(838, "prepare")
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        if change == "generation":
            row.generation += 1
        elif change == "failed":
            row.state = "failed"
        elif change == "delivery_tier":
            row.routing_tier = "delivery"
    if change == "new_pr":
        monkeypatch.setattr(
            conductor,
            "github_list",
            lambda *args: [{"source": {"issue": {"number": 42, "pull_request": {}}}}],
        )
    with pytest.raises(ValueError, match="validation_"):
        validation.apply(
            838,
            "prepare",
            actor="operator",
            request_key="late",
            expected_preview_sha256=proposal["preview_sha256"],
        )
    with Session(database) as db:
        assert (
            db.exec(
                select(FactoryAudit).where(FactoryAudit.action == validation.ACTION)
            ).first()
            is None
        )


@pytest.fixture
def held_refinement(database, completed_refinement):
    from factory.orchestration import factory_refinement_delivery as owner
    from factory.orchestration.factory_models import (
        WorkItem,
        WorkItemEdge,
        WorkItemEvent,
    )

    SQLModel.metadata.create_all(
        database, tables=[m.__table__ for m in (WorkItem, WorkItemEdge, WorkItemEvent)]
    )
    _validation, issue = completed_refinement
    issue.update(title="numeric correction support", body="Keep #7022 suspended")
    direction = {
        "landing_hold": {
            "actor": "factory:conductor",
            "cause": "refine-gate:task",
            "reason": "Leave unmerged",
        },
        "conductor_gates": [
            {
                "kind": "live_validation",
                "leave_unmerged": True,
                "scope": "Keep #7022 suspended; no live model call or production mutation",
            }
        ],
    }
    with Session(database) as db, db.begin():
        db.get(FactoryReceipt, 838).direction_json = json.dumps(direction)
    return owner, issue, direction


def held_apply(owner, proposal, key="continuation"):
    return owner.apply(
        838,
        "continue held work",
        actor="operator",
        request_key=key,
        expected_preview_sha256=proposal["preview_sha256"],
    )


def test_held_refinement_creates_distinct_restricted_delivery(
    database, held_refinement
):
    owner, _issue, direction = held_refinement
    with Session(database) as db:
        before = db.get(FactoryReceipt, 838).model_dump(mode="json")
        task_before = db.get(SwarmTask, "task").model_dump(mode="json")
    proposal = owner.preview(838, "continue held work")
    assert proposal["inherited_direction"] == direction
    assert not proposal["policy_after"]["auto_merge"]
    result = held_apply(owner, proposal)
    assert result["receipt_id"] != 838 and result["control_state"] == "paused"
    assert not result["execution_authorized"] and not result["landing_authorized"]
    assert held_apply(owner, proposal) == result
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).model_dump(mode="json") == before
        assert db.get(SwarmTask, "task").model_dump(mode="json") == task_before
        new = db.get(FactoryReceipt, result["receipt_id"])
        assert new.state == "queued" and new.task_id is None
        assert new.task_class == controls.DEFAULT_TASK_CLASS
        copied = json.loads(new.direction_json)
        assert all(copied[key] == value for key, value in direction.items())
        policy = json.loads(db.get(FactoryControl, "factory").policy_json)
        assert owner.admission_refusal(db, new, policy) is None
        for key in (
            "generation",
            "task_budget_usd",
            "turn_budget_usd",
            "max_turns_per_task",
            "task_timeout_seconds",
        ):
            assert policy[key] == proposal["policy_before"][key]
        assert not policy["auto_merge"] and not policy["intake"]["enabled"]
        assert len(db.exec(select(FactoryReceipt)).all()) == 2
    with pytest.raises(ValueError, match="conflicting_refinement_delivery_request"):
        owner.apply(
            838,
            "changed reason",
            actor="operator",
            request_key="continuation",
            expected_preview_sha256=proposal["preview_sha256"],
        )


@pytest.mark.parametrize(
    "change",
    [
        "hold_removed",
        "hold_changed",
        "gates_removed",
        "gate_changed",
        "version",
        "existing_delivery",
        "human",
        "assigned",
        "closed",
    ],
)
def test_held_refinement_stale_preview_is_atomic(database, held_refinement, change):
    owner, issue, _direction = held_refinement
    proposal = owner.preview(838, "continue held work")
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        direction = json.loads(row.direction_json)
        if change == "hold_removed":
            direction.pop("landing_hold")
        elif change == "hold_changed":
            direction["landing_hold"]["reason"] = "new restriction"
        elif change == "gates_removed":
            direction.pop("conductor_gates")
        elif change == "gate_changed":
            direction["conductor_gates"][0]["scope"] = "different scope"
        elif change == "version":
            db.get(FactoryControl, "factory").version += 1
        elif change == "existing_delivery":
            db.add(
                FactoryReceipt(
                    repo=row.repo,
                    issue_number=row.issue_number,
                    generation=row.generation,
                    task_class="bug-fix",
                    title="owned",
                    body="owned",
                    url=row.url,
                    actor="other",
                )
            )
        elif change == "human":
            issue["labels"] = [{"name": "agent-ready"}, {"name": "human"}]
        elif change == "assigned":
            issue["assignees"] = [{"login": "owner"}]
        elif change == "closed":
            issue["state"] = "closed"
        row.direction_json = json.dumps(direction)
    with pytest.raises(ValueError):
        held_apply(owner, proposal)
    with Session(database) as db:
        assert not db.exec(
            select(FactoryAudit).where(FactoryAudit.action == owner.ACTION)
        ).all()
        assert json.loads(db.get(FactoryControl, "factory").policy_json)["auto_merge"]


@pytest.mark.parametrize(
    "change",
    [
        "hold",
        "gate",
        "marker",
        "all_direction",
        "generation",
        "source",
        "auto_merge",
        "intake",
    ],
)
def test_held_delivery_admission_rejects_missing_or_changed_proof(
    database, held_refinement, change
):
    owner, _, _ = held_refinement
    result = held_apply(owner, owner.preview(838, "continue held work"))
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, result["receipt_id"])
        direction = json.loads(row.direction_json)
        policy = json.loads(db.get(FactoryControl, "factory").policy_json)
        if change == "hold":
            direction.pop("landing_hold")
        elif change == "gate":
            direction["conductor_gates"][0]["scope"] = "changed"
        elif change == "marker":
            direction.pop(owner.MARKER)
        elif change == "all_direction":
            direction = {}
        elif change == "generation":
            row.generation += 1
        elif change == "source":
            db.get(FactoryReceipt, 838).direction_json = "{}"
        elif change == "auto_merge":
            policy["auto_merge"] = True
        elif change == "intake":
            policy["intake"]["enabled"] = True
        row.direction_json = json.dumps(direction)
        assert (
            owner.admission_refusal(db, row, policy)
            == "refinement_delivery_evidence_changed"
        )
        assert row.task_id is None


def test_held_refinement_concurrent_apply_creates_once(database, held_refinement):
    owner, _, _ = held_refinement
    proposal = owner.preview(838, "continue held work")

    def apply(key):
        try:
            return held_apply(owner, proposal, key)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, ["first", "second"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    with Session(database) as db:
        assert len(db.exec(select(FactoryReceipt)).all()) == 2


def test_held_refinement_pr_race_refuses(database, held_refinement, monkeypatch):
    from factory.orchestration import factory_conductor as conductor

    owner, _, _ = held_refinement
    proposal = owner.preview(838, "continue held work")
    monkeypatch.setattr(
        conductor, "github_list", lambda *args: [{"number": 12, "body": "Fixes #7001"}]
    )
    with pytest.raises(ValueError, match="pr_reference_ambiguous"):
        held_apply(owner, proposal)


@pytest.mark.parametrize(
    "extra",
    [
        "review_only_hold",
        "nonrenewable_recovery",
        "landing_authorization",
        "policy_overlay",
    ],
)
def test_held_refinement_does_not_transfer_other_grants_or_holds(
    database, held_refinement, extra
):
    owner, _, _ = held_refinement
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        direction = json.loads(row.direction_json)
        direction[extra] = {"reason": "other owner"}
        row.direction_json = json.dumps(direction)
    with pytest.raises(ValueError):
        owner.preview(838, "continue held work")


def test_held_refinement_receipt_failure_rolls_back_configuration(
    database, held_refinement, monkeypatch
):
    from factory.orchestration import factory_intake

    owner, _, _ = held_refinement
    proposal = owner.preview(838, "continue held work")

    def refused(*args, **kwargs):
        raise ValueError("receipt rejected")

    monkeypatch.setattr(factory_intake, "receive_issue", refused)
    with pytest.raises(ValueError, match="receipt rejected"):
        held_apply(owner, proposal)
    with Session(database) as db:
        control = db.get(FactoryControl, "factory")
        assert (
            controls.validate_policy(json.loads(control.policy_json))
            == proposal["policy_before"]
        )
        assert control.version == proposal["expected_version"]
        assert len(db.exec(select(FactoryReceipt)).all()) == 1


def test_held_refinement_replay_does_not_repeat_external_observation(
    held_refinement, monkeypatch
):
    from factory.orchestration import factory_validation

    owner, _, _ = held_refinement
    proposal = owner.preview(838, "continue held work")
    expected = held_apply(owner, proposal)

    def forbidden(*args, **kwargs):
        raise AssertionError("replay contacted GitHub")

    monkeypatch.setattr(factory_validation, "_refinement_observation", forbidden)
    assert held_apply(owner, proposal) == expected


def test_backlog_preparation_preserves_bounds_holds_and_requires_enable(
    database, validation_ready
):
    validation = validation_ready
    proposal = validation.preview_backlog("resume useful backlog")
    before, after = proposal["policy_before"], proposal["policy_after"]
    assert after["issue_numbers"] == []
    assert after["intake"]["enabled"]
    assert not after["auto_merge"]
    assert not after["intake"]["refine_enabled"]
    assert not after["intake"]["close_enabled"]
    assert not after["problem_issues"]["enabled"]
    assert after["repos"]["org/other"]["paused"]
    for key in before.keys() - {
        "auto_merge",
        "issue_numbers",
        "intake",
        "problem_issues",
        "repos",
    }:
        assert before[key] == after[key]
    for key in before["intake"].keys() - {"enabled", "refine_enabled", "close_enabled"}:
        assert before["intake"][key] == after["intake"][key]
    with Session(database) as db:
        receipt_before = db.get(FactoryReceipt, 838).model_dump()
    args = dict(
        actor="operator",
        request_key="backlog",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    result = validation.apply_backlog("resume useful backlog", **args)
    assert result["state"] == "paused" and not result["execution_authorized"]
    assert validation.apply_backlog("resume useful backlog", **args) == result
    with pytest.raises(ValueError, match="conflicting_validation_request"):
        validation.apply_backlog("changed", **args)
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).model_dump() == receipt_before
        assert json.loads(db.get(FactoryControl, "factory").policy_json) == after
        events = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == validation.BACKLOG_ACTION)
        ).all()
        assert len(events) == 1


@pytest.mark.parametrize("state", ["uncertain", "landing"])
def test_backlog_rejects_existing_work(database, validation_ready, state):
    with Session(database) as db, db.begin():
        db.get(FactoryReceipt, 838).state = state
    with pytest.raises(ValueError, match="validation_other_work_present"):
        validation_ready.preview_backlog("resume")


def test_backlog_rejects_receipt_race_and_keeps_policy(database, validation_ready):
    validation = validation_ready
    proposal = validation.preview_backlog("resume")
    with Session(database) as db, db.begin():
        db.get(
            FactoryReceipt, 838
        ).direction_json = '{"landing_hold":{"reason":"new hold"}}'
    with pytest.raises(ValueError, match="validation_preview_changed"):
        validation.apply_backlog(
            "resume",
            actor="operator",
            request_key="race",
            expected_preview_sha256=proposal["preview_sha256"],
        )
    with Session(database) as db:
        assert (
            controls.validate_policy(
                json.loads(db.get(FactoryControl, "factory").policy_json)
            )
            == proposal["policy_before"]
        )


@pytest.mark.parametrize(
    "change", ["version", "policy", "enabled", "unknown_start", "active", "labels"]
)
def test_backlog_rechecks_authority_snapshot(database, validation_ready, change):
    validation = validation_ready
    proposal = validation.preview_backlog("resume")
    with Session(database) as db, db.begin():
        control = db.get(FactoryControl, "factory")
        if change == "version":
            control.version += 1
        elif change == "enabled":
            control.state = "enabled"
        elif change == "unknown_start":
            db.exec(select(FactoryStart)).one().status = "uncertain"
        elif change == "active":
            db.get(FactoryReceipt, 838).state = "admitted"
        else:
            policy = json.loads(control.policy_json)
            if change == "labels":
                policy["intake"]["labels"] = ["bug"]
            else:
                policy["task_budget_usd"] = 60
            control.policy_json = json.dumps(policy)
    with pytest.raises(ValueError):
        validation.apply_backlog(
            "resume",
            actor="operator",
            request_key="race",
            expected_preview_sha256=proposal["preview_sha256"],
        )
    with Session(database) as db:
        assert not db.exec(
            select(FactoryAudit).where(FactoryAudit.action == validation.BACKLOG_ACTION)
        ).all()


@pytest.mark.parametrize("override", [False, True])
def test_backlog_rejects_or_matched_nonready_labels(
    database, validation_ready, override
):
    with Session(database) as db, db.begin():
        control = db.get(FactoryControl, "factory")
        policy = json.loads(control.policy_json)
        if override:
            policy["repos"]["org/repo"]["labels"] = ["bug"]
        else:
            policy["intake"]["labels"] = ["agent-ready", "bug"]
        control.policy_json = json.dumps(policy)
    with pytest.raises(ValueError, match="backlog_requires_agent_ready_filter"):
        validation_ready.preview_backlog("resume")


def test_backlog_empty_manual_allowlist_cannot_ingest_unreviewed_work(
    database, validation_ready, monkeypatch
):
    from factory.orchestration import factory_conductor

    proposal = validation_ready.preview_backlog("resume")

    def forbidden(*args, **kwargs):
        pytest.fail("manual ingest must not read or receive formerly allowlisted work")

    monkeypatch.setattr(factory_conductor, "github_get", forbidden)
    from factory.orchestration import factory_intake

    monkeypatch.setattr(factory_intake, "receive_issue", forbidden)
    factory_conductor.ingest_eligible(proposal["policy_after"])
    inactive = {
        **proposal["policy_after"],
        "intake": {**proposal["policy_after"]["intake"], "enabled": False},
    }
    with pytest.raises(ValueError, match="invalid issue_numbers"):
        controls.validate_policy(inactive)


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize(
    "exclusions,accepted",
    [
        (["needs-human", "wontfix"], True),
        (["needs-human", "security-finding", "wontfix"], True),
        (["needs-human", "operator-only", "wontfix"], True),
        (["needs-human"], False),
        (["wontfix"], False),
    ],
)
def test_backlog_preview_preserves_effective_exclusions(
    database, validation_ready, active, exclusions, accepted
):
    if active:
        safe = validation_ready.preview_backlog("prepare")["policy_after"]
        safe["issue_numbers"] = [7001]
        safe["intake"]["enabled"] = False
    with Session(database) as db, db.begin():
        control = db.get(FactoryControl, "factory")
        policy = safe if active else json.loads(control.policy_json)
        policy["repos"][policy["repo"]]["exclude_labels"] = exclusions
        control.policy_json = json.dumps(policy)
        if active:
            db.get(FactoryReceipt, 838).state = "admitted"
    if not accepted:
        with pytest.raises(ValueError, match="backlog_requires_safe_exclusions"):
            validation_ready.preview_backlog("restore")
        return
    proposal = validation_ready.preview_backlog("restore")
    assert (
        controls.repo_entry(proposal["policy_after"], policy["repo"])["exclude_labels"]
        == exclusions
    )
    assert (
        proposal["policy_after"]["intake"]["exclude_labels"]
        == controls.intake_policy(policy)["exclude_labels"]
    )


def test_backlog_concurrent_apply_has_one_winner(database, validation_ready):
    proposal = validation_ready.preview_backlog("resume")

    def attempt(key):
        try:
            return validation_ready.apply_backlog(
                "resume",
                actor="operator",
                request_key=key,
                expected_preview_sha256=proposal["preview_sha256"],
            )
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["one", "two"]))
    assert sum(result["ok"] for result in results) == 1
    assert (
        next(result for result in results if not result["ok"])["reason"]
        == "validation_preview_changed"
    )


@pytest.mark.parametrize(
    "state,reserved", [("admitted", False), ("admitted", True), ("queued", False)]
)
def test_backlog_active_restore_only_enables_intake(
    database, validation_ready, state, reserved
):
    validation = validation_ready
    safe = validation.preview_backlog("initial safe posture")["policy_after"]
    safe["issue_numbers"] = [7001]
    safe["intake"]["enabled"] = False
    with Session(database) as db, db.begin():
        db.get(FactoryControl, "factory").policy_json = json.dumps(safe)
        db.get(FactoryReceipt, 838).state = state
        if reserved:
            start = db.exec(select(FactoryStart)).one()
            start.status, start.cost_usd = "reserved", None
        before_receipt = db.get(FactoryReceipt, 838).model_dump(mode="json")
        before_start = db.exec(select(FactoryStart)).one().model_dump(mode="json")
    proposal = validation.preview_backlog("restore discovery alongside existing work")
    expected = json.loads(json.dumps(safe))
    expected["intake"]["enabled"] = True
    assert proposal["policy_after"] == expected
    assert controls.admission_eligible(expected, 7001, "operator")
    assert not controls.admission_eligible(expected, 9999, "operator")
    if state == "admitted":
        # No generic configure exemption is introduced.
        refused = controls.set_control("configure", "operator", policy=expected)
        assert not refused["ok"] and refused["reason"] == "generation_not_advanced"
    result = validation.apply_backlog(
        "restore discovery alongside existing work",
        actor="operator",
        request_key="restore-active",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    assert result["ok"] and result["state"] == "paused"
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).model_dump(mode="json") == before_receipt
        assert (
            db.exec(select(FactoryStart)).one().model_dump(mode="json") == before_start
        )
        assert json.loads(db.get(FactoryControl, "factory").policy_json) == expected


@pytest.fixture
def active_backlog(database, validation_ready):
    safe = validation_ready.preview_backlog("safe")["policy_after"]
    safe["issue_numbers"] = [7001]
    safe["intake"]["enabled"] = False
    with Session(database) as db, db.begin():
        db.get(FactoryControl, "factory").policy_json = json.dumps(safe)
        db.get(FactoryReceipt, 838).state = "admitted"
    return validation_ready


@pytest.mark.parametrize("operation", ["restore", "batch"])
@pytest.mark.parametrize(
    "exclusions,accepted",
    [
        (["needs-human", "wontfix"], True),
        (["needs-human", "security-finding", "wontfix"], True),
        (["needs-human", "operator-only", "wontfix"], True),
        (["needs-human"], False),
        (["wontfix"], False),
    ],
)
def test_active_admission_checks_effective_repository_exclusions(
    database, active_backlog, operation, exclusions, accepted
):
    with Session(database) as db, db.begin():
        control = db.get(FactoryControl, "factory")
        policy = json.loads(control.policy_json)
        # The primary override, not the global defaults, is the guard's input.
        policy["repos"].setdefault(policy["repo"], {})["exclude_labels"] = exclusions
        control.policy_json = json.dumps(policy)
        before = controls.validate_policy(policy)
        if not accepted:
            with pytest.raises(
                ValueError, match="backlog_active_requires_safe_posture"
            ):
                controls._active_admission_policy(db, control)
            return
        after = (
            controls.active_intake_restoration_policy(db, control)
            if operation == "restore"
            else controls.refinement_batch_policy(db, control, [7002])
        )
        assert after["repos"] == before["repos"]
        assert after["intake"]["exclude_labels"] == before["intake"]["exclude_labels"]
        assert control.policy_json == json.dumps(policy)


@pytest.mark.parametrize("active", [True, False])
def test_broad_refinement_preserves_policy_and_pinned_work(
    database, active_backlog, active
):
    with Session(database) as db, db.begin():
        if not active:
            db.get(FactoryReceipt, 838).state = "succeeded"
        before = db.get(FactoryReceipt, 838).model_dump(mode="json")
    proposal = active_backlog.preview_backlog(
        "continuous eligible work", include_refinement=True
    )
    expected = json.loads(json.dumps(proposal["policy_before"]))
    expected["intake"].update(
        enabled=True, refine_enabled=True, refine_brief_only=True, refinement_batch=[]
    )
    assert proposal["policy_after"] == expected
    assert proposal["mode"] == "broad_brief_refinement"
    assert not proposal["execution_authorized"]
    assert controls.admission_eligible(
        expected, 9999, "factory:intake", task_class="refine", repo=expected["repo"]
    )
    assert controls.admission_eligible(
        expected, 9999, "factory:intake", task_class="bug-fix", repo=expected["repo"]
    )
    args = dict(
        actor="operator",
        request_key="continuous",
        expected_preview_sha256=proposal["preview_sha256"],
        include_refinement=True,
    )
    result = active_backlog.apply_backlog("continuous eligible work", **args)
    assert result["ok"] and result["state"] == "paused"
    assert active_backlog.apply_backlog("continuous eligible work", **args) == result
    with pytest.raises(ValueError):
        active_backlog.apply_backlog(
            "continuous eligible work", **{**args, "include_refinement": False}
        )
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).model_dump(mode="json") == before
        assert json.loads(db.get(FactoryControl, "factory").policy_json) == expected


@pytest.mark.parametrize("state", ["queued", "uncertain", "landing"])
def test_broad_refinement_refuses_queued_or_unresolved_work(
    database, active_backlog, state
):
    with Session(database) as db, db.begin():
        db.get(FactoryReceipt, 838).state = state
    with pytest.raises(ValueError):
        active_backlog.preview_backlog("continuous", include_refinement=True)


def test_legacy_backlog_replay_keeps_original_request_shape(database, validation_ready):
    proposal = validation_ready.preview_backlog("legacy")
    args = dict(
        actor="operator",
        request_key="legacy",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    result = validation_ready.apply_backlog("legacy", **args)
    assert (
        validation_ready.apply_backlog("legacy", **args, include_refinement=False)
        == result
    )
    with Session(database) as db:
        audit = db.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == validation_ready.BACKLOG_ACTION
            )
        ).one()
        assert "include_refinement" not in json.loads(audit.detail_json)["request"]


@pytest.mark.parametrize(
    "change",
    ["auto_merge", "refine_enabled", "close_enabled", "production", "other_repo"],
)
def test_active_backlog_rejects_unsafe_posture(database, active_backlog, change):
    with Session(database) as db, db.begin():
        control = db.get(FactoryControl, "factory")
        policy = json.loads(control.policy_json)
        if change == "auto_merge":
            policy[change] = True
        elif change in ("refine_enabled", "close_enabled"):
            policy["intake"][change] = True
        elif change == "production":
            policy["problem_issues"]["enabled"] = True
        else:
            policy["repos"]["org/other"]["paused"] = False
        control.policy_json = json.dumps(policy)
    with pytest.raises(ValueError, match="backlog_active_requires_safe_posture"):
        active_backlog.preview_backlog("restore")


@pytest.mark.parametrize("change", ["start", "unknown", "hold", "policy", "version"])
def test_active_backlog_rejects_stale_preview(database, active_backlog, change):
    proposal = active_backlog.preview_backlog("restore")
    with Session(database) as db, db.begin():
        control = db.get(FactoryControl, "factory")
        if change in ("start", "unknown"):
            db.exec(select(FactoryStart)).one().status = (
                "uncertain" if change == "unknown" else "reserved"
            )
        elif change == "hold":
            db.get(
                FactoryReceipt, 838
            ).direction_json = '{"landing_hold":{"reason":"changed"}}'
        elif change == "version":
            control.version += 1
        else:
            policy = json.loads(control.policy_json)
            policy["task_budget_usd"] = 60
            control.policy_json = json.dumps(policy)
    with pytest.raises(ValueError):
        active_backlog.apply_backlog(
            "restore",
            actor="operator",
            request_key="stale",
            expected_preview_sha256=proposal["preview_sha256"],
        )
    with Session(database) as db:
        assert not json.loads(db.get(FactoryControl, "factory").policy_json)["intake"][
            "enabled"
        ]


def test_active_backlog_replay_and_conflicting_key(database, active_backlog):
    proposal = active_backlog.preview_backlog("restore")
    args = dict(
        actor="operator",
        request_key="active-replay",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    result = active_backlog.apply_backlog("restore", **args)
    assert active_backlog.apply_backlog("restore", **args) == result
    with pytest.raises(ValueError, match="conflicting_validation_request"):
        active_backlog.apply_backlog("different", **args)


@pytest.mark.parametrize("state", ["queued", "cancelled", "missing"])
@pytest.mark.parametrize("other_state", ["queued", "admitted"])
@pytest.mark.parametrize("internal", [False, True])
def test_active_backlog_refuses_orphan_reservation(
    database, active_backlog, state, other_state, internal
):
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        # Unrelated work must not legitimize this reservation.
        other = row.model_dump(exclude={"id", "task_id"})
        if state == "missing":
            db.delete(row)
        else:
            row.state = state
        other.update(issue_number=7002, state=other_state)
        db.add(FactoryReceipt(id=839, task_id=None, **other))
        db.exec(select(FactoryStart)).one().status = "reserved"
    with pytest.raises(ValueError, match="validation_unresolved_execution"):
        if internal:
            controls.set_control("restore_active_intake", "operator")
        else:
            active_backlog.preview_backlog("restore")


def test_active_backlog_concurrent_apply_has_one_winner(database, active_backlog):
    proposal = active_backlog.preview_backlog("restore")

    def apply(key):
        try:
            return active_backlog.apply_backlog(
                "restore",
                actor="operator",
                request_key=key,
                expected_preview_sha256=proposal["preview_sha256"],
            )["ok"]
        except ValueError as exc:
            assert str(exc) == "validation_preview_changed"
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(apply, ["active-a", "active-b"])) == [False, True]


@pytest.fixture
def refinement_batch(database, active_backlog, monkeypatch):
    from factory.orchestration import factory_refinement_batch as batch
    from factory.orchestration.factory_models import WorkItem, WorkItemEdge

    SQLModel.metadata.create_all(
        database, tables=[WorkItem.__table__, WorkItemEdge.__table__]
    )
    issues = {
        number: {
            "number": number,
            "state": "open",
            "assignees": [],
            "labels": [],
            "html_url": f"https://github.com/org/repo/issues/{number}",
            "title": "archive",
            "body": "bounded brief",
            "updated_at": "2026-10-25T11:00:00Z",
        }
        for number in (7249, 7251)
    }
    monkeypatch.setattr(
        active_backlog,
        "issue_without_pr_references",
        lambda repo, number: dict(issues[number]),
    )
    return batch, issues


def test_refinement_batch_preserves_active_work_and_requires_enable(
    database, refinement_batch
):
    batch, _ = refinement_batch
    with Session(database) as db:
        receipt = db.get(FactoryReceipt, 838).model_dump(mode="json")
        start = db.exec(select(FactoryStart)).one().model_dump(mode="json")
    proposal = batch.preview([7251, 7249], "prepare exact briefs")
    before, after = proposal["policy_before"], proposal["policy_after"]
    expected = json.loads(json.dumps(before))
    expected["intake"].update(
        enabled=True, refine_enabled=True, refinement_batch=[7249, 7251]
    )
    assert after == expected
    assert not proposal["execution_authorized"]
    args = dict(
        actor="operator",
        request_key="batch",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    result = batch.apply([7249, 7251], "prepare exact briefs", **args)
    assert (
        result["ok"]
        and result["state"] == "paused"
        and not result["execution_authorized"]
    )
    assert batch.apply([7249, 7251], "prepare exact briefs", **args) == result
    with pytest.raises(ValueError, match="conflicting_validation_request"):
        batch.apply([7249], "prepare exact briefs", **args)
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).model_dump(mode="json") == receipt
        assert db.exec(select(FactoryStart)).one().model_dump(mode="json") == start
        assert len(db.exec(select(FactoryReceipt)).all()) == 1
        control = db.get(FactoryControl, "factory")
        assert control.state == "paused" and json.loads(control.policy_json) == expected


@pytest.mark.parametrize(
    "numbers", [[], [True], [0], [1, 1], list(range(1, 12)), "7249"]
)
def test_refinement_batch_rejects_invalid_scope(refinement_batch, numbers):
    batch, _ = refinement_batch
    with pytest.raises(ValueError, match="invalid refinement_batch"):
        batch.preview(numbers, "briefs")


@pytest.mark.parametrize(
    "change",
    [
        {"state": "closed"},
        {"assignees": [{"login": "owner"}]},
        {"labels": [{"name": "human"}]},
        {"labels": [{"name": "agent-ready"}]},
        {"labels": [{"name": "needs-thought"}]},
        {"labels": [{"name": "needs-human"}]},
        {"labels": None},
        {"number": 7250},
        {"pull_request": {}},
    ],
)
def test_refinement_batch_rechecks_issue_ownership(refinement_batch, change):
    batch, issues = refinement_batch
    proposal = batch.preview([7249, 7251], "briefs")
    issues[7249].update(change)
    with pytest.raises(ValueError, match="refinement_batch_issue_ineligible"):
        batch.apply(
            [7249, 7251],
            "briefs",
            actor="operator",
            request_key="race",
            expected_preview_sha256=proposal["preview_sha256"],
        )


@pytest.mark.parametrize(
    "change", ["body", "version", "hold", "history", "uncertain", "orphan", "queued"]
)
def test_refinement_batch_rejects_changed_evidence(database, refinement_batch, change):
    batch, issues = refinement_batch
    proposal = batch.preview([7249], "briefs")
    with Session(database) as db, db.begin():
        if change == "body":
            issues[7249]["body"] = "changed scope"
        elif change == "version":
            db.get(FactoryControl, "factory").version += 1
        elif change == "hold":
            db.get(
                FactoryReceipt, 838
            ).direction_json = '{"landing_hold":{"reason":"changed"}}'
        elif change in ("uncertain", "orphan"):
            db.exec(select(FactoryStart)).one().status = (
                "uncertain" if change == "uncertain" else "reserved"
            )
            if change == "orphan":
                db.get(FactoryReceipt, 838).state = "escalated"
        else:
            row = db.get(FactoryReceipt, 838).model_dump(exclude={"id", "task_id"})
            row.update(
                issue_number=7249 if change == "history" else 9999,
                state="cancelled" if change == "history" else "queued",
            )
            db.add(FactoryReceipt(**row))
    with pytest.raises(ValueError):
        batch.apply(
            [7249],
            "briefs",
            actor="operator",
            request_key="race",
            expected_preview_sha256=proposal["preview_sha256"],
        )
    with Session(database) as db:
        assert not json.loads(db.get(FactoryControl, "factory").policy_json)["intake"][
            "enabled"
        ]


def test_refinement_batch_allows_owned_reservation(database, refinement_batch):
    with Session(database) as db, db.begin():
        db.exec(select(FactoryStart)).one().status = "reserved"
    assert refinement_batch[0].preview([7249], "briefs")["policy_after"]["intake"][
        "refinement_batch"
    ] == [7249]


@pytest.mark.parametrize("change", ["local", "human", "blocked", "alias_history"])
def test_refinement_batch_checks_work_item_ownership_and_history(
    database, refinement_batch, change
):
    from factory.orchestration.factory_models import WorkItem, WorkItemEdge

    batch, _ = refinement_batch
    proposal = batch.preview([7249], "briefs")
    with Session(database) as db, db.begin():
        target = WorkItem(
            id=101,
            title="archive",
            state="open",
            source_kind="github",
            trust="trusted",
            authority="local" if change == "local" else "github",
            labels=["human"] if change == "human" else [],
            github_repo="org/repo",
            github_issue_number=7249,
        )
        db.add(target)
        if change == "blocked":
            db.add(
                WorkItem(
                    id=102,
                    title="dependency",
                    state="open",
                    source_kind="github",
                    trust="trusted",
                )
            )
            db.flush()
            db.add(WorkItemEdge(from_id=102, to_id=101, kind="blocks"))
        if change == "alias_history":
            db.get(FactoryReceipt, 838).work_item_id = 101
    with pytest.raises(
        ValueError,
        match="refinement_batch_(work_unavailable|work_blocked|existing_history)",
    ):
        batch.apply(
            [7249],
            "briefs",
            actor="operator",
            request_key="work-race",
            expected_preview_sha256=proposal["preview_sha256"],
        )


def test_refinement_batch_concurrent_apply_has_one_winner(refinement_batch):
    batch, _ = refinement_batch
    proposal = batch.preview([7249], "briefs")

    def apply(key):
        try:
            return batch.apply(
                [7249],
                "briefs",
                actor="operator",
                request_key=key,
                expected_preview_sha256=proposal["preview_sha256"],
            )["ok"]
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(apply, ["batch-a", "batch-b"])) == [False, True]


@pytest.mark.parametrize(
    "previous_state", ["succeeded", "cancelled", "escalated", "unstarted"]
)
def test_refinement_batch_replenishes_without_draining_unrelated_work(
    database, refinement_batch, previous_state
):
    batch, _ = refinement_batch
    first = batch.preview([7249], "first batch")
    batch.apply(
        [7249],
        "first batch",
        actor="operator",
        request_key="first",
        expected_preview_sha256=first["preview_sha256"],
    )
    with Session(database) as db, db.begin():
        active = db.get(FactoryReceipt, 838)
        active_before = active.model_dump(mode="json")
        if previous_state != "unstarted":
            previous = active.model_dump(exclude={"id", "task_id"})
            previous.update(
                issue_number=7249, task_class="refine", state=previous_state
            )
            db.add(FactoryReceipt(**previous))
    second = batch.preview([7251], "next batch")
    result = batch.apply(
        [7251],
        "next batch",
        actor="operator",
        request_key="second",
        expected_preview_sha256=second["preview_sha256"],
    )
    assert result["ok"] and result["state"] == "paused"
    assert second["policy_before"]["intake"]["refinement_batch"] == [7249]
    assert second["policy_after"]["intake"]["refinement_batch"] == [7251]
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).model_dump(mode="json") == active_before
        assert db.get(FactoryReceipt, 838).state == "admitted"


@pytest.mark.parametrize("previous_state", ["queued", "admitted", "uncertain"])
def test_refinement_batch_cannot_replace_in_flight_batch(
    database, refinement_batch, previous_state
):
    batch, _ = refinement_batch
    first = batch.preview([7249], "first batch")
    batch.apply(
        [7249],
        "first batch",
        actor="operator",
        request_key="first",
        expected_preview_sha256=first["preview_sha256"],
    )
    with Session(database) as db, db.begin():
        previous = db.get(FactoryReceipt, 838).model_dump(exclude={"id", "task_id"})
        previous.update(issue_number=7249, task_class="refine", state=previous_state)
        db.add(FactoryReceipt(**previous))
    with pytest.raises(ValueError):
        batch.preview([7251], "next batch")


@pytest.fixture
def scoped_delivery(database, held_delivery):
    pull, direction = held_delivery
    pull["draft"] = False
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        row.state = "succeeded"
        row.task_paused = False
        row.direction_json = json.dumps(
            {k: v for k, v in direction.items() if k != "landing_hold"}
        )
    return pull


def test_scoped_landing_grant_preview_apply_replay(database, scoped_delivery):
    from factory.orchestration import factory_landing_release as releases

    scope = {
        "classification": "private_internal_reversible",
        "evidence": "Operator inspected exact diff and deployment boundary.",
    }
    proposal = releases.preview(
        838, 7170, "a" * 40, "Land reviewed change", scope=scope
    )
    assert proposal["operation"] == "authorize"
    with Session(database) as db:
        assert releases.scoped_refusal(db, db.get(FactoryReceipt, 838), 7170, "a" * 40)
    args = dict(
        actor="operator",
        request_key="scoped",
        expected_preview_sha256=proposal["preview_sha256"],
        scope=scope,
    )
    result = releases.apply(838, 7170, "a" * 40, "Land reviewed change", **args)
    assert result["ok"] and not result["delivery_accepted"]
    assert releases.apply(838, 7170, "a" * 40, "Land reviewed change", **args) == result
    with Session(database) as db:
        row = db.get(FactoryReceipt, 838)
        assert row.state == "succeeded" and not row.task_paused
        assert releases.scoped_refusal(db, row, 7170, "a" * 40) is None
        assert releases.scoped_refusal(db, row, 7170, "b" * 40)
        assert json.loads(row.direction_json)["conductor_gates"]
        assert db.get(SwarmTask, "task").budget_usd == 50
        assert db.exec(select(FactoryStart)).one().cost_usd == 4
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(
                        FactoryAudit.action == releases.SCOPE_ACTION
                    )
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize(
    "classification", ["public", "security", "destructive", "private", ""]
)
def test_scoped_landing_rejects_other_scope(database, scoped_delivery, classification):
    from factory.orchestration import factory_landing_release as releases

    with pytest.raises(
        ValueError, match="landing_scope_not_private_internal_reversible"
    ):
        releases.preview(
            838,
            7170,
            "a" * 40,
            "reason",
            scope={"classification": classification, "evidence": "evidence"},
        )


@pytest.mark.parametrize("change", ["hold", "paused", "head", "scope"])
def test_scoped_landing_apply_rechecks_preview(database, scoped_delivery, change):
    from factory.orchestration import factory_landing_release as releases

    scope = {
        "classification": "private_internal_reversible",
        "evidence": "Exact diff assessment",
    }
    proposal = releases.preview(838, 7170, "a" * 40, "reason", scope=scope)
    if change in ("hold", "paused"):
        with Session(database) as db, db.begin():
            row = db.get(FactoryReceipt, 838)
            if change == "hold":
                direction = json.loads(row.direction_json)
                direction["landing_hold"] = {"reason": "security review"}
                row.direction_json = json.dumps(direction)
            else:
                row.task_paused = True
    elif change == "head":
        scoped_delivery["head"]["sha"] = "b" * 40
    else:
        scope["evidence"] = "Different assessment"
    with pytest.raises(ValueError):
        releases.apply(
            838,
            7170,
            "a" * 40,
            "reason",
            actor="operator",
            request_key="scoped",
            expected_preview_sha256=proposal["preview_sha256"],
            scope=scope,
        )
    with Session(database) as db:
        row = db.get(FactoryReceipt, 838)
        assert "landing_authorization" not in json.loads(row.direction_json)
        if change == "hold":
            assert controls.landing_held(row)


@pytest.mark.parametrize("task_paused", [False, True])
def test_landing_hold_only_preserves_expired_lease_and_funding(
    database, monkeypatch, task_paused
):
    from factory.orchestration import factory_policy_amendment as amendments
    from factory.orchestration import factory_funding_limits as funding

    paused(database)
    with Session(database) as db, db.begin():
        db.get(FactoryReceipt, 838).task_paused = task_paused
        controls._audit(
            db,
            "factory",
            "funding_granted",
            task_id="task",
            policy_overlay={},
            deadline_at="2020-01-01T00:00:00+00:00",
            review_due_at="2020-01-01T00:00:00+00:00",
        )
        original = funding.amendment(db, "task")
    before = controls.task_snapshot("task")
    overlay = {"landing_hold": True}
    direction = "Preserve explicitly authorized draft-only repository delivery."
    proposal = amendments.preview(838, overlay, direction)
    args = dict(
        actor="operator",
        request_key="hold-only",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    applied = amendments.apply(838, overlay, direction, **args)
    assert amendments.apply(838, overlay, direction, **args) == applied
    after = controls.task_snapshot("task")
    assert after["direction"]["landing_hold"]["request_key"] == "hold-only"
    assert after["task_paused"] == task_paused
    for key in (
        "policy",
        "deadline_at",
        "turns_used",
        "planner_turns_used",
        "committed_cost_usd",
    ):
        assert after[key] == before[key]
    with Session(database) as db:
        assert funding.amendment(db, "task") == original


@pytest.fixture
def recovered_amendment(database, expired_recovery):
    from factory.orchestration import factory_task_recovery as recovery

    args, _, now = expired_recovery
    args = {**args, "max_work_turns": 7}
    proposal = recovery.preview(**args)
    recovery.apply(
        **args,
        actor="operator",
        request_key="recover-for-amendment",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    return now


@pytest.mark.parametrize(
    "overlay", [{"max_review_rounds": 3}, {"max_task_turns_hard": 8}]
)
def test_recovered_amendment_preserves_deadline_and_recovery_grant(
    database, recovered_amendment, overlay
):
    from factory.orchestration import factory_policy_amendment as amendments
    from factory.orchestration import factory_funding_limits as funding

    with Session(database) as db:
        original_recovery = controls.task_recovery_grant("task", session=db)
        original_funding = funding.amendment(db, "task")
        original_task = db.get(SwarmTask, "task").model_dump()
    proposal = amendments.preview(
        838, overlay, "Adjust policy only; retain recovery limits."
    )
    args = dict(
        actor="operator",
        request_key="review-cap-only",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    applied = amendments.apply(
        838, overlay, "Adjust policy only; retain recovery limits.", **args
    )
    assert (
        amendments.apply(
            838, overlay, "Adjust policy only; retain recovery limits.", **args
        )
        == applied
    )
    with Session(database) as db:
        current = funding.amendment(db, "task")
        assert current["deadline_at"] == original_funding["deadline_at"]
        assert current["review_due_at"] == original_funding["review_due_at"]
        assert controls.task_recovery_grant("task", session=db) == original_recovery
        assert db.get(SwarmTask, "task").model_dump() == original_task
        row = db.get(FactoryReceipt, 838)
        assert row.task_paused and controls.landing_held(row)
        assert controls.nonrenewable_recovery(row)
        assert all(
            funding.effective_policy(db, row)[k] == v for k, v in overlay.items()
        )
        assert (
            controls.task_recovery_grant("task", session=db)["work_turn_ceiling"] == 7
        )
    assert (
        controls.task_snapshot("task")["deadline_at"]
        == original_recovery["deadline_at"]
    )


@pytest.mark.parametrize("change", ["recovery", "identical_recovery", "funding"])
def test_recovered_amendment_binds_existing_authority(
    database, recovered_amendment, change
):
    from factory.orchestration import factory_policy_amendment as amendments
    from factory.orchestration import factory_funding_limits as funding

    overlay = {"max_review_rounds": 3}
    proposal = amendments.preview(838, overlay, "Review cap.")
    with Session(database) as db, db.begin():
        if change in {"recovery", "identical_recovery"}:
            action = controls.TASK_RECOVERY_ACTION
            detail = {"proposal": controls.task_recovery_grant("task", session=db)}
            if change == "recovery":
                detail["proposal"]["work_turn_ceiling"] = 6
        else:
            action = "funding_granted"
            detail = funding.amendment(db, "task")
            detail.pop("audit_id")
            detail["review_due_at"] = recovered_amendment.isoformat()
        controls._audit(db, "other-operator", action, task_id="task", **detail)
    with pytest.raises(ValueError, match="policy_amendment_preview_changed"):
        amendments.apply(
            838,
            overlay,
            "Review cap.",
            actor="operator",
            request_key="stale-recovery",
            expected_preview_sha256=proposal["preview_sha256"],
        )


def test_recovered_amendment_preserves_expired_funding_lease(
    database, recovered_amendment
):
    from factory.orchestration import factory_policy_amendment as amendments
    from factory.orchestration import factory_funding_limits as funding

    with Session(database) as db, db.begin():
        prior = funding.amendment(db, "task")
        prior.pop("audit_id")
        prior["review_due_at"] = (
            recovered_amendment - timedelta(minutes=1)
        ).isoformat()
        controls._audit(db, "operator", "funding_granted", task_id="task", **prior)
    proposal = amendments.preview(838, {"max_review_rounds": 3}, "Review cap.")
    amendments.apply(
        838,
        {"max_review_rounds": 3},
        "Review cap.",
        actor="operator",
        request_key="no-renewal",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    with Session(database) as db:
        assert funding.amendment(db, "task")["review_due_at"] == prior["review_due_at"]


def test_recovered_amendment_rejects_expired_absolute_deadline(
    database, recovered_amendment, monkeypatch
):
    from factory.orchestration import factory_policy_amendment as amendments

    monkeypatch.setattr(
        controls, "_now", lambda: recovered_amendment + timedelta(hours=1)
    )
    with pytest.raises(ValueError, match="amendment_task_deadline_expired"):
        amendments.preview(838, {"max_review_rounds": 3}, "Review cap.")


def test_recovered_amendment_cannot_change_timeout(database, recovered_amendment):
    from factory.orchestration import factory_policy_amendment as amendments

    with pytest.raises(ValueError, match="amendment_recovery_deadline_immutable"):
        amendments.preview(838, {"task_timeout_seconds": 86400}, "Change timeout.")


@pytest.fixture
def progress_recovery(database, recovered_amendment, monkeypatch):
    from factory.orchestration import factory_progress_continuation as progress
    from factory.orchestration import factory_conductor as conductor

    head = "a" * 40
    artifact = {
        "pr_number": 3,
        "head_sha": head,
        "verdict": "changes_requested",
        "summary": "Remove the unrelated edits",
    }
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        policy = json.loads(row.policy_json)
        policy["reviewer_model"] = "astra"
        row.policy_json = json.dumps(policy)
        task = db.get(SwarmTask, "task")
        task.repo = "org/repo"
        task.base_branch = "main"
        db.add_all(
            [
                SwarmNodeRun(
                    task_id="task",
                    node_key="implement_saved",
                    attempt=1,
                    status="succeeded",
                    session_id=1,
                    cost_usd=0.1,
                    head_sha=head,
                    outcome_json=json.dumps(
                        {"artifact": {"pr_number": 3, "head_sha": head}}
                    ),
                ),
                AgentSession(
                    id=2,
                    local_session_id="factory:task:review_fix:1",
                    workspace="guest",
                    branch="factory/task",
                    model="astra",
                    node_key="review_fix",
                    node_attempt=1,
                ),
            ]
        )
        db.add(
            SwarmNodeRun(
                task_id="task",
                node_key="review_fix",
                attempt=1,
                session_id=2,
                pin_json=json.dumps({"model": "astra"}),
                status="succeeded",
                head_sha=head,
                model="astra",
                cost_usd=0.1,
                outcome_json=json.dumps({"artifact": artifact}),
            )
        )
    monkeypatch.setattr(
        conductor,
        "_review_recovery_evidence",
        lambda task, review, **kw: {"state": "ready", **conductor._artifact(review)},
    )
    quota = {
        "available": True,
        "providers": {
            "codex": {
                "observed": True,
                "age_seconds": 0,
                "status": "allowed",
                "windows": [
                    {
                        "name": "primary",
                        "used_percent": 20,
                        "expired": False,
                        "resets_at": (
                            recovered_amendment + timedelta(days=1)
                        ).isoformat(),
                    }
                ],
            }
        },
    }
    monkeypatch.setattr(
        progress.provider_quota, "fetch_provider_quota_sync", lambda **kw: quota
    )
    return {
        "receipt_id": 838,
        "pr_number": 3,
        "head_sha": head,
        "direction": "Continue the same artifact; retain landing hold and all accounting.",
    }, quota


def test_progress_authorization_preserves_original_grant_and_accounting(
    database, progress_recovery
):
    from factory.orchestration import factory_progress_continuation as progress
    from factory.orchestration import factory_funding_limits as funding

    args, _ = progress_recovery
    with Session(database) as db:
        original = funding.latest(db, "task", controls.TASK_RECOVERY_ACTION)
        spent = controls._accounting(controls._starts(db, "task"))
        direction = db.get(FactoryReceipt, 838).direction_json
    proposal = progress.preview(**args)
    request = {
        **args,
        "actor": "operator",
        "request_key": "progress-one",
        "expected_preview_sha256": proposal["preview_sha256"],
    }
    first = progress.apply(**request)
    assert first["ok"] and progress.apply(**request) == first
    with Session(database) as db:
        assert funding.latest(db, "task", controls.TASK_RECOVERY_ACTION) == original
        assert controls._accounting(controls._starts(db, "task")) == spent
        row = db.get(FactoryReceipt, 838)
        assert row.direction_json == direction
        assert row.task_id == "task" and not row.task_paused
        assert controls.landing_held(row)
        effective = controls.task_recovery_grant("task", session=db)
        assert effective["deadline_at"] == first["lease"]["deadline_at"]
        assert funding.effective_policy(db, row)["task_budget_usd"] == 50
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(FactoryAudit.action == progress.LEASE)
                ).all()
            )
            == 1
        )
    assert not progress.renew("task")  # No new reviewed head is no new authority.


@pytest.mark.parametrize(
    "change", ["version", "grant", "funding", "head", "uncertain", "pause", "quota"]
)
def test_progress_apply_rechecks_bound_authority(database, progress_recovery, change):
    from factory.orchestration import factory_progress_continuation as progress

    args, quota = progress_recovery
    proposal = progress.preview(**args)
    with Session(database) as db, db.begin():
        if change == "version":
            db.get(FactoryControl, "factory").version += 1
        elif change == "grant":
            raw = db.exec(
                select(FactoryAudit).where(
                    FactoryAudit.action == controls.TASK_RECOVERY_ACTION
                )
            ).first()
            db.add(
                FactoryAudit(
                    actor="other",
                    action=raw.action,
                    task_id="task",
                    detail_json=raw.detail_json,
                )
            )
        elif change == "funding":
            controls._audit(
                db,
                "other",
                "funding_granted",
                task_id="task",
                policy_overlay={"max_task_turns_hard": 8},
            )
        elif change == "head":
            row = db.exec(
                select(SwarmNodeRun).where(SwarmNodeRun.node_key == "review_fix")
            ).one()
            row.outcome_json = row.outcome_json.replace("a" * 40, "b" * 40)
        elif change == "uncertain":
            db.exec(select(FactoryStart)).first().status = "uncertain"
        elif change == "pause":
            db.get(FactoryReceipt, 838).task_paused = False
        elif change == "quota":
            quota["providers"]["codex"]["age_seconds"] = 999999
    with pytest.raises(ValueError):
        progress.apply(
            **args,
            actor="operator",
            request_key="progress-stale",
            expected_preview_sha256=proposal["preview_sha256"],
        )
    with Session(database) as db:
        assert (
            db.exec(
                select(FactoryAudit).where(FactoryAudit.action == progress.ACTION)
            ).first()
            is None
        )


def test_progress_new_review_renews_once_then_repeated_failure_stops(
    database, progress_recovery
):
    from factory.orchestration import factory_progress_continuation as progress

    args, _ = progress_recovery
    p = progress.preview(**args)
    progress.apply(
        **args,
        actor="operator",
        request_key="progress",
        expected_preview_sha256=p["preview_sha256"],
    )
    for i, head in enumerate(("b" * 40, "c" * 40)):
        artifact = {
            "pr_number": 3,
            "head_sha": head,
            "verdict": "changes_requested",
            "summary": "Still not fixed",
        }
        with Session(database) as db, db.begin():
            db.add(
                AgentSession(
                    id=3 + i,
                    local_session_id=f"factory:task:review_next_{i}:1",
                    workspace="guest",
                    branch="factory/task",
                    model="astra",
                    node_key=f"review_next_{i}",
                    node_attempt=1,
                )
            )
            db.add(
                SwarmNodeRun(
                    task_id="task",
                    node_key=f"review_next_{i}",
                    attempt=1,
                    session_id=3 + i,
                    pin_json=json.dumps({"model": "astra"}),
                    status="succeeded",
                    head_sha=head,
                    model="astra",
                    cost_usd=0.1,
                    outcome_json=json.dumps({"artifact": artifact}),
                )
            )
        assert progress.renew("task") is (i == 0)
    with Session(database) as db:
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(FactoryAudit.action == progress.LEASE)
                ).all()
            )
            == 2
        )


@pytest.mark.parametrize(
    "problem", ["unavailable", "exhausted", "unobserved", "missing_windows"]
)
def test_progress_subscription_evidence_fails_closed(progress_recovery, problem):
    from factory.orchestration import factory_progress_continuation as progress

    args, quota = progress_recovery
    if problem == "unavailable":
        quota["available"] = False
    elif problem == "exhausted":
        quota["providers"]["codex"]["windows"][0]["used_percent"] = 100
    elif problem == "unobserved":
        quota["providers"]["codex"]["observed"] = False
    else:
        quota["providers"]["codex"]["windows"] = []
    with pytest.raises(ValueError, match="progress_"):
        progress.preview(**args)


@pytest.mark.parametrize("restriction", ["pause", "cancel", "uncertain"])
def test_progress_renewal_does_not_clear_later_restrictions(
    database, progress_recovery, restriction
):
    from factory.orchestration import factory_progress_continuation as progress

    args, _ = progress_recovery
    proposal = progress.preview(**args)
    progress.apply(
        **args,
        actor="operator",
        request_key="progress",
        expected_preview_sha256=proposal["preview_sha256"],
    )
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        if restriction == "pause":
            row.task_paused = True
        elif restriction == "cancel":
            row.cancellation_requested = True
        else:
            db.exec(select(FactoryStart)).first().status = "uncertain"
        db.add(
            AgentSession(
                id=3,
                local_session_id="factory:task:review_later:1",
                workspace="guest",
                branch="factory/task",
                model="astra",
                node_key="review_later",
                node_attempt=1,
            )
        )
        db.add(
            SwarmNodeRun(
                task_id="task",
                node_key="review_later",
                attempt=1,
                session_id=3,
                pin_json=json.dumps({"model": "astra"}),
                status="succeeded",
                model="astra",
                cost_usd=0.1,
                head_sha="b" * 40,
                outcome_json=json.dumps(
                    {
                        "artifact": {
                            "pr_number": 3,
                            "head_sha": "b" * 40,
                            "verdict": "approve",
                            "summary": "Reviewed",
                        }
                    }
                ),
            )
        )
    assert not progress.renew("task")
    with Session(database) as db:
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(FactoryAudit.action == progress.LEASE)
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize(
    "fault",
    [
        "same_session",
        "missing_session",
        "foreign_session",
        "wrong_model",
        "missing_implementation",
        "implementation_session_missing",
    ],
)
def test_progress_requires_native_independent_review(
    database, progress_recovery, fault
):
    from factory.orchestration import factory_progress_continuation as progress

    args, _ = progress_recovery
    with Session(database) as db, db.begin():
        review = db.exec(
            select(SwarmNodeRun).where(SwarmNodeRun.node_key == "review_fix")
        ).one()
        implementation = db.exec(
            select(SwarmNodeRun).where(SwarmNodeRun.node_key == "implement_saved")
        ).one()
        if fault == "same_session":
            review.session_id = 1
        elif fault == "missing_session":
            review.session_id = None
        elif fault == "foreign_session":
            db.get(AgentSession, 2).local_session_id = "factory:other:review_fix:1"
        elif fault == "wrong_model":
            review.pin_json = json.dumps({"model": "spark"})
        elif fault == "missing_implementation":
            implementation.status = "failed"
        else:
            implementation.session_id = None
    with pytest.raises(
        ValueError, match="progress_(independent_review|review_provenance)_missing"
    ):
        progress.preview(**args)


@pytest.mark.parametrize("lane", ["allowed_models", "model_pools"])
def test_progress_refuses_non_subscription_execution_lane(
    database, progress_recovery, lane
):
    from factory.orchestration import factory_progress_continuation as progress

    args, _ = progress_recovery
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        policy = json.loads(row.policy_json)
        if lane == "allowed_models":
            policy["allowed_models"].append("muse")
        else:
            policy["model_pools"] = {"implement": ["muse", "astra"]}
        row.policy_json = json.dumps(policy)
    with pytest.raises(ValueError, match="progress_subscription_evidence_stale"):
        progress.preview(**args)


def test_progress_failed_latest_review_returns_to_normal_reconciliation(
    database, progress_recovery
):
    from factory.orchestration import factory_progress_continuation as progress

    args, _ = progress_recovery
    p = progress.preview(**args)
    progress.apply(
        **args,
        actor="operator",
        request_key="progress",
        expected_preview_sha256=p["preview_sha256"],
    )
    with Session(database) as db, db.begin():
        db.add(
            SwarmNodeRun(
                task_id="task",
                node_key="review_failed",
                attempt=1,
                status="failed",
                model="astra",
                cost_usd=0.1,
            )
        )
    assert progress.renew("task") is False
    with Session(database) as db:
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(FactoryAudit.action == progress.LEASE)
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize(
    "fault, expected",
    [
        ("spark", "unsupported_subscription_feed"),
        ("stale", "stale_observation"),
        ("missing", "missing_observation"),
        ("exhausted", "subscription_unavailable"),
    ],
)
def test_progress_refusal_identifies_exact_feed_without_mutation(
    database, progress_recovery, fault, expected
):
    from factory.orchestration import factory_progress_continuation as progress

    args, raw = progress_recovery
    raw["providers"]["codex"]["observed_at"] = "2026-10-30T02:40:00Z"
    if fault == "stale":
        raw["providers"]["codex"]["age_seconds"] = 901
    elif fault == "missing":
        raw["providers"] = {}
    elif fault == "exhausted":
        raw["providers"]["codex"]["windows"][0]["used_percent"] = 100
    with Session(database) as db, db.begin():
        row = db.get(FactoryReceipt, 838)
        if fault == "spark":
            policy = json.loads(row.policy_json)
            policy["model_pools"] = {"implement": ["astra", "spark"]}
            row.policy_json = json.dumps(policy)
        before = row.model_dump()
        audits = len(db.exec(select(FactoryAudit)).all())
    with pytest.raises(progress.SubscriptionRefusal) as caught:
        progress.preview(**args)
    detail = caught.value.diagnostics
    refused = detail["refused_model"]
    assert refused["state"] == expected
    assert detail["effect"] == "read_only_refusal_no_admission"
    if fault == "spark":
        assert refused["model"] == "spark" and refused["family"] == "muse"
        assert refused["provider"] is None and refused["observed_at"] is None
        assert refused["required_by"] == ["model_pools.implement"]
        assert any(m["state"] == "fresh_admissible" for m in detail["models"])
    elif fault == "stale":
        assert refused["provider"] == "codex" and refused["age_seconds"] == 901
        assert refused["observed_at"] == "2026-10-30T02:40:00Z"
    with Session(database) as db:
        assert db.get(FactoryReceipt, 838).model_dump() == before
        assert len(db.exec(select(FactoryAudit)).all()) == audits


def test_progress_subscription_diagnostic_is_bounded_and_keeps_exact_refusal():
    from factory.orchestration import factory_progress_continuation as progress

    policy = {"allowed_models": [f"unknown{i:02}" for i in range(40)]}
    detail = progress._subscription_diagnostics(
        policy, [], {"providers": {}}, {}, "unknown39"
    )
    assert len(detail["models"]) == 32 and detail["truncated"]
    assert detail["refused_model"]["model"] == "unknown39"
