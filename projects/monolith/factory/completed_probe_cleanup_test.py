"""Completed probe cleanup requires unchanged delivery and positive cessation."""

import asyncio
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

from factory import completed_probe_cleanup as cleanup
from factory.execution import execution_api, store
from factory.execution.constants import UNKNOWN_INVOCATION, exact_dispatch_id
from factory.execution.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from factory.orchestration.factory_models import FactoryAudit, FactoryControl
from factory.orchestration.models import SwarmTask

BASE = datetime(2026, 9, 1, tzinfo=UTC)
MODELS = (
    SwarmTask,
    FactoryControl,
    FactoryAudit,
    AgentSession,
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentTurn,
    AgentResultReceipt,
    PendingMessage,
)


@pytest.fixture
def db(tmp_path, monkeypatch, request):
    """Exercise identical assertions in file SQLite and isolated PG schemas."""
    postgres = os.getenv("COMPLETED_PROBE_TEST_POSTGRES") == "1"
    if postgres:
        pg = request.getfixturevalue("pg")
        suffix = uuid4().hex
        schemas = {
            name: f"cleanup_{name}_{suffix}" for name in ("swarm", "agent_sessions")
        }
        engine = create_engine(
            pg.url, execution_options={"schema_translate_map": schemas}
        )
        with engine.begin() as conn:
            for name in schemas.values():
                conn.execute(text(f'CREATE SCHEMA "{name}"'))
    else:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'completed_probe.db'}",
            connect_args={"check_same_thread": False, "timeout": 10},
            execution_options={
                "schema_translate_map": {"swarm": None, "agent_sessions": None}
            },
        )
    SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in MODELS])
    with Session(engine) as session:
        session.add_all(
            [
                FactoryControl(id="factory", actor="test", version=287),
                AgentCapacityPool(id=1),
            ]
        )
        session.commit()
    monkeypatch.setattr(cleanup.controls, "get_engine", lambda: engine)
    yield engine
    if postgres:
        with engine.begin() as conn:
            for name in schemas.values():
                conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
    engine.dispose()


def seed(db, number=1):
    """Independent native-result fixture, including its original dispatch audit."""
    local = f"{cleanup.PREFIX}claude:{number}"
    guest = f"s-completed-probe-{number}"
    receipt_id = f"completed-probe-receipt-{number}"
    owner = f"monolith-original-{number}"
    cli = f"cli-completed-{number}"
    body = json.dumps(
        {
            "result": "OK",
            "terminal_reason": "completed",
            "stop_reason": "end_turn",
            "is_error": False,
            "session_id": cli,
            "usage": {"input_tokens": 12, "output_tokens": 1},
        }
    ).encode()
    result_sha = hashlib.sha256(body).hexdigest()
    received_at = BASE + timedelta(seconds=2)
    with Session(db) as session:
        agent = AgentSession(
            local_session_id=local,
            workspace="quota-probe",
            branch="main",
            model="haiku",
            admission_tier="probe",
            status="completed",
            ember_session_id=guest,
            ember_lineage_id=guest,
            ember_session_token="private-fixture-token",
            ember_session_expires_at=1,
            cli_session_id=cli,
            result_receipt_fence_id=receipt_id,
            created_at=BASE,
            last_turn_at=BASE + timedelta(seconds=3),
        )
        session.add(agent)
        session.flush()
        session_id = agent.id
        provenance = {
            "session_id": session_id,
            "local_session_id": local,
            "seq": 1,
            "claim_owner": owner,
            "dispatch_count": 1,
            "guest_id": guest,
            "request_sha256": "a" * 64,
            "result_sha256": result_sha,
            "receipt_id": receipt_id,
            "received_at": received_at.isoformat(),
        }
        session.add_all(
            [
                AgentCapacityReservation(
                    local_session_id=local,
                    pending_seq=1,
                    session_id=session_id,
                    tier="probe",
                    model="haiku",
                    workload="claude-runtime",
                    owner=owner,
                    state="settled",
                    outcome="completed",
                    daily_key="preserved-accounting-key",
                    created_at=BASE,
                    settled_at=BASE + timedelta(seconds=4),
                ),
                AgentTurn(
                    session_id=session_id,
                    seq=1,
                    prompt="Return OK",
                    model="haiku",
                    result_text="OK",
                    terminal_reason="completed",
                    stop_reason="end_turn",
                    usage_json=json.dumps(
                        {"native_result_receipt": provenance, "input_tokens": 12}
                    ),
                    cost_usd=0,
                    list_cost_usd=0.001,
                    created_at=BASE + timedelta(seconds=3),
                ),
                AgentResultReceipt(
                    id=receipt_id,
                    token_sha256=hashlib.sha256(receipt_id.encode()).hexdigest(),
                    session_id=session_id,
                    local_session_id=local,
                    seq=1,
                    dispatch_count=1,
                    claim_owner=owner,
                    guest_id=guest,
                    request_sha256="a" * 64,
                    created_at=BASE,
                    accept_until=BASE + timedelta(hours=1),
                    retain_until=BASE + timedelta(days=30),
                    received_at=received_at,
                    result_sha256=result_sha,
                    result_body=body,
                ),
                FactoryAudit(
                    actor="factory:quota-probe",
                    action="quota_probe_started",
                    detail_json=json.dumps({"session_key": local, "model": "haiku"}),
                    created_at=BASE,
                ),
            ]
        )
        session.commit()
    milliseconds = lambda seconds: int(
        (BASE + timedelta(seconds=seconds)).timestamp() * 1000
    )
    observed = {
        "session_id": guest,
        "lineage_id": guest,
        "workload": "claude-runtime",
        "state": "destroyed",
        "terminal_reason": "destroyed",
        "generation": 0,
        "turn_seq": 1,
        "invoke_started_at": milliseconds(1),
        "last_invoke_at": milliseconds(2),
        "updated_at": milliseconds(5),
        "stop_intent": None,
        "interrupted_turn": None,
        "inflight_dispatch_id": f"{guest}:{milliseconds(1)}",
        "private": "must-not-be-audited",
    }
    return session_id, observed


