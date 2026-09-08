"""File-backed integration tests for explicit KG cessation reconciliation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from agent import routine_reconciliation as reconciliation
from agent.routine_reconciliation_models import RoutineReconciliation
from agent_sessions import admission, store
from agent_sessions.constants import UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentSession,
    AgentTurn,
    PendingMessage,
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
