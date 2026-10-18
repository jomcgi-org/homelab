"""Deterministic candidate-policy fixtures; no production policy is activated."""

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


@pytest.mark.parametrize("kind", sorted(shadow.SEMANTIC_KINDS))
def test_verified_negative_or_positive_evidence_changes_decision(kind):
    observation = shadow.VerifiedEvidence(
        kind,
        "cache hypothesis",
        "reproduction still fails with cache disabled",
        "test transport isolation next",
        "verified-result:1",
    )
    assert shadow.classify([observation])["classification"] == "progressing"
    # Repetition with a different receipt/node name is not fresh evidence.
    renamed = shadow.VerifiedEvidence(
        kind,
        observation.subject,
        observation.result,
        observation.next_action,
        "renamed-node:2",
    )
    assert shadow.classify([observation, renamed])["classification"] == "stalled"


def test_changed_verified_next_action_counts_but_claims_and_commits_do_not():
    old = shadow.VerifiedEvidence(
        "test_result", "transport test", "fails", "isolate network", "test:1"
    )
    new = shadow.VerifiedEvidence(
        "hypothesis_eliminated",
        "DNS",
        "independent reproduction excludes DNS",
        "isolate TLS",
        "test:2",
    )
    assert shadow.classify([old, new])["classification"] == "progressing"
    for claim in (
        {"summary": "making progress"},
        {"head_sha": "a" * 40, "pr_number": 1},
        {"tests_passed": True},
        {"kind": "test_result", "source": "worker says so"},
    ):
        assert shadow.classify([claim])["classification"] == "insufficient_evidence"
    assert shadow.classify([old, new, old])["classification"] == "stalled"


def test_wait_and_authority_take_precedence_over_activity():
    assert shadow.classify([], waiting=True)["classification"] == "waiting"
    assert (
        shadow.classify([], waiting=True, authority_blocked=True)["classification"]
        == "authority_blocked"
    )
    assert shadow.classify([])["classification"] == "insufficient_evidence"


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


def test_price_projection_deduplicates_turns_and_never_calls_reported_cost_cash():
    first = turn(1, listed=500, reported=2)
    report = shadow.price_projection(
        [start(1, cost=2), start(2, cost=3)],
        [first, first, turn(2, listed=499.9, reported=3)],
        supervision_list_usd=0.1,
    )
    assert report["known_list_usd"] == 1000
    assert report["observed_objective_backstop_exceeded"]
    assert report["sessions"][0]["observed_backstop_exceeded"]
    assert report["sessions"][0]["provider_reported_usd"] == 2
    assert report["known_cash_cost_usd"] is None
    assert report["subscription_quota"] is None
    assert not report["hard_stop_enforced"]
    assert not report["within_backstop_verified"]


def test_unknown_failed_preflight_and_active_exposure_are_never_refunded():
    report = shadow.price_projection(
        [
            start(1),
            start(2, status="uncertain", cost=1),
            start(None, cost=3),
            start(None, cost=0, basis="no_model_post"),
        ],
        [turn(1), turn(2, listed=1)],
        session_ids=[1, 2, 3],
    )
    assert report["known_list_usd"] == 1
    assert report["unknown_or_reserved_exposure_usd"] == 12
    assert report["sessions"][0]["unpriced_turns"] == 1
    assert report["sessions"][0]["unbounded_unknown_usage"]
    assert report["sessions"][1]["unbounded_unknown_usage"]
    assert report["sessions"][2]["unbounded_unknown_usage"]
    assert report["sessions"][2]["missing_turns"]


@pytest.mark.parametrize("listed", [None, float("nan"), float("inf"), -1, True])
def test_invalid_or_missing_prices_remain_unknown(listed):
    report = shadow.price_projection([start(1)], [turn(1, listed=listed)])
    assert report["known_list_usd"] == 0
    assert report["unknown_or_reserved_exposure_usd"] == 4
    assert report["sessions"][0]["unpriced_turns"] == 1


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


