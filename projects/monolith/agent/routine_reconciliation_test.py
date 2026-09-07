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


def held(engine, *, scout=False, applied=False):
    name = "kg-repo-diff" if scout else "kg:raw"
    with Session(engine) as db:
        agent = AgentSession(
            local_session_id="cycle:kg-drain:" + name,
            workspace="guest",
            branch="main",
            workflow_id="cycle",
            node_key="kg-drain",
            admission_tier="kg",
            status="failed",
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
                stop_reason=UNKNOWN_INVOCATION,
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
        "last_invoke_at": int((now - timedelta(seconds=30)).timestamp() * 1000),
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


def test_rearm_preserves_unknown_history_payload_and_lineage(database):
    request, before = held(database, scout=True)
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


def test_existing_extraction_retained_without_false_correction_success(database):
    request, before = held(database, applied=True)
    assert reconciliation.reconcile_held_job(**request)["next_run_at"] is None
    with Session(database) as db:
        after = reconciliation.read_reconciliation_state(
            db, request["job_name"], request["session_id"]
        )
        assert after["job_status"] == "reconciled_unknown"
        assert (
            after["raw_sha256"] == before["raw_sha256"]
            and after["provenance_sha256"] == before["provenance_sha256"]
        )
        assert (
            after["latest_stop_reason"] == UNKNOWN_INVOCATION
            and after["latest_terminal_reason"] == "error"
        )


def test_same_request_replay_survives_job_deletion_and_stale_observation(
    database, monkeypatch
):
    request, _ = held(database)
    result = reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        db.execute(text("DELETE FROM routine_jobs"))
        db.commit()
    monkeypatch.setattr(reconciliation, "MAX_EVIDENCE_AGE_SECONDS", -1)
    assert reconciliation.reconcile_held_job(**request) == result
    with pytest.raises(ValueError, match="conflicts"):
        reconciliation.reconcile_held_job(**{**request, "actor": "different"})
    with Session(database) as db:
        assert len(db.exec(select(RoutineReconciliation)).all()) == 1


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
def test_changed_expected_state_refuses_without_audit_or_releasing_permits(
    database, change
):
    request, _ = held(database)
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
        assert not db.exec(select(AgentCapacityReservation)).all()
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
def test_missing_or_unsafe_cessation_refuses(database, proof_change):
    request, _ = held(database)
    request["cessation"] |= proof_change
    with pytest.raises(ValueError):
        reconciliation.reconcile_held_job(**request)
    with Session(database) as db:
        assert not db.exec(select(RoutineReconciliation)).all()


@pytest.mark.parametrize("applied", [False, True])
def test_disposition_must_match_authoritative_provenance(database, applied):
    request, _ = held(database, applied=applied)
    request["disposition"] = "rearm" if applied else "retain_applied"
    with pytest.raises(ValueError):
        reconciliation.reconcile_held_job(**request)


def test_new_reserved_owner_blocks_and_rolls_back_adoption(database):
    request, _ = held(database)
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


def test_caller_transaction_rollback_reverts_audit_binding_job_and_permit(database):
    request, before = held(database)
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
        assert not db.exec(select(AgentCapacityReservation)).all()


def test_concurrent_identical_operator_requests_record_once(database):
    request, _ = held(database)
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda _: reconciliation.reconcile_held_job(**request), range(2))
        )
    assert results[0] == results[1]
    with Session(database) as db:
        assert len(db.exec(select(RoutineReconciliation)).all()) == 1
        assert len(db.exec(select(AgentCapacityReservation)).all()) == 1


@pytest.mark.parametrize(
    "unsafe", ["pending", "workflow", "kind", "shared_guest", "newer_session"]
)
def test_current_unsafe_state_cannot_be_authorized_by_refreshing_fingerprint(
    database, unsafe
):
    request, _ = held(database)
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


def test_failure_recording_audit_rolls_back_job_binding_and_settlement(
    database, monkeypatch
):
    request, before = held(database)
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
        assert not db.exec(select(AgentCapacityReservation)).all()


def test_old_park_stamp_cannot_prove_newer_unknown_attempt_ceased(database):
    request, _ = held(database)
    request["cessation"]["updated_at"] = request["cessation"]["last_invoke_at"] = 0
    with pytest.raises(ValueError, match="must follow"):
        reconciliation.reconcile_held_job(**request)


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
