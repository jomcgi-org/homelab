"""File-backed integration tests for explicit routine cessation reconciliation."""

from concurrent.futures import ThreadPoolExecutor
import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from agent import routine_jobs
from agent import routine_reconciliation as reconciliation
from agent.routine_reconciliation_models import RoutineReconciliation
from factory.execution import admission, store
from factory.execution.constants import UNKNOWN_INVOCATION
from factory.execution.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentSession,
    AgentTurn,
    PendingMessage,
    ProbeObservation,
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reconcile.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={
            "schema_translate_map": {"agent_sessions": None, "claude_agent": None}
        },
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                AgentCapacityPool,
                AgentCapacityReservation,
                AgentSession,
                AgentTurn,
                PendingMessage,
                RoutineReconciliation,
            )
        ],
    )
    with Session(engine) as db:
        for sql in (
            "CREATE TABLE routine_jobs (name TEXT PRIMARY KEY,routine_kind TEXT,interval_secs INTEGER,next_run_at TEXT,last_run_at TEXT,last_status TEXT,last_summary TEXT,locked_by TEXT,locked_at TEXT,ttl_secs INTEGER,payload TEXT,created_by TEXT,created_at TEXT)",
            "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY,name TEXT,status TEXT,application_version TEXT)",
            "CREATE TABLE raw_inputs (id INTEGER PRIMARY KEY,raw_id TEXT,source TEXT,content_hash TEXT,extra TEXT)",
            "CREATE TABLE atom_raw_provenance (id INTEGER PRIMARY KEY,raw_fk INTEGER,gardener_version TEXT,derived_note_id TEXT)",
        ):
            db.execute(text(sql))
        db.commit()
    monkeypatch.setattr(reconciliation, "get_engine", lambda: engine)
    monkeypatch.setattr(routine_jobs, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def held(
    engine,
    *,
    scout=False,
    applied=False,
    delivery_error=False,
    completion_recorded=True,
):
    name = "kg-repo-diff" if scout else "kg:raw"
    with Session(engine) as db:
        agent = AgentSession(
            local_session_id="cycle:kg-drain:" + name,
            workspace="guest",
            branch="main",
            workflow_id="cycle",
            node_key="kg-drain",
            admission_tier="kg",
            status="warn" if delivery_error else "failed",
            model="luna",
            ember_session_id="guest",
            ember_lineage_id="lineage",
            ember_session_token="private-token",
            cli_session_id="cli",
        )
        db.add(agent)
        db.flush()
        sid = agent.id
        db.add(
            AgentTurn(
                session_id=sid,
                seq=1,
                prompt="original",
                result_text="partial evidence",
                terminal_reason="error",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                stop_reason=None if delivery_error else UNKNOWN_INVOCATION,
                usage_json='{"recovery":{"cause":"executor_cancelled"}}',
            )
        )
        payload = (
            {"mode": "repo-diff", "last_sha": "old-cursor"}
            if scout
            else {"raw_id": "raw", "attempt": 2}
        )
        db.execute(
            text(
                "INSERT INTO routine_jobs VALUES (:name,'kg-drain',:interval,NULL,'old-time',:status,:summary,NULL,NULL,2100,:payload,'original-actor','old-created')"
            ),
            {
                "name": name,
                "interval": 3600 if scout else None,
                "status": UNKNOWN_INVOCATION,
                "summary": f"session_id={sid}: unknown original",
                "payload": json.dumps(payload),
            },
        )
        if delivery_error:
            db.add(
                AgentCapacityReservation(
                    local_session_id=agent.local_session_id,
                    pending_seq=1,
                    session_id=sid,
                    tier="kg",
                    routine_job_name=name,
                    state="uncertain",
                    outcome="delivery_error",
                )
            )
        db.execute(
            text(
                "INSERT INTO workflow_status VALUES ('cycle','drain_cycle','SUCCESS','version')"
            )
        )
        if not scout:
            db.execute(
                text(
                    "INSERT INTO raw_inputs VALUES (1,'raw','codex-session','content',:extra)"
                ),
                {
                    "extra": json.dumps(
                        {
                            "extraction_passes": 1 if applied else 0,
                            "extraction_rejected": [{"reason": "original"}]
                            if applied
                            else [],
                        }
                    )
                },
            )
            if applied:
                db.execute(
                    text(
                        "INSERT INTO atom_raw_provenance VALUES (1,1,:version,'already-applied')"
                    ),
                    {"version": reconciliation.EXTRACTION_VERSION},
                )
        db.commit()
    with Session(engine) as db:
        state = reconciliation.read_reconciliation_state(db, name, sid)
    now = datetime.now(timezone.utc)
    proof = {
        "session_id": "guest",
        "state": "parked",
        "generation": 0,
        "observed_at": now.isoformat(),
        "updated_at": int((now - timedelta(seconds=20)).timestamp() * 1000),
        "last_invoke_at": (
            int((now - timedelta(seconds=30)).timestamp() * 1000)
            if completion_recorded
            else None
        ),
        "evidence_sha256": "a" * 64,
    }
    return {
        "reconciliation_key": "operator-1",
        "actor": "human:joe",
        "job_name": name,
        "session_id": sid,
        "expected_state_sha256": state["state_sha256"],
        "cessation": proof,
        "disposition": "retain_applied" if applied else "rearm",
    }, state


def held_qwen(engine):
    """Create the project-tier row shape written by the real hold operation."""
    name = "docfix:0123456789abcdef"
    with Session(engine) as db:
        agent = AgentSession(
            local_session_id="cycle:qwen-drain:" + name,
            workspace="guest",
            branch="main",
            workflow_id="cycle",
            node_key="qwen-drain",
            admission_tier="project",
            status="failed",
            model="luna",
            ember_session_id="qwen-guest",
            ember_lineage_id="qwen-lineage",
            ember_session_token="private-token",
            cli_session_id="qwen-cli",
        )
        db.add(agent)
        db.flush()
        sid = agent.id
        db.add(
            AgentTurn(
                session_id=sid,
                seq=1,
                prompt="bounded docfix",
                result_text="partial result",
                terminal_reason="error",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                stop_reason=UNKNOWN_INVOCATION,
            )
        )
        db.add(
            AgentCapacityReservation(
                local_session_id=agent.local_session_id,
                pending_seq=1,
                session_id=sid,
                tier="project",
                routine_job_name=name,
                state="uncertain",
                outcome=UNKNOWN_INVOCATION,
                owner="drainer",
            )
        )
        db.execute(
            text(
                "INSERT INTO routine_jobs VALUES "
                "(:name,'qwen-drain',NULL,CURRENT_TIMESTAMP,NULL,NULL,NULL,"
                "'drainer','2000-01-01',2100,:payload,'knowledge.extraction',"
                "'old-created')"
            ),
            {
                "name": name,
                "payload": json.dumps(
                    {
                        "prompt": "bounded docfix",
                        "repo": "jomcgi-org/homelab",
                        "branch": "main",
                    }
                ),
            },
        )
        db.execute(
            text(
                "INSERT INTO workflow_status VALUES "
                "('cycle','drain_cycle','SUCCESS','version')"
            )
        )
        db.commit()

    assert routine_jobs.hold_job_for_unknown_outcome(
        name, sid, "reconcile before retry"
    )
    with Session(engine) as db:
        state = reconciliation.read_reconciliation_state(db, name, sid)
        row = db.execute(
            text(
                "SELECT last_status,next_run_at,locked_by,locked_at,last_summary "
                "FROM routine_jobs WHERE name=:name"
            ),
            {"name": name},
        ).one()
        assert row.last_status == UNKNOWN_INVOCATION
        assert row.next_run_at is None
        assert row.locked_by is None and row.locked_at is None
        assert row.last_summary.startswith(f"session_id={sid}:")
    now = datetime.now(timezone.utc)
    return {
        "reconciliation_key": "operator-qwen-1",
        "actor": "human:operator",
        "job_name": name,
        "session_id": sid,
        "expected_state_sha256": state["state_sha256"],
        "cessation": {
            "session_id": "qwen-guest",
            "state": "parked",
            "generation": 3,
            "observed_at": now.isoformat(),
            "updated_at": int((now - timedelta(seconds=10)).timestamp() * 1000),
            "last_invoke_at": int((now - timedelta(seconds=20)).timestamp() * 1000),
            "evidence_sha256": "b" * 64,
        },
        "disposition": "rearm",
    }, state


def test_qwen_docfix_rearm_settles_exact_permit_and_is_replay_safe(database):
    request, before = held_qwen(database)

    result = reconciliation.reconcile_held_job(**request)
    assert result["disposition"] == "rearm"
    assert result["original_outcome"] == UNKNOWN_INVOCATION
    assert result["next_run_at"]
    assert reconciliation.reconcile_held_job(**request) == result

    with Session(database) as db:
        after = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
        assert after["job_status"] == "reconciled_unknown"
        assert after["next_run_at"] is not None
        assert after["payload_sha256"] == before["payload_sha256"]
        assert after["turns_sha256"] == before["turns_sha256"]
        agent = db.get(AgentSession, request["session_id"])
        assert agent.status == "failed" and agent.ember_session_id is None
        permit = db.get(AgentCapacityReservation, before["reservation_id"])
        assert permit.state == "settled"
        assert permit.outcome == "guest_cessation_confirmed"
        assert len(db.exec(select(RoutineReconciliation)).all()) == 1


@pytest.mark.parametrize(
    "invalid",
    ["missing", "stale", "guest", "expected_state", "permit", "kind"],
)
def test_qwen_docfix_refuses_incomplete_or_mismatched_evidence(database, invalid):
    request, before = held_qwen(database)
    if invalid == "missing":
        del request["cessation"]["generation"]
    elif invalid == "stale":
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        request["cessation"]["observed_at"] = old.isoformat()
        request["cessation"]["updated_at"] = int(old.timestamp() * 1000)
        request["cessation"]["last_invoke_at"] = int(
            (old - timedelta(seconds=1)).timestamp() * 1000
        )
    elif invalid == "guest":
        request["cessation"]["session_id"] = "different-guest"
    elif invalid == "expected_state":
        request["expected_state_sha256"] = "c" * 64
    elif invalid == "permit":
        with Session(database) as db:
            permit = db.get(AgentCapacityReservation, before["reservation_id"])
            permit.routine_job_name = "docfix:different"
            db.add(permit)
            db.commit()
            changed = reconciliation.read_reconciliation_state(
                db, request["job_name"], request["session_id"]
            )
            request["expected_state_sha256"] = changed["state_sha256"]
    else:
        with Session(database) as db:
            db.execute(text("UPDATE routine_jobs SET routine_kind='unsupported'"))
            db.commit()

    with pytest.raises(ValueError):
        reconciliation.reconcile_held_job(**request)

    with Session(database) as db:
        row = db.execute(text("SELECT last_status,next_run_at FROM routine_jobs")).one()
        assert row.last_status == UNKNOWN_INVOCATION
        assert row.next_run_at is None
        permit = db.get(AgentCapacityReservation, before["reservation_id"])
        assert permit.state == "uncertain"
        assert db.exec(select(RoutineReconciliation)).first() is None
        assert db.get(AgentSession, request["session_id"]).ember_session_id == (
            "qwen-guest"
        )


def test_qwen_docfix_retain_applied_settles_without_rearming(database):
    request, before = held_qwen(database)
    request["disposition"] = "retain_applied"

    result = reconciliation.reconcile_held_job(**request)

    assert result["disposition"] == "retain_applied"
    assert result["next_run_at"] is None
    with Session(database) as db:
        permit = db.get(AgentCapacityReservation, before["reservation_id"])
        assert permit.state == "settled"
        assert permit.outcome == "guest_cessation_confirmed"
        assert db.execute(text("SELECT next_run_at FROM routine_jobs")).scalar() is None


@pytest.mark.parametrize("completion_recorded", [True, False])
def test_rearm_preserves_unknown_history_payload_and_lineage(
    database, completion_recorded
):
    request, before = held(
        database, scout=True, completion_recorded=completion_recorded
    )
    result = reconciliation.reconcile_held_job(**request)
    assert result["original_outcome"] == UNKNOWN_INVOCATION and result["next_run_at"]
    with Session(database) as db:
        after = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
        assert after["turns_sha256"] == before["turns_sha256"]
        assert (
            after["payload_sha256"] == before["payload_sha256"]
            and after["cursor_sha"] == "old-cursor"
        )
        agent = db.get(AgentSession, request["session_id"])
        assert (
            agent.status == "failed"
            and agent.ember_session_id is None
            and agent.ember_session_token is None
        )
        assert (
            agent.prior_ember_lineage_id == "lineage"
            and agent.prior_cli_session_id == "cli"
        )
        with pytest.raises(store.SessionOutcomeUnknown):
            store.create_pending_message(
                db, agent.id, "must not wake old guest", "luna"
            )
        audit = db.exec(select(RoutineReconciliation)).one()
        assert json.loads(audit.evidence_json)["before"] == before
        assert audit.actor == "human:joe"
        permits = db.exec(select(AgentCapacityReservation)).all()
        assert len(permits) == 1 and permits[0].state == "settled"
        admission.adopt_existing(db)
        assert admission.reservation(db, agent.local_session_id).state == "settled"
        assert admission.reserve_start(
            db,
            "new-cycle:kg-drain:kg-repo-diff",
            tier="kg",
            model="luna",
            routine_job_name="kg-repo-diff",
        )


def test_null_completion_settles_only_exact_permit_and_preserves_accounting(database):
    request, _ = held(database, completion_recorded=False)
    request["cessation"]["state"] = "evicted"
    request["cessation"]["evidence_sha256"] = reconciliation._sha(
        {
            "cp": {"session_id": "guest", "last_invoke_at": None},
            "positive_cessation": {
                "container_id": "original-container",
                "vm_id": "original-vm",
                "container_stop_completed": True,
            },
        }
    )
    with Session(database) as db:
        agent = db.get(AgentSession, request["session_id"])
        turn = store.get_turn(db, agent.id, 1)
        turn.usage_json = json.dumps(
            {"recovery": {"claim_owner": "original-owner", "dispatch_count": 1}}
        )
        db.add(turn)
        original = AgentCapacityReservation(
            local_session_id=agent.local_session_id,
            session_id=agent.id,
            pending_seq=1,
            tier="kg",
            routine_job_name=request["job_name"],
            state="uncertain",
            outcome="executor_cancelled",
            owner="original-owner",
        )
        unrelated = AgentCapacityReservation(
            local_session_id="independent-cycle:kg-drain:kg:other",
            pending_seq=1,
            tier="kg",
            routine_job_name="kg:other",
            state="reserved",
            owner="independent-owner",
        )
        db.add(original)
        db.add(unrelated)
        db.commit()
        db.refresh(turn)
        original_id, unrelated_id = original.id, unrelated.id
        original_before = original.model_dump()
        unrelated_before = unrelated.model_dump()
        turn_before = turn.model_dump()
        agent_created_at = agent.created_at
        before = reconciliation.read_reconciliation_state(
            db, request["job_name"], agent.id
        )
        request["expected_state_sha256"] = before["state_sha256"]

    result = reconciliation.reconcile_held_job(**request)

    assert result["disposition"] == "rearm"
    with Session(database) as db:
        original = db.get(AgentCapacityReservation, original_id)
        assert original.state == "settled"
        assert original.outcome == "guest_cessation_confirmed"
        assert original.settled_at is not None
        for field, value in original_before.items():
            if field not in {"state", "outcome", "settled_at"}:
                assert getattr(original, field) == value
        assert (
            db.get(AgentCapacityReservation, unrelated_id).model_dump()
            == unrelated_before
        )
        assert len(db.exec(select(AgentCapacityReservation)).all()) == 2
        assert len(db.exec(select(AgentSession)).all()) == 1
        turn = store.get_turn(db, request["session_id"], 1)
        assert turn.model_dump() == turn_before
        assert turn.cost_usd is None and turn.stop_reason == UNKNOWN_INVOCATION
        agent = db.get(AgentSession, request["session_id"])
        assert agent.status == "failed" and agent.created_at == agent_created_at
        assert agent.prior_ember_lineage_id == "lineage"
        assert agent.prior_cli_session_id == "cli"
        assert agent.ember_session_id is None
        after = reconciliation.read_reconciliation_state(
            db, request["job_name"], agent.id
        )
        for field in (
            "turns_sha256",
            "payload_sha256",
            "raw_sha256",
            "provenance_sha256",
        ):
            assert after[field] == before[field]
        audit = db.exec(select(RoutineReconciliation)).one()
        recorded = json.loads(audit.evidence_json)["request"]["cessation"]
        assert recorded == request["cessation"]
        assert recorded["last_invoke_at"] is None
        assert (
            db.execute(text("SELECT status FROM workflow_status")).scalar_one()
            == "SUCCESS"
        )


@pytest.mark.parametrize(
    "value", [True, False, "0", -1, 1.5, "after_updated", "after_observed", "missing"]
)
def test_malformed_completion_timestamp_preserves_held_state(database, value):
    request, before = held(database)
    proof = request["cessation"]
    if value == "missing":
        del proof["last_invoke_at"]
    elif value == "after_updated":
        proof["last_invoke_at"] = proof["updated_at"] + 1
    elif value == "after_observed":
        proof["last_invoke_at"] = (
            int(datetime.fromisoformat(proof["observed_at"]).timestamp() * 1000) + 1
        )
    else:
        proof["last_invoke_at"] = value
    with pytest.raises(ValueError):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert (
            reconciliation.read_reconciliation_state(
                db, request["job_name"], request["session_id"]
            )
            == before
        )
        assert not db.exec(select(RoutineReconciliation)).all()
        assert not db.exec(select(AgentCapacityReservation)).all()


@pytest.mark.parametrize("value", [None, True, False, "0", -1, 1.5, "future"])
def test_null_completion_still_requires_valid_update_timestamp(database, value):
    request, before = held(database, completion_recorded=False)
    proof = request["cessation"]
    proof["updated_at"] = (
        int(datetime.fromisoformat(proof["observed_at"]).timestamp() * 1000) + 1
        if value == "future"
        else value
    )
    with pytest.raises(ValueError, match="timestamps conflict"):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert (
            reconciliation.read_reconciliation_state(
                db, request["job_name"], request["session_id"]
            )
            == before
        )
        assert not db.exec(select(RoutineReconciliation)).all()
        assert not db.exec(select(AgentCapacityReservation)).all()


def test_delivery_error_hold_reconciles_with_recorded_outcome(database):
    request, before = held(database, delivery_error=True)

    result = reconciliation.reconcile_held_job(**request)

    assert result["original_outcome"] == "delivery_error"
    with Session(database) as db:
        after = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
        permit = db.exec(select(AgentCapacityReservation)).one()
        assert permit.state == "settled"
        assert after["turns_sha256"] == before["turns_sha256"]
        assert after["latest_stop_reason"] is None


def test_production_delivery_error_writer_creates_a_reconcilable_hold(
    database, monkeypatch
):
    request, _ = held(database)
    sid = request["session_id"]
    with Session(database) as db:
        db.delete(store.get_turn(db, sid, 1))
        agent = db.get(AgentSession, sid)
        agent.status = "running"
        db.add(agent)
        db.add(
            PendingMessage(
                session_id=sid,
                seq=1,
                message_text="original",
                model="luna",
                partial_text="retained delivery progress",
                claimed_by_replica="worker",
                dispatch_count=1,
            )
        )
        db.add(
            AgentCapacityReservation(
                local_session_id=agent.local_session_id,
                session_id=sid,
                pending_seq=1,
                routine_job_name=request["job_name"],
                tier="kg",
                state="running",
            )
        )
        db.commit()

    monkeypatch.setattr(store, "get_engine", lambda: database)
    store.mark_turn_error_sync(
        sid, 1, "invoke_timeout", claim_owner="worker", cessation_confirmed=False
    )

    with Session(database) as db:
        before = reconciliation.read_reconciliation_state(db, request["job_name"], sid)
        assert before["session_status"] == "warn"
        assert before["latest_terminal_reason"] == "error"
        assert before["latest_stop_reason"] is None
        assert before["reservation_state"] == "uncertain"
        assert before["reservation_outcome"] == "delivery_error"
        assert before["pending"] is False
        turn = store.get_turn(db, sid, 1)
        assert turn.result_text == "retained delivery progress"
        assert turn.cost_usd is None
    request["expected_state_sha256"] = before["state_sha256"]
    now = datetime.now(timezone.utc)
    request["cessation"].update(
        observed_at=now.isoformat(),
        updated_at=int(now.timestamp() * 1000),
        last_invoke_at=int(now.timestamp() * 1000),
    )

    assert (
        reconciliation.reconcile_held_job(**request)["original_outcome"]
        == "delivery_error"
    )

    with Session(database) as db:
        after = reconciliation.read_reconciliation_state(db, request["job_name"], sid)
        assert after["turns_sha256"] == before["turns_sha256"]
        assert after["payload_sha256"] == before["payload_sha256"]
        assert after["raw_sha256"] == before["raw_sha256"]
        assert after["provenance_sha256"] == before["provenance_sha256"]
        assert db.exec(select(AgentCapacityReservation)).one().state == "settled"


@pytest.mark.parametrize("delivery_error", [False, True])
def test_existing_extraction_retained_without_false_correction_success(
    database, delivery_error
):
    request, before = held(database, applied=True, delivery_error=delivery_error)
    result = reconciliation.reconcile_held_job(**request)
    assert result["next_run_at"] is None
    assert result["original_outcome"] == (
        "delivery_error" if delivery_error else UNKNOWN_INVOCATION
    )
    with Session(database) as db:
        after = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
        assert after["job_status"] == "reconciled_unknown"
        assert (
            after["raw_sha256"] == before["raw_sha256"]
            and after["provenance_sha256"] == before["provenance_sha256"]
        )
        assert after["latest_stop_reason"] == (
            None if delivery_error else UNKNOWN_INVOCATION
        )
        assert after["latest_terminal_reason"] == "error"
        permit = db.exec(select(AgentCapacityReservation)).one()
        assert permit.state == "settled"


@pytest.mark.parametrize("delivery_error", [False, True])
@pytest.mark.parametrize("guest_state", ["destroyed", "failed"])
def test_supervision_recovers_held_kg_correction_atomically(
    database, monkeypatch, delivery_error, guest_state
):
    """Recover a failed correction without reapplying its first extraction."""
    from factory.execution import permit_supervision as supervision
    from factory.execution.models import ProbeObservation
    from factory.execution.review_leases import ReservationReview
    from factory.orchestration.factory_models import FactoryStart
    from factory.orchestration.models import SwarmNodeRun, SwarmTask

    engine = database.execution_options(
        schema_translate_map={
            "agent_sessions": None,
            "claude_agent": None,
            "swarm": None,
        }
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                ProbeObservation,
                ReservationReview,
                FactoryStart,
                SwarmNodeRun,
                SwarmTask,
            )
        ],
    )
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    monkeypatch.setenv("FACTORY_RESERVATION_REVIEW_ENABLED", "true")
    monkeypatch.setenv("FACTORY_INVOKE_FAILURE_CESSATION_ENABLED", "true")
    monkeypatch.setattr(supervision, "get_engine", lambda: engine)
    request, _ = held(engine, applied=True, delivery_error=True)
    sid = request["session_id"]
    at = datetime.now(timezone.utc) - timedelta(seconds=10)
    with Session(engine) as db, db.begin():
        agent = db.get(AgentSession, sid)
        agent.status = "warn" if delivery_error else "failed"
        agent.last_turn_at = at
        first = db.exec(select(AgentTurn).where(AgentTurn.session_id == sid)).one()
        first.terminal_reason = "completed"
        first.stop_reason = None
        first.usage_json = "{}"
        prior = db.exec(select(AgentCapacityReservation)).one()
        prior.state = "settled"
        prior.outcome = "completed"
        prior.settled_at = at - timedelta(seconds=20)
        permit = AgentCapacityReservation(
            local_session_id=agent.local_session_id,
            session_id=sid,
            pending_seq=2,
            tier="kg",
            state="uncertain",
            outcome="delivery_error"
            if delivery_error
            else "response_lost_guest_ceased",
            owner="worker",
            routine_job_name=request["job_name"],
        )
        correction = AgentTurn(
            session_id=sid,
            seq=2,
            prompt="correction",
            result_text="lost",
            terminal_reason="error",
            stop_reason=None if delivery_error else UNKNOWN_INVOCATION,
            cost_usd=None,
            created_at=at,
            usage_json=json.dumps(
                {
                    "recovery": {
                        "claim_owner": "worker",
                        "dispatch_count": 1,
                        "last_dispatch_at": (at - timedelta(seconds=20)).isoformat(),
                    }
                }
            ),
        )
        db.add_all([agent, first, prior, permit, correction])
        db.flush()
        permit_id = permit.id
        prior_id = prior.id
    with Session(engine) as db:
        prior_snapshot = db.get(AgentCapacityReservation, prior_id).model_dump()
        before = reconciliation.read_reconciliation_state(db, request["job_name"], sid)

    async def get_session(guest):
        assert guest == "guest"
        return {
            "session_id": guest,
            "state": guest_state,
            "terminal_reason": guest_state,
            "interrupted_turn": None,
            "generation": 0,
            "invoke_started_at": int((at - timedelta(seconds=15)).timestamp() * 1000),
            "last_invoke_at": int(
                (
                    at - timedelta(seconds=25 if guest_state == "failed" else 5)
                ).timestamp()
                * 1000
            ),
            "updated_at": int(
                (
                    at + timedelta(seconds=-1 if guest_state == "failed" else 5)
                ).timestamp()
                * 1000
            ),
        }

    from types import SimpleNamespace

    transport = SimpleNamespace(get_session=get_session)
    asyncio.run(supervision.sweep_once(transport))
    asyncio.run(supervision.sweep_once(transport))
    with Session(engine) as db:
        after = reconciliation.read_reconciliation_state(db, request["job_name"], sid)
        assert db.get(AgentCapacityReservation, permit_id).state == "settled"
        assert db.get(ProbeObservation, permit_id).reason == "guest_cessation_confirmed"
        prior = db.exec(
            select(AgentCapacityReservation).where(
                AgentCapacityReservation.session_id == sid,
                AgentCapacityReservation.pending_seq == 1,
            )
        ).one()
        assert prior.model_dump() == prior_snapshot
        assert after["job_status"] == "reconciled_unknown"
        assert after["next_run_at"] is None
        assert after["guest_id"] is None
        assert after["latest_terminal_reason"] == "error"
        assert after["latest_stop_reason"] == (
            None if delivery_error else UNKNOWN_INVOCATION
        )
        for key in (
            "turns_sha256",
            "payload_sha256",
            "raw_sha256",
            "provenance_sha256",
        ):
            assert after[key] == before[key]
        assert db.exec(select(RoutineReconciliation)).one().unknown_turn_seq == 2


