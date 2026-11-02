"""Recovery refuses a busy session without inverting execution's lock order."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine, select

from factory.execution.models import AgentSession
from factory.orchestration import (
    evidence_continuation,
    factory_controls as controls,
    graph,
)
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
)
from factory.orchestration.models import SwarmTask


def test_task_then_nowait_session_does_not_deadlock_result_writer(pg, monkeypatch):
    engine = create_engine(pg.url)
    task_id = "evidence-lock-" + uuid4().hex
    with Session(engine) as db, db.begin():
        if db.get(FactoryControl, "factory") is None:
            db.add(FactoryControl(id="factory", actor="test"))
        db.add(
            SwarmTask(
                id=task_id,
                task_text="lock ordering",
                conductor_model="astra",
                budget_usd=50,
            )
        )
        db.flush()
        db.add(
            FactoryReceipt(
                repo="test/evidence",
                issue_number=1,
                generation=0,
                title="lock ordering",
                url="https://github.com/test/evidence/issues/1",
                body="test",
                actor="operator",
                state="escalated",
                task_id=task_id,
            )
        )
        session = AgentSession(
            local_session_id=f"factory:{task_id}:correct_1:1",
            workspace="/workspace/src",
            branch=f"factory/{task_id}",
            status="completed",
        )
        db.add(session)
        db.flush()
        session_id = session.id
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    session_locked, task_locked, writer_finished = Event(), Event(), Event()

    def result_writer():
        with Session(engine) as db, db.begin():
            db.exec(
                select(AgentSession)
                .where(AgentSession.id == session_id)
                .with_for_update()
            ).one()
            session_locked.set()
            assert task_locked.wait(5)
            graph._lock_task(db, task_id)
        writer_finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(result_writer)
        assert session_locked.wait(5)
        with controls._locked_session() as (db, _control):
            graph._lock_task(db, task_id)
            task_locked.set()
            with pytest.raises(ValueError, match="session_busy"):
                evidence_continuation.quiescence(db, task_id)
            # The savepoint, not the caller's reservation transaction, failed.
            assert db.execute(text("SELECT 1")).scalar_one() == 1
        assert writer_finished.wait(5)
        writer.result(timeout=5)
    engine.dispose()


def test_recovery_grant_claims_serialize_and_revocation_fences_admission(
    pg, monkeypatch
):
    from datetime import timedelta
    from factory.orchestration import recovery_delegation as delegation
    from core.jsonio import dumps

    engine = create_engine(pg.url)
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setenv("FACTORY_RECOVERY_DELEGATION_ENABLED", "true")
    # The owner result is absent; this test exercises real ledger queries and
    # locks, while SQLite owner tests exercise both complete recovery paths.
    monkeypatch.setattr(delegation, "_owner_result", lambda *_: None)
    owner = {
        "receipt_id": 1,
        "session_id": 2,
        "permit_id": 3,
        "actor": delegation.ACTOR,
        "request_key": "pg-" + uuid4().hex,
    }
    saved = {
        "request": {
            "target": {"kind": "settled_retry"},
            "expires_at": (controls._now() + timedelta(minutes=10)).isoformat(),
        },
        "owner_request": owner,
        "owner_preview_sha256": "a" * 64,
    }
    with controls._locked_session() as (db, control):
        control.state = "enabled"
        row = FactoryAudit(
            actor="operator", action=delegation.GRANT, detail_json=dumps(saved)
        )
        db.add(row)
        db.flush()
        grant_id = row.id
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: delegation._claim(), range(2)))
    assert sum(claim is not None for claim in claims) == 1
    token, _ = next(claim for claim in claims if claim is not None)
    assert token["grant_id"] == grant_id
    with controls._locked_session() as (db, _):
        assert delegation._state(db, grant_id)["calls"] == 1
        controls._audit(db, "operator", delegation.REVOKE, grant_id=grant_id)
    with controls._locked_session() as (db, _):
        with pytest.raises(ValueError, match="disabled_or_revoked"):
            delegation.guard(
                db, delegation.ACTOR, "settled_retry", owner, "a" * 64, token
            )
        assert not db.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == delegation.ADMIT,
                delegation._field(db, "grant_id", integer=True) == grant_id,
            )
        ).all()
        grant = db.get(FactoryAudit, grant_id)
        # Cleanup only this fixture's ledger; live uncertainty is not expired.
        delegation._record(db, grant, "refused", calls=1)
    engine.dispose()