def records(db):
    with Session(db) as session:
        return {
            model.__name__: [
                row.model_dump()
                for row in session.exec(select(model).order_by(model.id)).all()
            ]
            for model in MODELS
        }


def mutate(db, session_id, row_type, **changes):
    with Session(db) as session:
        if row_type is AgentSession:
            row = session.get(row_type, session_id)
        elif row_type is FactoryAudit:
            local = session.get(AgentSession, session_id).local_session_id
            row = session.exec(
                select(row_type).where(row_type.detail_json.contains(local))
            ).one()
        else:
            row = session.exec(
                select(row_type).where(row_type.session_id == session_id)
            ).one()
        for name, value in changes.items():
            setattr(row, name, value)
        session.add(row)
        session.commit()


@pytest.mark.parametrize("dispatch", ["legacy", "durable"])
def test_exact_completed_probe_retires_only_binding_and_fence_once(db, dispatch):
    session_id, observed = seed(db)
    other_id, _ = seed(db, 2)
    before = records(db)
    snapshot = cleanup._read(session_id)
    if dispatch == "durable":
        observed["inflight_dispatch_id"] = exact_dispatch_id(
            session_id, snapshot["guest_id"], 1, snapshot["claim_owner"], 1
        )
    assert cleanup._retire(snapshot, observed, time.monotonic())
    after = records(db)
    expected_agents = before["AgentSession"]
    expected = next(row for row in expected_agents if row["id"] == session_id)
    expected["prior_ember_lineage_id"] = expected["ember_lineage_id"]
    expected["prior_cli_session_id"] = expected["cli_session_id"]
    for field in (
        "ember_session_id",
        "ember_session_token",
        "ember_session_expires_at",
        "ember_lineage_id",
        "cli_session_id",
        "result_receipt_fence_id",
    ):
        expected[field] = None
    assert after["AgentSession"] == expected_agents
    assert next(row for row in after["AgentSession"] if row["id"] == other_id)[
        "result_receipt_fence_id"
    ]
    for model in MODELS:
        if model not in (AgentSession, FactoryAudit):
            assert after[model.__name__] == before[model.__name__]
    assert after["FactoryAudit"][:-1] == before["FactoryAudit"]
    audit = after["FactoryAudit"][-1]
    assert audit["action"] == cleanup.ACTION
    assert audit["actor"] == "factory:quota-probe-cleanup"
    detail = json.loads(audit["detail_json"])
    assert {key: detail[key] for key in snapshot} == snapshot
    assert detail["terminal_evidence"]["session_id"] == observed["session_id"]
    assert "must-not-be-audited" not in audit["detail_json"]
    assert "private-fixture-token" not in audit["detail_json"]
    with pytest.raises(ValueError):
        cleanup._retire(snapshot, observed, time.monotonic())
    assert records(db) == after
    assert session_id not in cleanup._candidates(0)