@pytest.mark.parametrize("completion_recorded", [True, False])
@pytest.mark.parametrize("delivery_error", [False, True])
def test_same_request_replay_survives_job_deletion_and_stale_observation(
    database, monkeypatch, delivery_error, completion_recorded
):
    request, _ = held(
        database, delivery_error=delivery_error, completion_recorded=completion_recorded
    )
    result = reconciliation.reconcile_held_job(**request)
    assert result["original_outcome"] == (
        "delivery_error" if delivery_error else UNKNOWN_INVOCATION
    )
    with Session(database) as db:
        db.execute(text("DELETE FROM routine_jobs"))
        db.commit()
    monkeypatch.setattr(reconciliation, "MAX_EVIDENCE_AGE_SECONDS", -1)
    assert reconciliation.reconcile_held_job(**request) == result
    with pytest.raises(ValueError, match="conflicts"):
        reconciliation.reconcile_held_job(**{**request, "actor": "different"})
    with Session(database) as db:
        assert len(db.exec(select(RoutineReconciliation)).all()) == 1
        permits = db.exec(select(AgentCapacityReservation)).all()
        assert len(permits) == 1 and permits[0].state == "settled"


@pytest.mark.parametrize(
    "change",
    [
        "pending",
        "workflow",
        "binding",
        "new_turn",
        "job_payload",
        "raw",
        "provenance",
        "lease",
    ],
)
@pytest.mark.parametrize("delivery_error", [False, True])
@pytest.mark.parametrize("completion_recorded", [True, False])
def test_changed_expected_state_refuses_without_audit_or_releasing_permits(
    database, change, delivery_error, completion_recorded
):
    request, _ = held(
        database, delivery_error=delivery_error, completion_recorded=completion_recorded
    )
    with Session(database) as db:
        agent = db.get(AgentSession, request["session_id"])
        if change == "pending":
            db.add(
                PendingMessage(
                    session_id=agent.id, seq=2, message_text="queued", model="luna"
                )
            )
        elif change == "workflow":
            db.execute(text("UPDATE workflow_status SET status='PENDING'"))
        elif change == "binding":
            agent.ember_session_id = "replacement"
            db.add(agent)
        elif change == "new_turn":
            db.add(
                AgentTurn(
                    session_id=agent.id,
                    seq=2,
                    prompt="later",
                    result_text="late",
                    terminal_reason="completed",
                )
            )
        elif change == "job_payload":
            db.execute(text("UPDATE routine_jobs SET payload='{}'"))
        elif change == "raw":
            db.execute(text("UPDATE raw_inputs SET content_hash='changed'"))
        elif change == "provenance":
            db.execute(
                text(
                    "INSERT INTO atom_raw_provenance VALUES (1,1,:version,'late-atom')"
                ),
                {"version": reconciliation.EXTRACTION_VERSION},
            )
        elif change == "lease":
            db.execute(
                text("UPDATE routine_jobs SET locked_by='another',locked_at='now'")
            )
        db.commit()
    with pytest.raises(ValueError):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert not db.exec(select(RoutineReconciliation)).all()
        permits = db.exec(select(AgentCapacityReservation)).all()
        if delivery_error:
            assert len(permits) == 1
            assert permits[0].state == "uncertain"
            assert permits[0].outcome == "delivery_error"
        else:
            assert permits == []
        assert (
            db.execute(text("SELECT last_status FROM routine_jobs")).scalar_one()
            == UNKNOWN_INVOCATION
        )


