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
from factory.orchestration.factory_models import FactoryControl, FactoryReceipt
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