@pytest.mark.parametrize(
    "model,changes",
    [
        (AgentSession, {"status": "running"}),
        (AgentSession, {"admission_tier": "interactive"}),
        (AgentSession, {"model": "opus"}),
        (AgentSession, {"local_session_id": "synthetic:not-a-quota-probe"}),
        (AgentSession, {"workflow_id": "owned-workflow"}),
        (AgentSession, {"node_key": "new-owner"}),
        (AgentSession, {"node_attempt": 2}),
        (AgentSession, {"ember_session_id": "s-successor"}),
        (AgentSession, {"ember_lineage_id": "s-another-lineage"}),
        (AgentSession, {"prior_ember_lineage_id": "s-retained-ancestor"}),
        (AgentSession, {"prior_cli_session_id": "cli-retained-ancestor"}),
        (AgentSession, {"result_receipt_fence_id": "new-fence"}),
        (AgentSession, {"guest_cleanup_id": "existing-cleanup"}),
        (AgentCapacityReservation, {"state": "uncertain"}),
        (AgentCapacityReservation, {"outcome": "unknown_invocation"}),
        (AgentCapacityReservation, {"owner": "other-replica"}),
        (AgentCapacityReservation, {"routine_job_name": "factory-retro-daily"}),
        (AgentCapacityReservation, {"model": "opus"}),
        (AgentCapacityReservation, {"workload": "codex-runtime"}),
        (AgentCapacityReservation, {"settled_at": None}),
        (AgentTurn, {"stop_reason": UNKNOWN_INVOCATION}),
        (AgentTurn, {"result_text": "Changed saved result"}),
        (AgentTurn, {"usage_json": "{}"}),
        (AgentResultReceipt, {"dispatch_count": 2}),
        (AgentResultReceipt, {"received_at": None}),
        (AgentResultReceipt, {"superseded_at": BASE}),
        (AgentResultReceipt, {"result_sha256": "b" * 64}),
        (AgentResultReceipt, {"result_body": b"{}"}),
        (AgentResultReceipt, {"guest_id": "s-other"}),
        (FactoryAudit, {"actor": "untrusted"}),
        (FactoryAudit, {"detail_json": "{}"}),
    ],
)
def test_changed_or_unknown_identity_fails_closed(db, model, changes):
    session_id, observed = seed(db)
    snapshot = cleanup._read(session_id)
    mutate(db, session_id, model, **changes)
    before = records(db)
    with pytest.raises(ValueError):
        cleanup._retire(snapshot, observed, time.monotonic())
    assert records(db) == before


@pytest.mark.parametrize(
    "field", ["ember_session_id", "ember_lineage_id", "prior_ember_lineage_id"]
)
def test_conflicting_guest_alias_is_rechecked_under_lock(db, field):
    session_id, observed = seed(db)
    snapshot = cleanup._read(session_id)
    other_id, _ = seed(db, 2)
    mutate(db, other_id, AgentSession, **{field: snapshot["guest_id"]})
    before = records(db)
    with pytest.raises(ValueError, match="probe_alias_present"):
        cleanup._retire(snapshot, observed, time.monotonic())
    assert records(db) == before