@pytest.mark.parametrize(
    "proof_change",
    [
        {"state": "running"},
        {"session_id": "wrong"},
        {"generation": -1},
        {"observed_at": "2000-01-01T00:00:00+00:00"},
        {"evidence_sha256": "unverified"},
    ],
)
@pytest.mark.parametrize("delivery_error", [False, True])
@pytest.mark.parametrize("completion_recorded", [True, False])
def test_missing_or_unsafe_cessation_refuses(
    database, proof_change, delivery_error, completion_recorded
):
    request, _ = held(
        database, delivery_error=delivery_error, completion_recorded=completion_recorded
    )
    request["cessation"] |= proof_change
    with pytest.raises(ValueError):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert not db.exec(select(RoutineReconciliation)).all()
        permits = db.exec(select(AgentCapacityReservation)).all()
        if delivery_error:
            assert len(permits) == 1
            assert permits[0].state == "uncertain"
            assert permits[0].outcome == "delivery_error"
        else:
            assert permits == []


@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize("delivery_error", [False, True])
def test_disposition_must_match_authoritative_provenance(
    database, applied, delivery_error
):
    request, _ = held(database, applied=applied, delivery_error=delivery_error)
    request["disposition"] = "rearm" if applied else "retain_applied"
    with pytest.raises(ValueError):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert not db.exec(select(RoutineReconciliation)).all()
        permits = db.exec(select(AgentCapacityReservation)).all()
        if delivery_error:
            assert len(permits) == 1
            assert permits[0].state == "uncertain"
            assert permits[0].outcome == "delivery_error"
        else:
            assert permits == []


@pytest.mark.parametrize("completion_recorded", [True, False])
def test_new_reserved_owner_blocks_and_rolls_back_adoption(
    database, completion_recorded
):
    request, _ = held(database, completion_recorded=completion_recorded)
    with Session(database) as db:
        db.add(
            AgentCapacityReservation(
                local_session_id="new-cycle",
                pending_seq=1,
                tier="kg",
                routine_job_name="kg:raw",
            )
        )
        db.commit()
    with pytest.raises(ValueError, match="owns this job"):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert len(db.exec(select(AgentCapacityReservation)).all()) == 1
        assert db.get(AgentSession, request["session_id"]).ember_session_id == "guest"


@pytest.mark.parametrize("delivery_error", [False, True])
@pytest.mark.parametrize("completion_recorded", [True, False])
def test_caller_transaction_rollback_reverts_audit_binding_job_and_permit(
    database, delivery_error, completion_recorded
):
    request, before = held(
        database, delivery_error=delivery_error, completion_recorded=completion_recorded
    )
    with Session(database) as db:
        reconciliation.reconcile_held_job(**request, session=db)
        db.rollback()
    with Session(database) as db:
        assert (
            reconciliation.read_reconciliation_state(
                db, request["job_name"], request["session_id"]
            )
            == before
        )
        assert not db.exec(select(RoutineReconciliation)).all()
        permits = db.exec(select(AgentCapacityReservation)).all()
        if delivery_error:
            assert len(permits) == 1
            assert permits[0].state == "uncertain"
            assert permits[0].outcome == "delivery_error"
        else:
            assert permits == []


@pytest.mark.parametrize("delivery_error", [False, True])
@pytest.mark.parametrize("completion_recorded", [True, False])
def test_concurrent_identical_operator_requests_record_once(
    database, delivery_error, completion_recorded
):
    request, _ = held(
        database, delivery_error=delivery_error, completion_recorded=completion_recorded
    )
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda _: reconciliation.reconcile_held_job(**request), range(2))
        )
    assert results[0] == results[1]
    assert results[0]["original_outcome"] == (
        "delivery_error" if delivery_error else UNKNOWN_INVOCATION
    )
    with Session(database) as db:
        assert len(db.exec(select(RoutineReconciliation)).all()) == 1
        permits = db.exec(select(AgentCapacityReservation)).all()
        assert len(permits) == 1 and permits[0].state == "settled"


@pytest.mark.parametrize(
    "unsafe", ["pending", "workflow", "kind", "shared_guest", "newer_session"]
)
@pytest.mark.parametrize("delivery_error", [False, True])
@pytest.mark.parametrize("completion_recorded", [True, False])
def test_current_unsafe_state_cannot_be_authorized_by_refreshing_fingerprint(
    database, unsafe, delivery_error, completion_recorded
):
    request, _ = held(
        database, delivery_error=delivery_error, completion_recorded=completion_recorded
    )
    with Session(database) as db:
        if unsafe == "pending":
            db.add(
                PendingMessage(
                    session_id=request["session_id"],
                    seq=2,
                    message_text="later",
                    model="luna",
                )
            )
        elif unsafe == "workflow":
            db.execute(text("UPDATE workflow_status SET status='PENDING'"))
        elif unsafe == "kind":
            db.execute(text("UPDATE routine_jobs SET routine_kind='qwen-drain'"))
        else:
            db.add(
                AgentSession(
                    local_session_id="new-cycle:kg-drain:kg:raw",
                    workspace="guest",
                    branch="main",
                    workflow_id="new-cycle",
                    node_key="kg-drain",
                    status="failed",
                    ember_session_id="guest" if unsafe == "shared_guest" else None,
                )
            )
        db.commit()
        request["expected_state_sha256"] = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )["state_sha256"]
    with pytest.raises(ValueError, match="ownership"):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert not db.exec(select(RoutineReconciliation)).all()
        permits = db.exec(select(AgentCapacityReservation)).all()
        if delivery_error:
            assert len(permits) == 1
            assert permits[0].state == "uncertain"
            assert permits[0].outcome == "delivery_error"
        else:
            assert permits == []


@pytest.mark.parametrize("delivery_error", [False, True])
@pytest.mark.parametrize("completion_recorded", [True, False])
def test_failure_recording_audit_rolls_back_job_binding_and_settlement(
    database, monkeypatch, delivery_error, completion_recorded
):
    request, before = held(
        database, delivery_error=delivery_error, completion_recorded=completion_recorded
    )
    add = Session.add

    def fail_audit(self, instance, *args, **kwargs):
        if isinstance(instance, RoutineReconciliation):
            raise RuntimeError("audit unavailable")
        return add(self, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "add", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert (
            reconciliation.read_reconciliation_state(
                db, request["job_name"], request["session_id"]
            )
            == before
        )
        permits = db.exec(select(AgentCapacityReservation)).all()
        if delivery_error:
            assert len(permits) == 1
            assert permits[0].state == "uncertain"
            assert permits[0].outcome == "delivery_error"
        else:
            assert permits == []


@pytest.mark.parametrize("delivery_error", [False, True])
@pytest.mark.parametrize("completion_recorded", [True, False])
def test_old_park_stamp_cannot_prove_newer_unknown_attempt_ceased(
    database, delivery_error, completion_recorded
):
    request, _ = held(
        database, delivery_error=delivery_error, completion_recorded=completion_recorded
    )
    request["cessation"]["updated_at"] = 0
    request["cessation"]["last_invoke_at"] = 0 if completion_recorded else None
    with pytest.raises(ValueError, match="must follow"):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert not db.exec(select(RoutineReconciliation)).all()
        permits = db.exec(select(AgentCapacityReservation)).all()
        if delivery_error:
            assert len(permits) == 1
            assert permits[0].state == "uncertain"
            assert permits[0].outcome == "delivery_error"
        else:
            assert permits == []


def test_caller_identity_map_cannot_restore_stale_lineage(database):
    request, _ = held(database)
    with Session(database) as caller:
        cached = caller.get(AgentSession, request["session_id"])
        assert cached.ember_lineage_id == "lineage"
        with Session(database) as other:
            changed = other.get(AgentSession, request["session_id"])
            changed.ember_lineage_id = "current-lineage"
            other.add(changed)
            other.commit()
        request["expected_state_sha256"] = reconciliation.read_reconciliation_state(
            caller, request["job_name"], request["session_id"]
        )["state_sha256"]
        reconciliation.reconcile_held_job(**request, session=caller)
        caller.commit()
        assert (
            caller.get(AgentSession, request["session_id"]).prior_ember_lineage_id
            == "current-lineage"
        )


def _add_cleanup_claim(database, request, **changes):
    with Session(database) as db:
        agent = db.get(AgentSession, request["session_id"])
        fields = {
            "guest_cleanup_id": "b" * 32,
            "guest_cleanup_guest_id": agent.ember_session_id,
            "guest_cleanup_workflow_id": agent.workflow_id,
            "guest_cleanup_dispatch_json": "[]",
            "guest_cleanup_started_at": datetime.now(timezone.utc),
            **changes,
        }
        for field, value in fields.items():
            setattr(agent, field, value)
        db.add(agent)
        db.commit()
        state = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
    request["expected_state_sha256"] = state["state_sha256"]
    return state, fields


def test_positive_kg_reconciliation_retires_matching_cleanup_atomically(database):
    request, _ = held(database, scout=True, delivery_error=True)
    before, claim = _add_cleanup_claim(database, request)
    with Session(database) as db:
        reconciliation.reconcile_held_job(**request, session=db)
        agent = db.get(AgentSession, request["session_id"])
        assert all(getattr(agent, field) is None for field in claim)
        db.rollback()
    with Session(database) as db:
        agent = db.get(AgentSession, request["session_id"])
        assert agent.guest_cleanup_id == claim["guest_cleanup_id"]
        assert agent.ember_session_id == "guest"
        assert db.exec(select(AgentCapacityReservation)).one().state == "uncertain"
        assert db.exec(select(RoutineReconciliation)).first() is None
    result = reconciliation.reconcile_held_job(**request)
    assert reconciliation.reconcile_held_job(**request) == result
    with Session(database) as db:
        agent = db.get(AgentSession, request["session_id"])
        assert all(getattr(agent, field) is None for field in claim)
        assert agent.ember_session_id is None
        assert agent.prior_ember_lineage_id == "lineage"
        after = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
        assert after["turns_sha256"] == before["turns_sha256"]
        assert after["cursor_sha"] == before["cursor_sha"]
        assert db.exec(select(AgentTurn)).one().cost_usd is None
        assert db.exec(select(AgentCapacityReservation)).one().state == "settled"
        assert len(db.exec(select(RoutineReconciliation)).all()) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"guest_cleanup_guest_id": "other-guest"},
        {"guest_cleanup_workflow_id": "other-workflow"},
        {"guest_cleanup_id": None},
        {"guest_cleanup_dispatch_json": None},
        {"guest_cleanup_dispatch_json": "{}"},
    ],
)
def test_kg_reconciliation_refuses_mismatched_cleanup_claim(database, change):
    request, _ = held(database, delivery_error=True)
    before, _ = _add_cleanup_claim(database, request, **change)
    with pytest.raises(ValueError, match="cleanup claim ownership conflict"):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert (
            reconciliation.read_reconciliation_state(
                db, request["job_name"], request["session_id"]
            )
            == before
        )
        assert db.exec(select(AgentCapacityReservation)).one().state == "uncertain"
        assert db.exec(select(RoutineReconciliation)).first() is None