def test_objective_readmission_uses_immutable_history_once_and_keeps_old_limits(db):
    with Session(db) as session:
        receipt = session.get(FactoryReceipt, 838)
        # Also discover each session through native prefix links. Do not count
        # that session twice, or accidentally include the unrelated objective.
        result = shadow.snapshot(session, receipt, POLICY)
        assert result["objective_task_ids"] == ["current", "old"]
        assert result["prices"]["known_list_usd"] == 700
        assert len(result["prices"]["sessions"]) == 2
        assert result["binding_limits"] == {
            "task_budget_usd": 50,
            "objective_budget_usd": 200,
            "max_task_turns_hard": 9,
            "turn_budget_usd": 4,
        }
        assert result["progress"]["classification"] == "authority_blocked"


def test_shadow_observes_below_spend_gate_without_model_or_control_mutations(
    db, monkeypatch
):
    monkeypatch.setenv("FACTORY_PROGRESS_SHADOW_ENABLED", "true")
    monkeypatch.setattr(
        controls, "set_control", lambda *a, **kw: pytest.fail("shadow changed control")
    )
    monkeypatch.setattr(
        watchdog, "_clear", lambda *a, **kw: pytest.fail("shadow cleared watchdog")
    )
    monkeypatch.setattr(
        watchdog, "assess", lambda *a, **kw: pytest.fail("shadow called paid judge")
    )
    with Session(db) as session:
        for row in session.exec(select(FactoryStart)).all():
            row.cost_usd = 0
        session.commit()
    assert (
        watchdog.check("current", {**POLICY, "progress_watchdog": {"enabled": True}})
        == "under_threshold"
    )
    assert watchdog.check("current", POLICY) == "disabled"
    with Session(db) as session:
        audits = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == shadow.ACTION)
        ).all()
        assert len(audits) == 1
        assert session.get(FactoryReceipt, 838).task_paused
        assert json.loads(audits[0].detail_json)["mode"] == "shadow"
        assert session.exec(select(FactoryStart)).all()[0].max_cost_usd == 4


def test_shadow_disabled_does_not_read_or_write_new_state(db, monkeypatch):
    monkeypatch.delenv("FACTORY_PROGRESS_SHADOW_ENABLED", raising=False)
    monkeypatch.setattr(
        shadow, "snapshot", lambda *a: pytest.fail("disabled shadow read")
    )
    assert watchdog.check("current", POLICY) == "disabled"
    with Session(db) as session:
        assert (
            session.exec(
                select(FactoryAudit).where(FactoryAudit.action == shadow.ACTION)
            ).all()
            == []
        )


def test_collector_keeps_blobs_out_of_price_reads_and_marks_unpriced_supervision(db):
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
        session.add(
            FactoryAudit(
                task_id="old",
                actor="factory:watchdog",
                action="watchdog_assessed",
                detail_json='{"cost_usd":null}',
            )
        )
        session.commit()
        result = shadow.snapshot(session, session.get(FactoryReceipt, 838), POLICY)
        assert result["prices"]["unpriced_supervision_assessments"] == 1
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


def test_shadow_failure_does_not_disable_legacy_watchdog_or_resume(db, monkeypatch):
    monkeypatch.setenv("FACTORY_PROGRESS_SHADOW_ENABLED", "true")

    def broken(*args):
        raise ValueError("unreadable candidate evidence")

    monkeypatch.setattr(shadow, "snapshot", broken)
    with Session(db) as session:
        for row in session.exec(select(FactoryStart)).all():
            row.cost_usd = 0
        session.commit()
    assert (
        watchdog.check("current", {**POLICY, "progress_watchdog": {"enabled": True}})
        == "under_threshold"
    )
    with Session(db) as session:
        assert session.get(FactoryReceipt, 838).task_paused
        assert (
            session.exec(
                select(FactoryAudit).where(FactoryAudit.action == shadow.ACTION)
            ).all()
            == []
        )