@pytest.mark.parametrize("new_work", ["pending", "turn", "permit", "receipt", "origin"])
def test_new_work_or_ambiguous_history_blocks_cleanup(db, new_work):
    session_id, observed = seed(db)
    snapshot = cleanup._read(session_id)
    with Session(db) as session:
        if new_work == "pending":
            extra = PendingMessage(
                session_id=session_id, seq=2, message_text="new work"
            )
        elif new_work == "turn":
            extra = AgentTurn(
                session_id=session_id, seq=2, prompt="new", result_text="new"
            )
        elif new_work == "permit":
            extra = AgentCapacityReservation(
                session_id=session_id,
                local_session_id="new-local",
                pending_seq=2,
                tier="probe",
            )
        elif new_work == "receipt":
            original = session.exec(
                select(AgentResultReceipt).where(
                    AgentResultReceipt.session_id == session_id
                )
            ).one()
            extra = AgentResultReceipt(
                **{
                    **original.model_dump(),
                    "id": "extra-receipt",
                    "token_sha256": "c" * 64,
                    "seq": 2,
                }
            )
        else:
            original = session.exec(select(FactoryAudit)).one()
            extra = FactoryAudit(**{**original.model_dump(), "id": None})
        session.add(extra)
        session.commit()
    before = records(db)
    with pytest.raises(ValueError):
        cleanup._retire(snapshot, observed, time.monotonic())
    assert records(db) == before


@pytest.mark.parametrize(
    "model,changes",
    [
        (AgentSession, {"ember_session_token": "rotated-fixture-token"}),
        (AgentSession, {"ember_session_expires_at": 2}),
        (AgentTurn, {"cost_usd": 0.5}),
        (AgentCapacityReservation, {"daily_key": "changed-accounting"}),
        (AgentResultReceipt, {"response_observed_at": BASE + timedelta(seconds=6)}),
        (
            AgentResultReceipt,
            {"response_observer_released_at": BASE + timedelta(seconds=6)},
        ),
        (FactoryAudit, {"created_at": BASE + timedelta(seconds=1)}),
    ],
)
def test_snapshot_compare_rejects_other_concurrent_changes(db, model, changes):
    session_id, observed = seed(db)
    snapshot = cleanup._read(session_id)
    mutate(db, session_id, model, **changes)
    before = records(db)
    with pytest.raises(ValueError, match="probe_snapshot_changed"):
        cleanup._retire(snapshot, observed, time.monotonic())
    assert records(db) == before


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": "s-other"},
        {"lineage_id": "s-other"},
        {"workload": "codex-runtime"},
        {"state": "running"},
        {"state": "evicted"},
        {"terminal_reason": "completed"},
        {"generation": 1},
        {"generation": False},
        {"turn_seq": 2},
        {"turn_seq": "1"},
        {"stop_intent": "destroy"},
        {"interrupted_turn": {}},
        {"invoke_started_at": None},
        {"last_invoke_at": 0},
        {"updated_at": 0},
        {"updated_at": 2**63 - 1},
        {"inflight_dispatch_id": "different-dispatch"},
    ],
)
def test_positive_guest_evidence_must_match_original_terminal_dispatch(db, change):
    session_id, observed = seed(db)
    before = records(db)
    with pytest.raises(ValueError):
        cleanup._retire(
            cleanup._read(session_id), {**observed, **change}, time.monotonic()
        )
    assert records(db) == before


@pytest.mark.parametrize("observed", [None, [], "404", {}, {"error": {"status": 404}}])
def test_missing_or_malformed_guest_is_never_cessation(db, observed):
    session_id, _ = seed(db)
    before = records(db)
    with pytest.raises((TypeError, ValueError)):
        cleanup._retire(cleanup._read(session_id), observed, time.monotonic())
    assert records(db) == before


@pytest.mark.parametrize("age", [-60, cleanup.MAX_EVIDENCE_AGE_SECONDS + 1])
def test_stale_or_future_observation_clock_fails_closed(db, age):
    session_id, observed = seed(db)
    before = records(db)
    with pytest.raises(ValueError, match="probe_guest_evidence_stale"):
        cleanup._retire(cleanup._read(session_id), observed, time.monotonic() - age)
    assert records(db) == before