@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize("guest_state", ["evicted", "destroyed"])
@pytest.mark.parametrize("review_mode", ["legacy", "stop", "correction"])
@pytest.mark.parametrize("early_terminal", [False, True])
def test_factory_supervision_atomically_reconciles_terminal_held_job(
    database, monkeypatch, applied, guest_state, review_mode, early_terminal
):
    import asyncio
    from factory.execution import permit_supervision as supervision
    from factory.execution.models import ProbeObservation
    from factory.execution.review_leases import ReservationReview
    from factory.orchestration.factory_models import FactoryStart
    from factory.orchestration.models import SwarmTask, SwarmNodeRun

    request, before = held(database, delivery_error=True, applied=applied)
    engine = database.execution_options(
        schema_translate_map={
            "agent_sessions": None,
            "claude_agent": None,
            "swarm": None,
        }
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                ProbeObservation,
                SwarmTask,
                FactoryStart,
                SwarmNodeRun,
                ReservationReview,
            )
        ],
    )
    monkeypatch.setenv("FACTORY_RESERVATION_REVIEW_ENABLED", "true")
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    monkeypatch.setattr(supervision, "get_engine", lambda: engine)
    now = datetime.now(timezone.utc)

    started = int((now - timedelta(seconds=90)).timestamp() * 1000)
    if early_terminal:
        with Session(engine) as db, db.begin():
            permit = db.exec(select(AgentCapacityReservation)).one()
            permit.owner = "worker"
            db.add(permit)
    if review_mode != "legacy":
        from factory.execution import review_leases

        with Session(engine) as db:
            permit = db.exec(select(AgentCapacityReservation)).one()
            agent = db.get(AgentSession, permit.session_id)
            agent.status = "failed"
            turn = db.exec(select(AgentTurn)).one()
            turn.stop_reason = UNKNOWN_INVOCATION
            turn.usage_json = json.dumps(
                {"recovery": {"claim_owner": permit.owner, "dispatch_count": 1}}
            )
            permit.outcome = "reservation_review_stop"
            if review_mode == "correction":
                turn.seq = 2
                permit.pending_seq = 2
                db.add(
                    AgentTurn(
                        session_id=agent.id,
                        seq=1,
                        prompt="first",
                        result_text="needs correction",
                        terminal_reason="completed",
                    )
                )
                db.add(
                    AgentCapacityReservation(
                        local_session_id=agent.local_session_id,
                        session_id=agent.id,
                        pending_seq=1,
                        tier="kg",
                        state="settled",
                        routine_job_name=permit.routine_job_name,
                    )
                )
            db.add(turn)
            db.add(permit)
            db.add(agent)
            db.flush()
            db.add(
                ReservationReview(
                    permit_id=permit.id,
                    identity_sha256=review_leases.identity(permit),
                    lease_expires_at=now,
                    state="stopping",
                    verdict="stop" if review_mode == "stop" else "steer",
                    stop_intent_json=json.dumps(
                        {
                            "snapshot": {
                                "session_id": agent.id,
                                "guest_id": "guest",
                                "seq": permit.pending_seq,
                                "owner": permit.owner,
                                "workflow_id": agent.workflow_id,
                                "dispatch_count": 1,
                            },
                            "precondition": dict(
                                session_id="guest",
                                generation=0,
                                invoke_started_at=started,
                                vm_id="vm",
                                node_id="node",
                                pod_uid="pod",
                                instance_id="node/pod",
                                boot_id="boot",
                            ),
                        }
                    ),
                )
            )
            db.commit()
            before = reconciliation.read_reconciliation_state(
                db, request["job_name"], request["session_id"]
            )

    if early_terminal:
        with Session(engine) as db:
            permit = db.exec(
                select(AgentCapacityReservation).where(
                    AgentCapacityReservation.state == "uncertain"
                )
            ).one()
            permit.owner = "worker"
            turn = db.exec(
                select(AgentTurn).where(
                    AgentTurn.session_id == permit.session_id,
                    AgentTurn.seq == permit.pending_seq,
                )
            ).one()
            turn.created_at = now - timedelta(seconds=60)
            turn.usage_json = json.dumps(
                {
                    "recovery": {
                        "claim_owner": "worker",
                        "dispatch_count": 1,
                        "last_dispatch_at": (now - timedelta(seconds=100)).isoformat(),
                    }
                }
            )
            db.add_all([permit, turn])
            db.commit()
            before = reconciliation.read_reconciliation_state(
                db, request["job_name"], request["session_id"]
            )

    class Transport:
        async def get_session(self, guest):
            assert guest == "guest"
            return {
                "session_id": guest,
                "state": guest_state,
                "generation": 0,
                "invoke_started_at": int(
                    (now - timedelta(seconds=90)).timestamp() * 1000
                ),
                "last_invoke_at": None
                if early_terminal
                else int((now - timedelta(seconds=30)).timestamp() * 1000),
                "updated_at": int(
                    (
                        now - timedelta(seconds=60, milliseconds=185)
                        if early_terminal
                        else now - timedelta(seconds=20)
                    ).timestamp()
                    * 1000
                ),
            }

    asyncio.run(supervision.sweep_once(Transport()))
    with Session(engine) as db:
        permit = db.exec(
            select(AgentCapacityReservation).order_by(
                AgentCapacityReservation.pending_seq.desc()
            )
        ).first()
        assert permit.state == "settled"
        after = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
        assert after["job_status"] == "reconciled_unknown"
        assert (after["next_run_at"] is None) == (applied or review_mode == "stop")
        assert after["turns_sha256"] == before["turns_sha256"]
        assert db.get(ProbeObservation, permit.id).reason == "guest_cessation_confirmed"


def test_explicit_stop_reconciles_without_rearming_unprocessed_raw(database):
    request, before = held(database)
    request["disposition"] = "stop"
    result = reconciliation.reconcile_held_job(**request)
    assert result["next_run_at"] is None
    with Session(database) as db:
        after = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
        assert after["next_run_at"] is None
        assert after["provenance_sha256"] == before["provenance_sha256"]
        assert after["turns_sha256"] == before["turns_sha256"]


def test_predispatch_review_stop_prevents_routine_failure_retry_and_next_claim(
    database, monkeypatch
):
    from factory import reservation_reviews as reviews
    from factory.execution import review_leases as leases

    from factory.execution.models import ProbeObservation

    SQLModel.metadata.create_all(
        database,
        tables=[leases.ReservationReview.__table__, ProbeObservation.__table__],
    )
    monkeypatch.setattr(reviews, "get_engine", lambda: database)
    monkeypatch.setenv("FACTORY_RESERVATION_REVIEW_ENABLED", "true")
    with Session(database) as db:
        db.execute(
            text(
                "INSERT INTO routine_jobs (name,routine_kind,interval_secs,next_run_at,locked_by,locked_at,ttl_secs,payload) VALUES ('kg:pending','kg-drain',900,CURRENT_TIMESTAMP,'worker',CURRENT_TIMESTAMP,2100,'{}')"
            )
        )
        db.add(
            AgentCapacityReservation(
                local_session_id="cycle:kg-drain:kg:pending",
                pending_seq=1,
                tier="kg",
                model="luna",
                routine_job_name="kg:pending",
                created_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        )
        db.commit()
    candidate = reviews.claim_review()
    reviews.apply_decision(
        candidate,
        {"action": "stop", "reason": "Do not retry this approach", "guidance": ""},
    )
    assert not routine_jobs.complete_job(
        "kg:pending", "error", expected_holder="worker", defer_seconds=300
    )
    assert not routine_jobs.defer_job("kg:pending", 300)
    assert (
        routine_jobs.claim_job("next-worker", ttl_secs=2100, kinds=("kg-drain",))
        is None
    )
    with Session(database) as db:
        row = db.execute(
            text(
                "SELECT last_status,next_run_at FROM routine_jobs WHERE name='kg:pending'"
            )
        ).one()
        assert row.last_status == "factory_stopped"
        assert row.next_run_at is None
        assert db.exec(select(AgentCapacityReservation)).one().state == "settled"


@pytest.mark.parametrize(
    "change", ["old_invoke", "wrong_owner", "parked", "missing_owner", "future_invoke"]
)
def test_terminal_dispatch_reconciliation_rejects_unmatched_evidence(database, change):
    request, _ = held(database, delivery_error=True, completion_recorded=False)
    now = datetime.now(timezone.utc)
    with Session(database) as db, db.begin():
        permit = db.exec(select(AgentCapacityReservation)).one()
        permit.owner = None if change == "missing_owner" else "worker"
        turn = db.exec(select(AgentTurn)).one()
        turn.created_at = now - timedelta(seconds=60)
        turn.usage_json = json.dumps(
            {
                "recovery": {
                    "claim_owner": "other" if change == "wrong_owner" else "worker",
                    "dispatch_count": 1,
                    "last_dispatch_at": (now - timedelta(seconds=100)).isoformat(),
                }
            }
        )
        db.add_all([permit, turn])
    request["cessation"].update(
        state="parked" if change == "parked" else "evicted",
        invoke_started_at=int(
            (
                now
                - timedelta(
                    seconds=110
                    if change == "old_invoke"
                    else 50
                    if change == "future_invoke"
                    else 90
                )
            ).timestamp()
            * 1000
        ),
        updated_at=int((now - timedelta(seconds=61)).timestamp() * 1000),
    )
    with Session(database) as db:
        original = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
    request["expected_state_sha256"] = original["state_sha256"]
    with pytest.raises(ValueError, match="does not match the held dispatch"):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert (
            reconciliation.read_reconciliation_state(
                db, request["job_name"], request["session_id"]
            )
            == original
        )
        assert db.exec(select(RoutineReconciliation)).first() is None


def unbound_held(database, monkeypatch, *, applied=False, outcome="lease_expired"):
    from factory.execution import permit_supervision as supervision
    from factory.execution.models import ProbeObservation
    from factory.execution.review_leases import ReservationReview
    from factory.orchestration.factory_models import FactoryStart
    from factory.orchestration.models import SwarmTask, SwarmNodeRun

    request, _ = held(database, delivery_error=True, applied=applied)
    engine = database.execution_options(
        schema_translate_map={
            "agent_sessions": None,
            "claude_agent": None,
            "swarm": None,
        }
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                ProbeObservation,
                ReservationReview,
                FactoryStart,
                SwarmTask,
                SwarmNodeRun,
            )
        ],
    )
    monkeypatch.setenv("FACTORY_RESERVATION_REVIEW_ENABLED", "true")
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    monkeypatch.setattr(supervision, "get_engine", lambda: engine)
    with Session(engine) as db, db.begin():
        agent = db.get(AgentSession, request["session_id"])
        agent.status = "failed"
        for field in (
            "ember_session_id",
            "ember_lineage_id",
            "ember_session_token",
            "cli_session_id",
        ):
            setattr(agent, field, None)
        permit = db.exec(select(AgentCapacityReservation)).one()
        permit.outcome = outcome
        permit.owner = "worker"
        turn = db.exec(select(AgentTurn)).one()
        turn.stop_reason = UNKNOWN_INVOCATION
        turn.result_text = ""
        turn.usage_json = json.dumps(
            {
                "recovery": {
                    "cause": outcome,
                    "claim_owner": "worker",
                    "dispatch_count": 1,
                    "last_dispatch_at": (
                        datetime.now(timezone.utc) - timedelta(minutes=2)
                    ).isoformat(),
                }
            }
        )
        db.add(agent)
        db.add(permit)
        db.add(turn)
        pid = permit.id
    return engine, request, pid


