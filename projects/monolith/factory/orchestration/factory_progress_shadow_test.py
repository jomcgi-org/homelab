"""Retired shadow writes leave objective accounting and existing holds intact."""

import json
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from factory.execution.models import AgentSession, AgentTurn
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_progress_shadow as shadow
from factory.orchestration import factory_progress_watchdog as watchdog
from factory.orchestration import graph
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryReceipt,
    FactoryStart,
)
from factory.orchestration.models import SwarmNodeRun, SwarmPlanNode, SwarmTask


def start(session_id, *, status="failed", cost=None, reservation=4, basis=None):
    return SimpleNamespace(
        session_id=session_id,
        status=status,
        cost_usd=cost,
        max_cost_usd=reservation,
        accounting_basis=basis,
    )


def turn(session_id, seq=1, *, listed=None, reported=None):
    return SimpleNamespace(
        session_id=session_id, seq=seq, list_cost_usd=listed, cost_usd=reported
    )


@pytest.mark.parametrize("status", ["reserved", "uncertain", "failed", "succeeded"])
@pytest.mark.parametrize("listed", [None, 0, 500])
def test_exposure_keeps_unknown_or_inflight_charges(status, listed):
    held = listed is None or status in ("reserved", "uncertain")
    assert shadow.exposure(
        [start(1, status=status, cost=6)], [turn(1, listed=listed)]
    ) == (6 if held else 0)


def test_unknown_failed_preflight_and_active_exposure_are_never_refunded():
    assert (
        shadow.exposure(
            [
                start(1),
                start(2, status="uncertain", cost=1),
                start(None, cost=3),
                start(None, cost=0, basis="no_model_post"),
            ],
            [turn(1), turn(2, listed=1)],
        )
        == 12
    )


@pytest.mark.parametrize("listed", [None, float("nan"), float("inf"), -1, True])
def test_invalid_or_missing_prices_remain_unknown(listed):
    assert shadow.exposure([start(1)], [turn(1, listed=listed)]) == 4


@pytest.mark.parametrize(
    "basis", ["no_model_post", "capacity_denied", "no_session_created"]
)
def test_only_positive_no_execution_proof_removes_exposure(basis):
    assert shadow.exposure([start(None, basis=basis), start(1, basis=basis)], []) == 0


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'shadow.db'}",
        execution_options={
            "schema_translate_map": {"swarm": None, "agent_sessions": None}
        },
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                SwarmTask,
                FactoryReceipt,
                FactoryStart,
                FactoryAudit,
                SwarmNodeRun,
                SwarmPlanNode,
                AgentSession,
                AgentTurn,
            )
        ],
    )
    for module in (controls, graph):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    with Session(engine) as session:
        tasks = [
            SwarmTask(
                id=key, task_text="fixture", conductor_model="astra", budget_usd=50
            )
            for key in ("old", "current", "unrelated")
        ]
        receipt = FactoryReceipt(
            id=838,
            repo="org/repo",
            issue_number=7001,
            title="test",
            body="",
            url="https://example.invalid",
            actor="operator",
            task_id="current",
            state="admitted",
            task_paused=True,
        )
        session.add_all(tasks + [receipt])
        session.add(
            FactoryAudit(
                task_id="old",
                actor="operator",
                action="admit_next",
                detail_json=json.dumps({"receipt_id": 838}),
            )
        )
        session.add_all(
            [
                AgentSession(
                    id=index,
                    local_session_id=f"factory:{key}:implement:1",
                    workspace="fixture",
                    branch="branch",
                    status="completed",
                )
                for index, key in enumerate(("old", "current", "unrelated"), 1)
            ]
        )
        session.add_all(
            [
                AgentTurn(
                    session_id=index,
                    seq=1,
                    prompt="fixture",
                    result_text="",
                    list_cost_usd=cost,
                )
                for index, cost in enumerate((400, 300, 999), 1)
            ]
        )
        session.add_all(
            [
                FactoryStart(
                    task_id=key,
                    start_key="start",
                    actor="factory",
                    model="astra",
                    max_cost_usd=4,
                    cost_usd=cost,
                    status="succeeded",
                    session_id=index,
                )
                for index, (key, cost) in enumerate((("old", 400), ("current", 300)), 1)
            ]
        )
        session.commit()
    yield engine
    engine.dispose()