def test_audit_failure_rolls_back_binding_and_fence(db, monkeypatch):
    session_id, observed = seed(db)
    snapshot = cleanup._read(session_id)
    before = records(db)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(cleanup.controls, "_audit", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            cleanup._retire(snapshot, observed, time.monotonic())
    assert records(db) == before
    assert cleanup._retire(snapshot, observed, time.monotonic())


def test_concurrent_cleanup_has_one_winner_and_one_audit(db):
    session_id, observed = seed(db)
    snapshot = cleanup._read(session_id)
    barrier = Barrier(2)

    def attempt():
        barrier.wait(timeout=5)
        try:
            return cleanup._retire(snapshot, observed, time.monotonic())
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(attempt) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == [False, True]
    assert (
        len(
            [
                row
                for row in records(db)["FactoryAudit"]
                if row["action"] == cleanup.ACTION
            ]
        )
        == 1
    )


def test_postgres_lock_timeout_rolls_back_without_partial_cleanup(db):
    if db.dialect.name != "postgresql":
        pytest.skip("Row-lock timeout is exercised by the PostgreSQL target")
    session_id, observed = seed(db)
    snapshot = cleanup._read(session_id)
    before = records(db)
    with Session(db) as holder, ThreadPoolExecutor(max_workers=1) as workers:
        store._lock_session(holder, session_id)
        future = workers.submit(cleanup._retire, snapshot, observed, time.monotonic())
        with pytest.raises(OperationalError):
            future.result(timeout=5)
        holder.rollback()
    assert records(db) == before
    assert cleanup._retire(snapshot, observed, time.monotonic())


def test_sweep_reads_only_exact_guest_and_never_deletes_or_invokes(db, monkeypatch):
    _, observed = seed(db)
    transport = AsyncMock()
    transport.get_session.return_value = observed
    monkeypatch.setattr(execution_api, "_transport", transport)
    assert asyncio.run(cleanup.sweep_once()) == 0
    assert asyncio.run(cleanup.sweep_once()) == 0
    transport.get_session.assert_awaited_once_with(observed["session_id"])
    assert [call[0] for call in transport.mock_calls] == ["get_session"]
    assert records(db)["AgentSession"][0]["result_receipt_fence_id"] is None


def test_sweep_404_preserves_fence_without_logging_sensitive_response(
    db, monkeypatch, caplog
):
    _, observed = seed(db)
    before = records(db)
    transport = AsyncMock()
    request = httpx.Request("GET", "https://cp.test/sessions/exact")
    transport.get_session.side_effect = httpx.HTTPStatusError(
        "private-response-not-for-logs",
        request=request,
        response=httpx.Response(404, request=request),
    )
    monkeypatch.setattr(execution_api, "_transport", transport)
    with caplog.at_level("INFO", logger=cleanup.__name__):
        assert asyncio.run(cleanup.sweep_once()) == 0
    assert records(db) == before
    transport.get_session.assert_awaited_once_with(observed["session_id"])
    assert [call[0] for call in transport.mock_calls] == ["get_session"]
    assert "HTTPStatusError" in caplog.text
    assert "private-response-not-for-logs" not in caplog.text


def test_bounded_sweep_advances_past_held_candidates(db, monkeypatch):
    good_id, good = seed(db)
    held = [seed(db, number)[0] for number in range(2, cleanup.BATCH_SIZE + 2)]
    for session_id in held:
        mutate(db, session_id, AgentCapacityReservation, state="uncertain")
    before = records(db)
    transport = AsyncMock()
    transport.get_session.return_value = good
    monkeypatch.setattr(execution_api, "_transport", transport)
    after_id = asyncio.run(cleanup.sweep_once())
    assert after_id == min(held)
    assert records(db) == before
    transport.get_session.assert_not_awaited()
    assert asyncio.run(cleanup.sweep_once(after_id)) == 0
    transport.get_session.assert_awaited_once_with(good["session_id"])
    agents = records(db)["AgentSession"]
    assert (
        next(row for row in agents if row["id"] == good_id)["result_receipt_fence_id"]
        is None
    )
    assert all(row["result_receipt_fence_id"] for row in agents if row["id"] in held)