@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize(
    "outcome", ["lease_expired", "delivery_error", "executor_cancelled"]
)
def test_supervision_recovers_unbound_job_atomically(
    database, monkeypatch, applied, outcome
):
    import asyncio
    from factory.execution import permit_supervision as supervision

    engine, request, pid = unbound_held(
        database, monkeypatch, applied=applied, outcome=outcome
    )
    with Session(engine) as db:
        before = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )

    class NoTransport:
        def __getattr__(self, name):
            raise AssertionError(
                f"Unbound recovery must not contact guest transport: {name}"
            )

    asyncio.run(supervision.sweep_once(NoTransport()))
    asyncio.run(supervision.sweep_once(NoTransport()))
    with Session(engine) as db:
        after = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
        assert (after["next_run_at"] is None) == applied
        assert after["turns_sha256"] == before["turns_sha256"]
        assert after["payload_sha256"] == before["payload_sha256"]
        assert db.get(AgentCapacityReservation, pid).outcome == "no_guest_bound"
        assert db.get(AgentCapacityReservation, pid).state == "settled"
        audit = db.exec(select(RoutineReconciliation)).one()
        result = json.loads(audit.result_json)
        assert result["no_guest_bound"] is True
        assert result["guest_cessation_confirmed"] is False
        assert db.exec(select(AgentTurn)).one().cost_usd is None
        with pytest.raises(store.SessionOutcomeUnknown):
            store.set_ember_session(
                db, request["session_id"], "late-guest", "token", None
            )


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "binding",
        "partial",
        "legacy",
        "identity",
        "workflow",
        "pending",
        "competing_permit",
    ],
)
def test_unbound_recovery_refuses_changed_or_incomplete_proof(
    database, monkeypatch, change
):
    from factory.execution import permit_supervision as supervision

    engine, request, pid = unbound_held(database, monkeypatch)
    candidate = supervision._prepare(pid)
    assert candidate is not None
    with Session(engine) as db, db.begin():
        agent = db.get(AgentSession, request["session_id"])
        permit = db.get(AgentCapacityReservation, pid)
        turn = db.exec(select(AgentTurn)).one()
        if change == "owner":
            permit.owner = "other-worker"
        elif change == "binding":
            agent.prior_ember_lineage_id = "old-binding"
        elif change == "partial":
            usage = json.loads(turn.usage_json)
            usage["recovery"]["partial_text"] = "possible delivery"
            turn.usage_json = json.dumps(usage)
        elif change == "legacy":
            turn.stop_reason = None
            agent.status = "warn"
            permit.outcome = "delivery_error"
        elif change == "identity":
            turn.prompt = "changed prompt"
        elif change == "pending":
            db.add(
                PendingMessage(session_id=agent.id, seq=2, message_text="late retry")
            )
        elif change == "competing_permit":
            db.add(
                AgentCapacityReservation(
                    local_session_id="other-attempt",
                    session_id=agent.id,
                    pending_seq=2,
                    tier="kg",
                    routine_job_name=request["job_name"],
                    state="reserved",
                )
            )
        else:
            db.execute(text("UPDATE workflow_status SET status='PENDING'"))
        db.add(agent)
        db.add(permit)
        db.add(turn)
    supervision._record_no_guest(candidate)
    with Session(engine) as db:
        assert db.get(AgentCapacityReservation, pid).state == "uncertain"
        assert db.exec(select(RoutineReconciliation)).first() is None
        assert (
            reconciliation.read_reconciliation_state(
                db, request["job_name"], request["session_id"]
            )["next_run_at"]
            is None
        )


def test_unbound_recovery_rolls_back_when_audit_fails(database, monkeypatch):
    from factory.execution import permit_supervision as supervision

    engine, request, pid = unbound_held(database, monkeypatch)
    candidate = supervision._prepare(pid)
    add = Session.add

    def fail_audit(self, instance, *args, **kwargs):
        if isinstance(instance, RoutineReconciliation):
            raise RuntimeError("audit unavailable")
        return add(self, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "add", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        supervision._record_no_guest(candidate)
    with Session(engine) as db:
        assert db.get(AgentCapacityReservation, pid).state == "uncertain"
        assert db.exec(select(RoutineReconciliation)).first() is None
        assert (
            reconciliation.read_reconciliation_state(
                db, request["job_name"], request["session_id"]
            )["next_run_at"]
            is None
        )


class NoGuestTransport:
    def __init__(self):
        self.accesses = []

    def __getattr__(self, name):
        self.accesses.append(name)
        raise AssertionError(f"Unbound recovery contacted guest transport: {name}")


@pytest.fixture
def interrupted_dispatch(database, monkeypatch):
    """Compose the real writers, cancelling before the binding callback runs."""
    from factory.execution import mcp, permit_supervision as supervision
    from factory.execution.models import ProbeObservation
    from factory.execution.review_leases import ReservationReview
    from factory.orchestration.factory_models import FactoryStart
    from factory.orchestration.models import SwarmTask, SwarmNodeRun

    engine = database.execution_options(
        schema_translate_map={
            "agent_sessions": None,
            "claude_agent": None,
            "swarm": None,
        }
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                ProbeObservation,
                ReservationReview,
                FactoryStart,
                SwarmTask,
                SwarmNodeRun,
            )
        ],
    )
    attempt = {
        "engine": engine,
        "name": "kg:interrupted",
        "prompt": "extract the original interrupted raw input",
        "deliveries": [],
        "clock": datetime(2026, 10, 1, tzinfo=timezone.utc),
    }

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                attempt["clock"].astimezone(tz)
                if tz
                else attempt["clock"].replace(tzinfo=None)
            )

    for module in (mcp, store, admission, routine_jobs, reconciliation, supervision):
        monkeypatch.setattr(module, "get_engine", lambda: attempt["engine"])
    for module in (store, admission, reconciliation):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setenv("FACTORY_RESERVATION_REVIEW_ENABLED", "true")
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    monkeypatch.setattr(supervision, "_now", lambda: attempt["clock"])
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _: None)

    async def cancel_before_binding(existing, cli, prompt, model, **kwargs):
        assert existing is None
        assert cli is None
        assert callable(kwargs["on_create"])
        await kwargs["admission_check"]()
        attempt["deliveries"].append(prompt)
        # In production deliver awaits this callback's commit before model POST.
        # The rollout here cancels before even that callback, not after a POST.
        raise asyncio.CancelledError

    monkeypatch.setattr(mcp._transport, "deliver", cancel_before_binding)
    with Session(engine) as db, db.begin():
        db.execute(
            text(
                "INSERT INTO routine_jobs "
                "(name,routine_kind,next_run_at,ttl_secs,payload,created_by) "
                "VALUES (:name,'kg-drain','2000-01-01',2100,:payload,'test')"
            ),
            {
                "name": attempt["name"],
                "payload": json.dumps({"raw_id": "interrupted", "attempt": 2}),
            },
        )
        db.execute(
            text(
                "INSERT INTO raw_inputs VALUES "
                "(1,'interrupted','codex-session','original-content','{}')"
            )
        )
        db.execute(
            text(
                "INSERT INTO workflow_status VALUES "
                "('interrupted-cycle','drain_cycle','SUCCESS','original-version')"
            )
        )
    with Session(engine) as db, db.begin():
        admission.lock_pool(db)
        job = routine_jobs.claim_job(
            "original-drainer", 2100, kind="kg-drain", session=db
        )
        assert job is not None
        local_id = "interrupted-cycle:kg-drain:" + attempt["name"]
        assert admission.reserve_start(
            db, local_id, tier="kg", model="luna", routine_job_name=attempt["name"]
        )
        agent = store.create_session(
            db,
            local_id,
            "<guest>",
            "main",
            model="luna",
            workflow_id="interrupted-cycle",
            node_key="kg-drain",
            admission_tier="kg",
            commit=False,
        )
        attempt["sid"] = agent.id
        attempt["pid"] = admission.reservation(db, local_id).id
        attempt["guard"] = {
            "expected_locked_by": "original-drainer",
            "expected_locked_at": job["locked_at"],
        }
    with Session(engine) as db:
        store.create_pending_message(db, attempt["sid"], attempt["prompt"], "luna")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mcp._execute_pending_message(attempt["sid"]))
    assert routine_jobs.hold_job_for_unknown_outcome(
        attempt["name"], attempt["sid"], "executor cancelled", **attempt["guard"]
    )
    with Session(engine) as db:
        attempt["before"] = reconciliation.read_reconciliation_state(
            db, attempt["name"], attempt["sid"]
        )
        turn = db.exec(select(AgentTurn)).one()
        recovery = json.loads(turn.usage_json)["recovery"]
        assert recovery["cause"] == "executor_cancelled"
        assert recovery["dispatch_count"] == 1
        assert recovery["last_dispatch_at"]
        attempt["owner"] = recovery["claim_owner"]
        assert (
            db.get(AgentCapacityReservation, attempt["pid"]).owner == attempt["owner"]
        )
        assert db.get(AgentSession, attempt["sid"]).ember_session_id is None
    assert routine_jobs.claim_job("premature", 2100, kind="kg-drain") is None
    assert_interrupted_evidence(attempt, settled=False)
    yield attempt
    attempt["engine"].dispose()


def assert_interrupted_evidence(attempt, *, settled, active_count=None):
    """Every scenario preserves the exact lost turn and accounts for capacity."""
    from factory.execution import mcp
    from factory.execution.models import ProbeObservation

    with Session(attempt["engine"]) as db:
        after = reconciliation.read_reconciliation_state(
            db, attempt["name"], attempt["sid"]
        )
        for field in ("turns_sha256", "payload_sha256"):
            assert after[field] == attempt["before"][field]
        turn = store.get_turn(db, attempt["sid"], 1)
        assert turn.prompt == attempt["prompt"]
        assert turn.cost_usd is None
        assert turn.stop_reason == UNKNOWN_INVOCATION
        assert store.get_pending_message(db, attempt["sid"], 1) is None
        permit = db.get(AgentCapacityReservation, attempt["pid"])
        assert permit.state == ("settled" if settled else "uncertain")
        assert permit.outcome == ("no_guest_bound" if settled else "executor_cancelled")
        assert (permit.settled_at is not None) == settled
        assert len(db.exec(select(RoutineReconciliation)).all()) == int(settled)
        observation = db.get(ProbeObservation, attempt["pid"])
        if settled:
            assert observation is not None
            assert observation.reason == "no_guest_bound"
            assert observation.settled_at == permit.settled_at
        elif observation is not None:
            assert observation.settled_at is None
        active = db.exec(
            select(AgentCapacityReservation).where(
                AgentCapacityReservation.state != "settled"
            )
        ).all()
        expected_active = int(not settled) if active_count is None else active_count
        assert len(active) == expected_active
        assert admission.free_background_slots(db) == min(
            admission.background_limit() - expected_active,
            admission.total_limit() - expected_active,
        )
    asyncio.run(mcp._execute_pending_message(attempt["sid"]))
    assert attempt["deliveries"] == [attempt["prompt"]]


def sweep_interrupted(attempt):
    from factory.execution import permit_supervision as supervision

    attempt["clock"] += timedelta(seconds=15)
    transport = NoGuestTransport()
    asyncio.run(supervision.sweep_once(transport))
    # sweep_once catches exceptions, so the tripwire alone cannot prove this.
    assert transport.accesses == []


def test_composed_interruption_rearms_once_after_commit(
    interrupted_dispatch, monkeypatch
):
    """Cases a, c and f: scheduler visibility, duplicate holds and no replay."""
    from agent import api

    attempt = interrupted_dispatch
    reconcile = api.reconcile_held_job
    committed = []

    def check_commit_boundary(**kwargs):
        result = reconcile(**kwargs)
        # The writer has re-armed its own transaction, but another scheduler
        # connection must still see the old hold until the outer commit.
        assert kwargs["session"].exec(select(RoutineReconciliation)).one()
        assert routine_jobs.claim_job("before-commit", 2100, kind="kg-drain") is None
        committed.append(result)
        return result

    monkeypatch.setattr(api, "reconcile_held_job", check_commit_boundary)
    sweep_interrupted(attempt)
    assert len(committed) == 1
    with Session(attempt["engine"]) as db:
        settled_at = db.get(AgentCapacityReservation, attempt["pid"]).settled_at
    assert not routine_jobs.hold_job_for_unknown_outcome(
        attempt["name"], attempt["sid"], "late old hold", **attempt["guard"]
    )
    sweep_interrupted(attempt)
    assert_interrupted_evidence(attempt, settled=True)
    job = routine_jobs.claim_job("ordinary-scheduler", 2100, kind="kg-drain")
    assert job is not None and job["name"] == attempt["name"]
    assert routine_jobs.claim_job("duplicate-scheduler", 2100, kind="kg-drain") is None
    sweep_interrupted(attempt)
    with Session(attempt["engine"]) as db:
        assert db.get(AgentCapacityReservation, attempt["pid"]).settled_at == settled_at
    assert len(committed) == 1
    assert_interrupted_evidence(attempt, settled=True)