POLICY = {
    "task_budget_usd": 50,
    "turn_budget_usd": 4,
    "max_task_turns_hard": 9,
    "progress_watchdog": {"enabled": False},
}


def test_objective_readmission_uses_immutable_history_once_without_mutations(db):
    with Session(db) as session:
        receipt = session.get(FactoryReceipt, 838)
        # Also discover each session through native prefix links. Do not count
        # that session twice, or accidentally include the unrelated objective.
        result = shadow.snapshot(session, receipt)
        assert result["objective_task_ids"] == ["current", "old"]
        assert sum(turn.list_cost_usd for turn in result["turns"]) == 700
        assert len(result["turns"]) == 2
        assert result["unknown_or_reserved_exposure_usd"] == 0
        assert receipt.task_paused
        assert not session.dirty
        assert not session.new


@pytest.mark.parametrize("legacy_flag", ["false", "true"])
@pytest.mark.parametrize("watch_enabled", [False, True])
def test_retired_shadow_flag_never_records_or_changes_holds(
    db, monkeypatch, legacy_flag, watch_enabled
):
    monkeypatch.setenv("FACTORY_PROGRESS_SHADOW_ENABLED", legacy_flag)
    monkeypatch.setattr(
        shadow, "snapshot", lambda *a: pytest.fail("watchdog read retired shadow")
    )
    monkeypatch.setattr(
        controls, "set_control", lambda *a, **kw: pytest.fail("shadow changed control")
    )
    monkeypatch.setattr(
        watchdog, "_clear", lambda *a, **kw: pytest.fail("shadow cleared watchdog")
    )
    from factory.orchestration import factory_funding

    monkeypatch.setattr(
        factory_funding,
        "request",
        lambda *a, **kw: pytest.fail("shadow called paid judge"),
    )
    with Session(db) as session:
        for row in session.exec(select(FactoryStart)).all():
            row.cost_usd = 0
        session.commit()
    assert watchdog.check(
        "current", {**POLICY, "progress_watchdog": {"enabled": watch_enabled}}
    ) == ("under_threshold" if watch_enabled else "disabled")
    with Session(db) as session:
        audits = session.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "progress_shadow_assessed"
            )
        ).all()
        assert audits == []
        assert session.get(FactoryReceipt, 838).task_paused
        assert session.exec(select(FactoryStart)).all()[0].max_cost_usd == 4


def test_collector_keeps_blobs_out_of_price_reads_and_preserves_supervision(db):
    from sqlalchemy import event

    queries = []
    event.listen(
        db,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, executemany: (
            queries.append(statement)
        ),
    )
    with Session(db) as session:
        session.add_all(
            [
                FactoryAudit(
                    task_id="old",
                    actor="factory:watchdog",
                    action="watchdog_assessed",
                    detail_json=json.dumps({"cost_usd": cost}),
                )
                for cost in [None, 0.5]
            ]
        )
        session.commit()
        result = shadow.snapshot(session, session.get(FactoryReceipt, 838))
        assert result["supervision_list_usd"] == 0.5
    turn_queries = [
        query for query in queries if "SELECT" in query and "agent_turns" in query
    ]
    assert len(turn_queries) == 1
    assert all(
        field not in turn_queries[0]
        for field in (
            "diff_blob",
            "artifact_blob",
            "result_text",
            "prompt",
            "usage_json",
        )
    )


def test_historical_shadow_is_still_readable_without_recomputing_it(db):
    with Session(db) as session:
        previous = {"mode": "shadow", "progress": {"classification": "waiting"}}
        session.add(
            FactoryAudit(
                task_id="current",
                actor="factory:progress-shadow",
                action="progress_shadow_assessed",
                detail_json=json.dumps(previous),
            )
        )
        session.commit()
        assert watchdog.detail(session, "current", POLICY)["shadow"] == previous
        assert session.get(FactoryReceipt, 838).task_paused