def test_composed_interruption_reconciliation_survives_restart(
    interrupted_dispatch, monkeypatch
):
    """Cases b and f: fail after the real writes, reopen, then reconcile once."""
    from factory.execution import permit_supervision as supervision
    from factory.execution.models import ProbeObservation

    attempt = interrupted_dispatch
    candidate = supervision._prepare(attempt["pid"])
    assert candidate is not None
    add = Session.add
    flushed_settlements = []

    def interrupt_after_observation_write(db, instance, *args, **kwargs):
        result = add(db, instance, *args, **kwargs)
        if isinstance(instance, ProbeObservation) and instance.settled_at is not None:
            # Fail only after *all* settlement writes have reached the database,
            # including the observation timestamp, before the outer commit.
            db.flush()
            assert db.exec(select(RoutineReconciliation)).one()
            assert db.get(AgentCapacityReservation, attempt["pid"]).state == "settled"
            assert db.exec(select(ProbeObservation.settled_at)).one() is not None
            assert (
                reconciliation.read_reconciliation_state(
                    db, attempt["name"], attempt["sid"]
                )["next_run_at"]
                is not None
            )
            flushed_settlements.append(instance.settled_at)
            raise RuntimeError("process interrupted after settlement writes")
        return result

    monkeypatch.setattr(Session, "add", interrupt_after_observation_write)
    with pytest.raises(RuntimeError, match="process interrupted"):
        supervision._record_no_guest(candidate)
    assert len(flushed_settlements) == 1
    attempt["engine"].dispose()
    attempt["engine"] = create_engine(
        attempt["engine"].url,
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options=attempt["engine"].get_execution_options(),
    )
    with Session(attempt["engine"]) as db:
        assert db.get(ProbeObservation, attempt["pid"]).settled_at is None
        state = reconciliation.read_reconciliation_state(
            db, attempt["name"], attempt["sid"]
        )
        assert state["next_run_at"] is None
        assert state["state_sha256"] == attempt["before"]["state_sha256"]
    assert routine_jobs.claim_job("restarted-too-early", 2100, kind="kg-drain") is None
    assert_interrupted_evidence(attempt, settled=False)
    monkeypatch.setattr(Session, "add", add)
    sweep_interrupted(attempt)
    sweep_interrupted(attempt)
    assert_interrupted_evidence(attempt, settled=True)
    assert (
        routine_jobs.claim_job("restarted-scheduler", 2100, kind="kg-drain") is not None
    )
    assert routine_jobs.claim_job("second-scheduler", 2100, kind="kg-drain") is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("binding", "prior_binding_evidence"),
        ("owner", "missing_dispatch_identity"),
        ("pending", "pending_executor"),
        ("session", "identity_changed"),
    ],
)
def test_composed_interruption_refuses_stale_identity(
    interrupted_dispatch, change, reason
):
    """Cases d and f: exact proof is re-read after prepare, before settlement."""
    from factory.execution import permit_supervision as supervision
    from factory.execution.models import ProbeObservation

    attempt = interrupted_dispatch
    candidate = supervision._prepare(attempt["pid"])
    assert candidate is not None
    with Session(attempt["engine"]) as db, db.begin():
        if change == "binding":
            db.get(AgentSession, attempt["sid"]).prior_ember_lineage_id = "late-binding"
        elif change == "owner":
            db.get(AgentCapacityReservation, attempt["pid"]).owner = "other-executor"
        elif change == "session":
            # last_turn_at is covered by the identity hash but by no
            # eligibility predicate, so only the hash re-check can refuse it.
            db.get(AgentSession, attempt["sid"]).last_turn_at = datetime(
                2026, 10, 1, 0, 0, 5, tzinfo=timezone.utc
            )
        else:
            # A competing writer's queue is evidence, never execute it here.
            db.add(
                PendingMessage(
                    session_id=attempt["sid"], seq=2, message_text="late retry"
                )
            )
    assert supervision._record_no_guest(candidate) == reason
    with Session(attempt["engine"]) as db:
        observation = db.get(ProbeObservation, attempt["pid"])
        assert observation.reason == reason
        assert observation.settled_at is None
        assert (
            reconciliation.read_reconciliation_state(
                db, attempt["name"], attempt["sid"]
            )["next_run_at"]
            is None
        )
    assert routine_jobs.claim_job("unsafe-scheduler", 2100, kind="kg-drain") is None
    assert_interrupted_evidence(attempt, settled=False)


def test_composed_interruption_old_notifications_preserve_new_attempt(
    interrupted_dispatch,
):
    """Cases c, e and f: old proof cannot touch a newly admitted live attempt."""
    from factory.execution import permit_supervision as supervision

    attempt = interrupted_dispatch
    old_candidate = supervision._prepare(attempt["pid"])
    assert old_candidate is not None
    sweep_interrupted(attempt)
    with Session(attempt["engine"]) as db, db.begin():
        admission.lock_pool(db)
        job = routine_jobs.claim_job("new-drainer", 2100, kind="kg-drain", session=db)
        assert job is not None
        local_id = "new-cycle:kg-drain:" + attempt["name"]
        assert admission.reserve_start(
            db, local_id, tier="kg", model="luna", routine_job_name=attempt["name"]
        )
        agent = store.create_session(
            db,
            local_id,
            "<guest>",
            "main",
            model="luna",
            workflow_id="new-cycle",
            node_key="kg-drain",
            admission_tier="kg",
            commit=False,
        )
        new_sid = agent.id
        new_pid = admission.reservation(db, local_id).id
    with Session(attempt["engine"]) as db:
        store.create_pending_message(db, new_sid, "new attempt prompt", "luna")
    assert store.claim_pending_message_for_session_sync(new_sid, "new-executor") == 1
    assert admission.recheck(new_sid, 1, "new-executor")
    with Session(attempt["engine"]) as db:
        store.set_ember_session(db, new_sid, "new-live-guest", "test-token", None)
    with Session(attempt["engine"]) as db:
        before_job = db.execute(text("SELECT * FROM routine_jobs")).mappings().one()
        before_permit = db.get(AgentCapacityReservation, new_pid).model_dump()
        before_agent = db.get(AgentSession, new_sid).model_dump()
        before_pending = store.get_pending_message(db, new_sid, 1).model_dump()
    assert not routine_jobs.hold_job_for_unknown_outcome(
        attempt["name"], attempt["sid"], "late old hold", **attempt["guard"]
    )
    assert store.release_pending_message_claim_sync(
        attempt["sid"], 1, attempt["owner"], "executor_cancelled", dispatch_count=1
    )
    assert supervision._record_no_guest(old_candidate) == "no_guest_bound"
    sweep_interrupted(attempt)
    with Session(attempt["engine"]) as db:
        assert dict(
            db.execute(text("SELECT * FROM routine_jobs")).mappings().one()
        ) == dict(before_job)
        assert db.get(AgentCapacityReservation, new_pid).model_dump() == before_permit
        assert db.get(AgentSession, new_sid).model_dump() == before_agent
        assert store.get_pending_message(db, new_sid, 1).model_dump() == before_pending
        assert before_permit["state"] == "running"
    assert routine_jobs.claim_job("duplicate-scheduler", 2100, kind="kg-drain") is None
    assert_interrupted_evidence(attempt, settled=True, active_count=1)


def held_review(engine, *, complete=True):
    request, _ = held(engine, scout=True)
    key = "cycle:kg-drain:kg-fact-review"
    member = key + ":fact:1"
    with Session(engine) as db, db.begin():
        db.execute(
            text("UPDATE routine_jobs SET name='kg-fact-review', payload=:payload"),
            {"payload": json.dumps({"mode": "fact-review", "review_protocol": 3})},
        )
        agent = db.get(AgentSession, request["session_id"])
        agent.local_session_id = key
        db.add(agent)
        db.execute(
            text(
                "CREATE TABLE fact_review_batches (invocation_key TEXT PRIMARY KEY, member_keys TEXT, status TEXT, completed_at TEXT)"
            )
        )
        db.execute(
            text(
                "CREATE TABLE fact_review_runs (invocation_key TEXT PRIMARY KEY, status TEXT, completed_at TEXT)"
            )
        )
        params = {
            "key": key,
            "keys": json.dumps([member]),
            "status": "complete" if complete else "prepared",
            "done": "2026-10-18" if complete else None,
        }
        db.execute(
            text("INSERT INTO fact_review_batches VALUES (:key,:keys,:status,:done)"),
            params,
        )
        db.execute(
            text("INSERT INTO fact_review_runs VALUES (:key,:status,:done)"),
            {**params, "key": member},
        )
    request["job_name"] = "kg-fact-review"
    with Session(engine) as db:
        before = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
    request["expected_state_sha256"] = before["state_sha256"]
    return request, before


def test_completed_review_rearms_without_replaying_or_changing_receipts(database):
    request, before = held_review(database)
    result = reconciliation.reconcile_held_job(**request)
    assert result["next_run_at"]
    assert reconciliation.reconcile_held_job(**request) == result
    with Session(database) as db:
        after = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
    assert after["review_batch"] == before["review_batch"]
    assert after["turns_sha256"] == before["turns_sha256"]
    assert after["session_status"] == "failed"
    assert after["guest_id"] is None


@pytest.mark.parametrize(
    "change",
    [
        "prepared",
        "missing",
        "member_missing",
        "member_incomplete",
        "wrong_membership",
        "wrong_payload",
        "wrong_guest",
    ],
)
def test_review_uncertainty_or_identity_mismatch_preserves_hold(database, change):
    request, _ = held_review(database, complete=change != "prepared")
    with Session(database) as db, db.begin():
        if change == "missing":
            db.execute(text("DELETE FROM fact_review_batches"))
        elif change == "member_missing":
            db.execute(text("DELETE FROM fact_review_runs"))
        elif change == "member_incomplete":
            db.execute(
                text("UPDATE fact_review_runs SET status='prepared', completed_at=NULL")
            )
        elif change == "wrong_membership":
            db.execute(
                text("UPDATE fact_review_batches SET member_keys='[\"other:fact:1\"]'")
            )
        elif change == "wrong_payload":
            db.execute(text("UPDATE routine_jobs SET payload='{}'"))
    if change == "wrong_guest":
        request["cessation"]["session_id"] = "other-guest"
    if change not in {"wrong_membership", "wrong_payload"}:
        with Session(database) as db:
            request["expected_state_sha256"] = reconciliation.read_reconciliation_state(
                db, request["job_name"], request["session_id"]
            )["state_sha256"]
    with pytest.raises(ValueError):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert db.execute(
            text("SELECT last_status,next_run_at FROM routine_jobs")
        ).one() == (UNKNOWN_INVOCATION, None)
        assert db.get(AgentSession, request["session_id"]).ember_session_id == "guest"
        assert db.exec(select(RoutineReconciliation)).all() == []


def test_completed_review_requires_fresh_cessation(database):
    request, _ = held_review(database)
    request["cessation"]["observed_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat()
    with pytest.raises(ValueError):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert db.execute(text("SELECT next_run_at FROM routine_jobs")).scalar() is None
        assert db.get(AgentSession, request["session_id"]).ember_session_id == "guest"


def recomputation_attempt(engine):
    from knowledge.models import FactReviewBatch, FactReviewRun

    request, _ = held(engine, scout=True)
    key = "cycle:kg-drain:kg-fact-review"
    engine = engine.execution_options(
        schema_translate_map={
            "agent_sessions": None,
            "claude_agent": None,
            "knowledge": None,
        }
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            FactReviewBatch.__table__,
            FactReviewRun.__table__,
            ProbeObservation.__table__,
        ],
    )
    with Session(engine) as db, db.begin():
        db.execute(
            text("UPDATE routine_jobs SET name='kg-fact-review',payload=:payload"),
            {"payload": json.dumps({"mode": "fact-review", "review_protocol": 3})},
        )
        agent = db.get(AgentSession, request["session_id"])
        agent.local_session_id = key
        permit = AgentCapacityReservation(
            local_session_id=key,
            session_id=agent.id,
            pending_seq=1,
            tier="kg",
            state="uncertain",
            outcome="response_lost_guest_ceased",
            routine_job_name="kg-fact-review",
        )
        db.add_all(
            [
                permit,
                FactReviewBatch(
                    invocation_key=key, member_keys=[key + ":fact:1", key + ":fact:2"]
                ),
                FactReviewRun(
                    invocation_key=key + ":fact:1",
                    status="complete",
                    completed_at=datetime.now(timezone.utc),
                    result={"outcome": "supported", "evidence": "retained"},
                ),
                FactReviewRun(
                    invocation_key=key + ":fact:2",
                    sources=[{"text": "retained source"}],
                ),
            ]
        )
        db.flush()
        pid = permit.id
    return (
        engine,
        {"mode": "preview", "session_id": request["session_id"], "permit_id": pid},
        key,
    )


def apply_request(db, preview):
    result = reconciliation.recover_review_job(
        actor="operator", recovery=preview, session=db
    )
    return {
        **preview,
        "mode": "apply",
        "reconciliation_key": "exact-review-recovery",
        "expected_state_sha256": result["expected_state_sha256"],
        "acknowledgment": "accept_duplicate_compute_and_writes",
    }


def test_operator_recomputation_preserves_unknown_history_and_completed_receipts(
    database,
):
    from knowledge.models import FactReviewBatch, FactReviewRun

    engine, preview, key = recomputation_attempt(database)
    with Session(engine) as db:
        before = reconciliation.read_reconciliation_state(
            db, "kg-fact-review", preview["session_id"]
        )
        completed = db.get(FactReviewRun, key + ":fact:1").model_dump()
        unfinished = db.get(FactReviewRun, key + ":fact:2").model_dump()
        permit_before = db.get(
            AgentCapacityReservation, preview["permit_id"]
        ).model_dump()
        request = apply_request(db, preview)
        db.commit()
        assert db.get(FactReviewRun, key + ":fact:1").model_dump() == completed
        assert db.get(FactReviewRun, key + ":fact:2").model_dump() == unfinished
        assert db.get(FactReviewBatch, key).status == "prepared"
        assert (
            db.get(AgentCapacityReservation, preview["permit_id"]).model_dump()
            == permit_before
        )
        assert db.exec(select(RoutineReconciliation)).all() == []
        assert (
            reconciliation.read_reconciliation_state(
                db, "kg-fact-review", preview["session_id"]
            )
            == before
        )
        result = reconciliation.recover_review_job(
            actor="operator", recovery=request, session=db
        )
        db.commit()
        assert result["abandoned_members"] == result["retained_completed_members"] == 1
        assert not result["guest_cessation_confirmed"]
        assert result["capacity_reservation_retained"]
        assert db.get(FactReviewRun, key + ":fact:1").model_dump() == completed
        assert db.get(FactReviewRun, key + ":fact:2").model_dump() == {
            **unfinished,
            "status": "abandoned",
        }
        assert db.get(FactReviewBatch, key).status == "abandoned"
        after = reconciliation.read_reconciliation_state(
            db, "kg-fact-review", preview["session_id"]
        )
        for field in ("session_sha256", "turns_sha256", "payload_sha256", "guest_id"):
            assert after[field] == before[field]
        permit = db.get(AgentCapacityReservation, preview["permit_id"])
        assert permit.state == "uncertain" and permit.settled_at is None
        assert permit.outcome == "operator_recompute_unknown"
        assert "kg-fact-review" not in admission.reserved_routine_jobs(db)
        assert db.exec(select(RoutineReconciliation)).one().actor == "operator"
        assert (
            reconciliation.recover_review_job(
                actor="operator", recovery=request, session=db
            )
            == result
        )
        with pytest.raises(ValueError, match="conflicts"):
            reconciliation.recover_review_job(
                actor="another", recovery=request, session=db
            )
        assert db.exec(select(RoutineReconciliation)).one()


@pytest.mark.parametrize(
    "change",
    ["digest", "permit", "pending", "workflow", "lock", "ack", "fence", "new_permit"],
)
def test_operator_recomputation_refuses_changed_or_active_ownership(database, change):
    from knowledge.models import FactReviewBatch

    engine, preview, key = recomputation_attempt(database)
    with Session(engine) as db:
        request = apply_request(db, preview)
        if change == "digest":
            request["expected_state_sha256"] = "0" * 64
        elif change == "permit":
            request["permit_id"] += 1
        elif change == "pending":
            db.add(
                PendingMessage(
                    session_id=preview["session_id"],
                    seq=2,
                    message_text="active",
                    model="luna",
                )
            )
        elif change == "workflow":
            db.execute(text("UPDATE workflow_status SET status='PENDING'"))
        elif change == "lock":
            db.execute(text("UPDATE routine_jobs SET locked_by='new-owner'"))
        elif change == "ack":
            request["acknowledgment"] = "yes"
        elif change == "fence":
            db.get(
                AgentSession, preview["session_id"]
            ).result_receipt_fence_id = "active-recovery"
        elif change == "new_permit":
            db.add(
                AgentCapacityReservation(
                    local_session_id="new",
                    pending_seq=1,
                    tier="kg",
                    routine_job_name="kg-fact-review",
                )
            )
        db.commit()
        with pytest.raises(ValueError):
            reconciliation.recover_review_job(
                actor="operator", recovery=request, session=db
            )
        assert db.get(FactReviewBatch, key).status == "prepared"
        assert (
            db.get(AgentCapacityReservation, preview["permit_id"]).outcome
            == "response_lost_guest_ceased"
        )
        assert db.exec(select(RoutineReconciliation)).first() is None
        assert db.execute(text("SELECT next_run_at FROM routine_jobs")).scalar() is None


def test_operator_recomputation_rolls_back_with_outer_transaction(database):
    from knowledge.models import FactReviewBatch

    engine, preview, key = recomputation_attempt(database)
    with Session(engine) as db:
        request = apply_request(db, preview)
        reconciliation.recover_review_job(
            actor="operator", recovery=request, session=db
        )
        db.rollback()
    with Session(engine) as db:
        assert db.get(FactReviewBatch, key).status == "prepared"
        assert (
            db.get(AgentCapacityReservation, preview["permit_id"]).state == "uncertain"
        )
        assert db.exec(select(RoutineReconciliation)).first() is None


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "wrong_guest",
        "wrong_owner",
        "wrong_dispatch",
        "stale",
        "future",
        "active",
        "generation_regression",
        "invoke_changed",
        "cp_regression",
        "bad_completion",
    ],
)
def test_operator_recomputation_releases_capacity_only_for_exact_fresh_cessation(
    database, monkeypatch, fault
):
    import factory.api

    engine, preview, key = recomputation_attempt(database)
    monkeypatch.setattr(reconciliation, "get_engine", lambda: engine)
    now = datetime.now(timezone.utc)
    dispatched = now - timedelta(minutes=3)
    started = dispatched + timedelta(seconds=1)
    stopped = now - timedelta(minutes=2)
    with Session(engine) as db, db.begin():
        permit = db.get(AgentCapacityReservation, preview["permit_id"])
        permit.owner = "original-owner"
        turn = db.exec(select(AgentTurn)).one()
        turn.usage_json = json.dumps(
            {
                "recovery": {
                    "claim_owner": "original-owner",
                    "dispatch_count": 1,
                    "last_dispatch_at": dispatched.isoformat(),
                }
            }
        )
    observation = {
        "session_id": "guest",
        "state": "evicted",
        "generation": 0,
        "invoke_started_at": int(started.timestamp() * 1000),
        "updated_at": int(stopped.timestamp() * 1000),
        "observed_at": now.isoformat(),
    }
    if fault == "missing":
        observation = None
    elif fault == "wrong_guest":
        observation["session_id"] = "different"
    elif fault == "wrong_owner":
        with Session(engine) as db, db.begin():
            db.get(
                AgentCapacityReservation, preview["permit_id"]
            ).owner = "another-owner"
    elif fault == "wrong_dispatch":
        observation["invoke_started_at"] = int(
            (dispatched - timedelta(seconds=1)).timestamp() * 1000
        )
    elif fault == "stale":
        observation["observed_at"] = (now - timedelta(minutes=1)).isoformat()
    elif fault == "future":
        observation["updated_at"] = int((now + timedelta(minutes=1)).timestamp() * 1000)
    elif fault == "active":
        observation["state"] = "running"
    if fault in {"generation_regression", "invoke_changed", "cp_regression"}:
        with Session(engine) as db, db.begin():
            db.add(
                ProbeObservation(
                    permit_id=preview["permit_id"],
                    guest_id="guest",
                    checked_at=now,
                    generation=1 if fault == "generation_regression" else 0,
                    invoke_started_at=int(started.timestamp() * 1000)
                    + (1 if fault == "invoke_changed" else 0),
                    cp_updated_at=int(stopped.timestamp() * 1000)
                    + (1 if fault == "cp_regression" else 0),
                )
            )
    elif fault == "bad_completion":
        observation["last_invoke_at"] = observation["updated_at"] + 1
    calls = []

    def observe(guest):
        calls.append(guest)
        return observation

    monkeypatch.setattr(factory.api, "observe_guest_for_recomputation", observe)
    result = reconciliation.recover_review_job(actor="operator", recovery=preview)
    request = {
        **preview,
        "mode": "apply",
        "reconciliation_key": "cessation-recompute",
        "expected_state_sha256": result["expected_state_sha256"],
        "acknowledgment": "accept_duplicate_compute_and_writes",
    }
    result = reconciliation.recover_review_job(actor="operator", recovery=request)
    assert calls == ["guest", "guest"]
    assert result["guest_cessation_confirmed"] is (fault is None)
    assert result["capacity_reservation_retained"] is (fault is not None)
    with Session(engine) as db:
        permit = db.get(AgentCapacityReservation, preview["permit_id"])
        assert permit.state == ("settled" if fault is None else "uncertain")
        assert permit.outcome == (
            "operator_recompute_ceased_unknown"
            if fault is None
            else "operator_recompute_unknown"
        )
        assert db.exec(select(AgentTurn)).one().stop_reason == UNKNOWN_INVOCATION
        assert db.get(AgentSession, preview["session_id"]).status == "failed"
        audit = json.loads(db.exec(select(RoutineReconciliation)).one().evidence_json)
        assert audit["observation"] == observation
    assert (
        reconciliation.recover_review_job(actor="operator", recovery=request) == result
    )
    assert calls == ["guest", "guest"]


@pytest.mark.parametrize(
    "change",
    [
        None,
        "no_audit",
        "owner",
        "turn",
        "guest",
        "ack",
        "digest",
        "receipt_owner",
        "receipt_guest",
        "receipt_missing",
        "result",
        "superseded",
        "old_terminal",
        "new_invoke",
        "running",
        "stale",
        "malformed_completion",
        "generation_regression",
        "late_receipt",
        "race",
    ],
)
def test_recovered_review_redispatch_settles_only_exact_audited_attempt(
    database, monkeypatch, change
):
    from factory.execution import permit_supervision as supervision
    from factory.execution.models import AgentResultReceipt
    from factory.orchestration.factory_models import FactoryStart
    from factory.orchestration.models import SwarmNodeRun, SwarmTask

    engine, preview, key = recomputation_attempt(database)
    engine = engine.execution_options(
        schema_translate_map={
            "agent_sessions": None,
            "claude_agent": None,
            "knowledge": None,
            "swarm": None,
        }
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (AgentResultReceipt, FactoryStart, SwarmNodeRun, SwarmTask)
        ],
    )
    monkeypatch.setattr(supervision, "get_engine", lambda: engine)
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    monkeypatch.setenv("FACTORY_RESERVATION_REVIEW_ENABLED", "true")
    now = datetime.now(timezone.utc)

    def at(seconds):
        return now - timedelta(seconds=seconds)

    with Session(engine) as db, db.begin():
        permit = db.get(AgentCapacityReservation, preview["permit_id"])
        permit.owner = "worker-two"
        turn = db.exec(
            select(AgentTurn).where(AgentTurn.session_id == preview["session_id"])
        ).one()
        turn.created_at = at(10)
        turn.usage_json = json.dumps(
            {
                "recovery": {
                    "claim_owner": "worker-two",
                    "dispatch_count": 2,
                    "last_dispatch_at": at(100).isoformat(),
                }
            }
        )
        db.add_all(
            [
                AgentResultReceipt(
                    id=f"receipt-{i}",
                    token_sha256=f"token-{i}",
                    session_id=preview["session_id"],
                    local_session_id=key,
                    seq=1,
                    dispatch_count=i,
                    claim_owner="worker-one" if i == 1 else "worker-two",
                    guest_id="guest",
                    request_sha256=f"request-{i}",
                    created_at=at(150 if i == 1 else 98),
                    accept_until=at(5),
                    retain_until=now + timedelta(days=1),
                    superseded_at=at(98) if i == 1 else None,
                    response_observer_released_at=at(19) if i == 2 else None,
                )
                for i in (1, 2)
            ]
        )
    candidate_before = supervision._prepare(preview["permit_id"])
    assert candidate_before is not None
    with Session(engine) as db, db.begin():
        request = apply_request(db, preview)
        reconciliation.recover_review_job(
            actor="operator", recovery=request, session=db
        )
        row = db.exec(select(RoutineReconciliation)).one()
        original_audit = row.model_dump()
        if change == "no_audit":
            db.delete(row)
        elif change in {"ack", "digest"}:
            data = json.loads(row.evidence_json)
            data["request"][
                "acknowledgment" if change == "ack" else "expected_state_sha256"
            ] = "wrong"
            row.evidence_json = json.dumps(data)
        elif change == "owner":
            db.get(AgentCapacityReservation, preview["permit_id"]).owner = "other"
        elif change == "turn":
            db.exec(select(AgentTurn)).one().result_text = "changed"
        elif change == "guest":
            db.get(AgentSession, preview["session_id"]).ember_session_id = "other"
        elif (change or "").startswith("receipt_") or change in {
            "result",
            "superseded",
            "late_receipt",
        }:
            receipt = db.get(AgentResultReceipt, "receipt-2")
            if change == "receipt_missing":
                db.delete(receipt)
            elif change == "receipt_owner":
                receipt.claim_owner = "other"
            elif change == "receipt_guest":
                receipt.guest_id = "other"
            elif change == "result":
                receipt.result_sha256 = "result"
            elif change == "late_receipt":
                receipt.created_at = at(15)
            else:
                receipt.superseded_at = now
    candidate = supervision._prepare(preview["permit_id"])
    if change in {"no_audit", "owner", "turn", "guest", "ack", "digest"}:
        assert candidate is None
    else:
        assert candidate == candidate_before
        observed = {
            "session_id": "guest",
            "state": "running" if change == "running" else "evicted",
            "generation": 0,
            "invoke_started_at": int(
                at(140 if change != "new_invoke" else 5).timestamp() * 1000
            ),
            "last_invoke_at": None,
            "updated_at": int(
                at(20 if change != "old_terminal" else 110).timestamp() * 1000
            ),
        }
        if change == "malformed_completion":
            observed["last_invoke_at"] = True
        if change == "generation_regression":
            with Session(engine) as db, db.begin():
                audit = db.get(ProbeObservation, preview["permit_id"])
                audit.generation = 1
                audit.invoke_started_at = observed["invoke_started_at"]
        if change == "race":
            with Session(engine) as db, db.begin():
                db.get(AgentCapacityReservation, preview["permit_id"]).owner = "raced"
        supervision._record(candidate, observed, at(60) if change == "stale" else now)
    with Session(engine) as db:
        permit = db.get(AgentCapacityReservation, preview["permit_id"])
        assert permit.state == ("settled" if change is None else "uncertain")
        assert permit.outcome == (
            "operator_recompute_ceased_unknown"
            if change is None
            else "operator_recompute_unknown"
        )
        if change is None:
            assert (
                db.exec(select(RoutineReconciliation)).one().model_dump()
                == original_audit
            )
            assert db.exec(select(AgentTurn)).one().stop_reason == UNKNOWN_INVOCATION
            assert (
                db.get(AgentSession, preview["session_id"]).ember_session_id == "guest"
            )
            assert supervision._prepare(preview["permit_id"]) is None


@pytest.fixture
def completed_raw(database, monkeypatch):
    from knowledge.models import (
        RawInput,
        AtomRawProvenance,
        Note,
        Chunk,
        NoteLink,
        Dispute,
    )
    from knowledge.entities import Entity, NoteEntity

    held_request, _ = held(database)
    name, sid = held_request["job_name"], held_request["session_id"]
    database = database.execution_options(
        schema_translate_map={
            "agent_sessions": None,
            "claude_agent": None,
            "knowledge": None,
        }
    )
    monkeypatch.setattr(reconciliation, "get_engine", lambda: database)
    with Session(database) as db:
        db.execute(text("DROP TABLE raw_inputs"))
        db.execute(text("DROP TABLE atom_raw_provenance"))
        db.commit()
    SQLModel.metadata.create_all(
        database,
        tables=[
            m.__table__
            for m in (
                RawInput,
                AtomRawProvenance,
                Note,
                Chunk,
                NoteLink,
                Dispute,
                Entity,
                NoteEntity,
            )
        ],
    )
    with Session(database) as db:
        db.add(
            RawInput(
                raw_id="raw",
                path="raws/raw.md",
                source="ember-session",
                content_hash="content",
                extra={"scope": "repo:jomcgi-org/homelab"},
            )
        )
        agent = db.get(AgentSession, sid)
        agent.status = "completed"
        turn = db.exec(select(AgentTurn).where(AgentTurn.session_id == sid)).one()
        turn.stop_reason = None
        turn.terminal_reason = "completed"
        turn.result_text = '```json\n{"assertions":[],"dispute_resolution":null,"doc_drift":[],"notes":"saved"}\n```'
        db.execute(
            text(
                "UPDATE routine_jobs SET last_status=NULL,locked_by='luna-drainer:cycle:0',locked_at='old-time',next_run_at='old-due' WHERE name=:name"
            ),
            {"name": name},
        )
        permit = AgentCapacityReservation(
            local_session_id=agent.local_session_id,
            pending_seq=1,
            session_id=sid,
            tier="kg",
            routine_job_name=name,
            state="settled",
            outcome="completed",
        )
        db.add(permit)
        db.commit()
        pid = permit.id
    monkeypatch.setenv("KG_COMPLETED_RAW_RECOVERY_ENABLED", "true")
    # Empty saved output needs no embeddings, but constructing the existing
    # client must not depend on a developer's model endpoint configuration.
    monkeypatch.setattr("knowledge.extraction.EmbeddingClient", lambda: object())
    return database, {
        "mode": "preview",
        "job_name": name,
        "session_id": sid,
        "permit_id": pid,
    }


def _completed_apply(request, preview):
    return {
        **request,
        "mode": "apply",
        "reconciliation_key": "saved-result-1",
        "expected_state_sha256": preview["expected_state_sha256"],
    }


def test_completed_raw_applies_original_result_and_replays_receipt(completed_raw):
    from knowledge.models import RawInput, AtomRawProvenance

    engine, request = completed_raw
    preview = reconciliation.recover_completed_raw(actor="operator", recovery=request)
    apply = _completed_apply(request, preview)
    result = reconciliation.recover_completed_raw(actor="operator", recovery=apply)
    assert result["rearmed"] is False and result["atoms"] == 0
    with Session(engine) as db:
        raw = db.exec(select(RawInput)).one()
        assert raw.extra["extraction_passes"] == 1
        assert len(db.exec(select(AtomRawProvenance)).all()) == 1
        job = (
            db.execute(text("SELECT * FROM routine_jobs WHERE name='kg:raw'"))
            .mappings()
            .one()
        )
        assert job["last_status"] == "reconciled_completed"
        assert job["locked_by"] is None and job["next_run_at"] is None
        turn = db.exec(select(AgentTurn)).one()
        assert (
            turn.terminal_reason == "completed"
            and '"notes":"saved"' in turn.result_text
        )
        db.execute(text("DELETE FROM routine_jobs WHERE name='kg:raw'"))
        db.commit()
    assert (
        reconciliation.recover_completed_raw(actor="operator", recovery=apply) == result
    )
    with pytest.raises(ValueError, match="conflicts"):
        reconciliation.recover_completed_raw(actor="other", recovery=apply)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE agent_sessions SET status='failed'",
        "UPDATE agent_turns SET terminal_reason='error'",
        "UPDATE routine_jobs SET locked_by='another-owner'",
        "UPDATE workflow_status SET status='PENDING'",
        "UPDATE capacity_reservations SET state='uncertain'",
    ],
)
def test_completed_raw_refuses_ambiguous_ownership(completed_raw, sql):
    engine, request = completed_raw
    with Session(engine) as db:
        db.execute(text(sql))
        db.commit()
    with pytest.raises(ValueError, match="ambiguous"):
        reconciliation.recover_completed_raw(actor="operator", recovery=request)


def test_completed_raw_changed_result_and_disabled_apply_stay_fenced(
    completed_raw, monkeypatch
):
    engine, request = completed_raw
    preview = reconciliation.recover_completed_raw(actor="operator", recovery=request)
    apply = _completed_apply(request, preview)
    monkeypatch.setenv("KG_COMPLETED_RAW_RECOVERY_ENABLED", "false")
    with pytest.raises(ValueError, match="disabled"):
        reconciliation.recover_completed_raw(actor="operator", recovery=apply)
    monkeypatch.setenv("KG_COMPLETED_RAW_RECOVERY_ENABLED", "true")
    with Session(engine) as db:
        db.execute(text("UPDATE agent_turns SET result_text='changed'"))
        db.commit()
    with pytest.raises(ValueError, match="state changed"):
        reconciliation.recover_completed_raw(actor="operator", recovery=apply)


def test_completed_raw_failure_rolls_back_application_and_lease(
    completed_raw, monkeypatch
):
    from knowledge.models import RawInput, AtomRawProvenance

    engine, request = completed_raw
    preview = reconciliation.recover_completed_raw(actor="operator", recovery=request)
    original = reconciliation.read_reconciliation_state

    def fail_after_application(db, *args):
        state = original(db, *args)
        if state["extraction_passes"] == 1:
            raise ValueError("injected finalization failure")
        return state

    monkeypatch.setattr(
        reconciliation, "read_reconciliation_state", fail_after_application
    )
    with pytest.raises(ValueError, match="injected"):
        reconciliation.recover_completed_raw(
            actor="operator", recovery=_completed_apply(request, preview)
        )
    with Session(engine) as db:
        assert db.exec(select(AtomRawProvenance)).all() == []
        assert "extraction_passes" not in db.exec(select(RawInput)).one().extra
        assert (
            db.execute(
                text("SELECT locked_by FROM routine_jobs WHERE name='kg:raw'")
            ).scalar_one()
            == "luna-drainer:cycle:0"
        )
        assert db.exec(select(RoutineReconciliation)).all() == []


def test_completed_raw_rejects_unbound_sibling_permit(completed_raw):
    engine, request = completed_raw
    with Session(engine) as db:
        db.add(
            AgentCapacityReservation(
                local_session_id="new-attempt",
                pending_seq=1,
                session_id=None,
                tier="kg",
                routine_job_name=request["job_name"],
                state="reserved",
            )
        )
        db.commit()
    with pytest.raises(ValueError, match="ambiguous"):
        reconciliation.recover_completed_raw(actor="operator", recovery=request)


def test_completed_raw_rejects_contradictory_settled_permit(completed_raw):
    engine, request = completed_raw
    with Session(engine) as db:
        permit = db.get(AgentCapacityReservation, request["permit_id"])
        permit.outcome = "delivery_error"
        db.commit()
    with pytest.raises(ValueError, match="ambiguous"):
        reconciliation.recover_completed_raw(actor="operator", recovery=request)


def test_completed_raw_concurrent_same_key_returns_one_receipt(
    completed_raw, monkeypatch
):
    from threading import Barrier, local

    engine, request = completed_raw
    preview = reconciliation.recover_completed_raw(actor="operator", recovery=request)
    apply = _completed_apply(request, preview)
    barrier, seen = Barrier(2), local()
    original = reconciliation._completed_raw_state

    def overlap(*args):
        state = original(*args)
        if not getattr(seen, "read", False):
            seen.read = True
            barrier.wait(timeout=10)
        return state

    monkeypatch.setattr(reconciliation, "_completed_raw_state", overlap)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                reconciliation.recover_completed_raw, actor="operator", recovery=apply
            )
            for _ in range(2)
        ]
        results = [f.result(timeout=20) for f in futures]
    assert results[0] == results[1]
    with Session(engine) as db:
        assert len(db.exec(select(RoutineReconciliation)).all()) == 1


def test_completed_raw_nonempty_application_rolls_back_with_receipt_failure(
    completed_raw, monkeypatch
):
    from knowledge.models import Note, Chunk, AtomRawProvenance, RawInput

    engine, request = completed_raw

    class Embedder:
        async def embed(self, text):
            return [0.0] * 1024

        async def embed_batch(self, texts):
            return [[0.0] * 1024 for _ in texts]

    monkeypatch.setattr("knowledge.extraction.EmbeddingClient", Embedder)
    monkeypatch.setattr("knowledge.atoms.EmbeddingClient", Embedder)
    monkeypatch.setattr(
        "knowledge.store.KnowledgeStore.search_notes_with_context",
        lambda *args, **kwargs: [],
    )
    output = {
        "assertions": [
            {
                "title": "Monolith retries extraction",
                "body": "When extraction fails, Monolith retries the work.",
                "scope": "repo:jomcgi-org/homelab",
                "verification_state": "unverified",
                "confidence": 0.8,
                "edges": {},
            }
        ],
        "dispute_resolution": None,
        "doc_drift": [],
        "notes": "saved",
    }
    with Session(engine) as db:
        db.exec(select(AgentTurn)).one().result_text = (
            "```json\n" + json.dumps(output) + "\n```"
        )
        db.commit()
    preview = reconciliation.recover_completed_raw(actor="operator", recovery=request)
    original = reconciliation.read_reconciliation_state
    saw_atoms = []

    def fail_after_atoms(db, *args):
        state = original(db, *args)
        if state["extraction_passes"] == 1:
            assert len(db.exec(select(Note)).all()) == 1
            assert len(db.exec(select(Chunk)).all()) >= 1
            saw_atoms.append(True)
            raise ValueError("receipt failure after atoms")
        return state

    monkeypatch.setattr(reconciliation, "read_reconciliation_state", fail_after_atoms)
    with pytest.raises(ValueError, match="receipt failure"):
        reconciliation.recover_completed_raw(
            actor="operator", recovery=_completed_apply(request, preview)
        )
    assert saw_atoms == [True]
    with Session(engine) as db:
        for model in (Note, Chunk, AtomRawProvenance, RoutineReconciliation):
            assert db.exec(select(model)).all() == []
        assert "extraction_passes" not in db.exec(select(RawInput)).one().extra
        assert (
            db.execute(
                text("SELECT locked_by FROM routine_jobs WHERE name='kg:raw'")
            ).scalar_one()
            == "luna-drainer:cycle:0"
        )
