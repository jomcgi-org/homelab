"""Probe cadence uses real observations and survives process boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from factory import quota_probe as probe
from factory.execution.models import (
    AgentSession,
    AgentCapacityReservation,
    PendingMessage,
    AgentTurn,
    AgentResultReceipt,
)
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
)
from factory.orchestration.models import SwarmTask

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'probe.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={
            "schema_translate_map": {"swarm": None, "agent_sessions": None}
        },
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                SwarmTask,
                FactoryControl,
                FactoryReceipt,
                WorkItem,
                FactoryAudit,
                AgentSession,
                AgentCapacityReservation,
                PendingMessage,
                AgentTurn,
                AgentResultReceipt,
            )
        ],
    )
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="test"))
        session.commit()
    monkeypatch.setattr(probe.controls, "get_engine", lambda: engine)
    monkeypatch.setattr(probe.controls, "_now", lambda: NOW)
    monkeypatch.setattr(
        FactoryAudit.model_fields["created_at"], "default_factory", lambda: NOW
    )
    yield engine
    engine.dispose()


def payload(age=0, observed=True):
    return {
        "available": True,
        "providers": {
            "claude": {
                "observed": observed,
                "age_seconds": age,
                "windows": [{"name": "7d", "used_percent": 11}],
            }
        },
    }


def active(db, paused=False):
    with Session(db) as session:
        session.add(
            FactoryReceipt(
                repo="test",
                issue_number=1,
                title="test",
                body="",
                url="test",
                actor="test",
                state="admitted",
                task_paused=paused,
            )
        )
        session.commit()


@pytest.mark.parametrize(
    "busy,age,expected",
    [(False, 3599, False), (False, 3600, True), (True, 899, False), (True, 900, True)],
)
def test_cadence(db, busy, age, expected):
    if busy:
        active(db)
    assert bool(probe.claim(payload(age))) is expected


def test_paused_work_uses_hourly_cadence(db):
    active(db, paused=True)
    assert probe.claim(payload(900)) is None


@pytest.mark.parametrize("age", [None, True, -1, float("nan"), float("inf"), "10"])
def test_invalid_age_cannot_suppress_probe(db, age):
    assert probe.claim(payload(age))


def test_missing_observation_probes_but_broken_broker_does_not(db):
    assert probe.claim({"available": False}) is None
    assert probe.claim(payload(observed=False))


def test_known_expired_window_does_not_count_as_fresh(db):
    reading = payload()
    reading["providers"]["claude"]["windows"][0]["expired"] = True
    assert probe.claim(reading)


def test_persistent_attempt_limits_retry_even_without_observation(db, monkeypatch):
    assert probe.claim(payload(observed=False))
    monkeypatch.setattr(probe.controls, "_now", lambda: NOW + timedelta(seconds=3599))
    assert probe.claim(payload(observed=False)) is None
    monkeypatch.setattr(probe.controls, "_now", lambda: NOW + timedelta(seconds=3600))
    assert probe.claim(payload(observed=False))


def test_concurrent_replicas_reserve_only_once(db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        keys = list(pool.map(lambda _: probe.claim(payload(observed=False)), range(2)))
    assert sum(key is not None for key in keys) == 1


@pytest.mark.parametrize(
    "status,ember,pending",
    [("running", None, False), ("warning", "guest", False), ("warning", None, True)],
)
def test_unresolved_guest_blocks_new_probe(db, status, ember, pending):
    with Session(db) as session:
        row = AgentSession(
            local_session_id=probe.PREFIX + "old",
            workspace="<guest>",
            branch="main",
            status=status,
            ember_session_id=ember,
        )
        session.add(row)
        session.flush()
        if pending:
            session.add(PendingMessage(session_id=row.id, seq=1, message_text="OK"))
        session.commit()
    assert probe.claim(payload(observed=False)) is None


def test_completed_guest_does_not_block(db):
    with Session(db) as session:
        session.add(
            AgentSession(
                local_session_id=probe.PREFIX + "old",
                workspace="<guest>",
                branch="main",
                status="idle",
            )
        )
        session.commit()
    assert probe.claim(payload(observed=False))


@pytest.mark.parametrize("outcome", ["observed", "no_observation", "failed"])
def test_probe_requires_broker_evidence_not_model_reply(db, monkeypatch, outcome):
    from factory.execution import execution_api, provider_quota

    calls = []

    async def fetch(**kwargs):
        return payload(observed=bool(calls) and outcome == "observed")

    async def run(prompt, **kwargs):
        calls.append((prompt, kwargs))
        if outcome == "failed":
            raise RuntimeError("failed")
        return "quota is zero"  # Never trust model prose as telemetry.

    async def sleep(_):
        pass

    monkeypatch.setattr(provider_quota, "fetch_provider_quota", fetch)
    monkeypatch.setattr(execution_api, "run_synthetic_session", run)
    monkeypatch.setattr(probe.asyncio, "sleep", sleep)
    asyncio.run(probe.tick())
    assert len(calls) == 1
    assert calls[0][1]["model"] == "haiku"
    assert calls[0][1]["read_timeout"] == 120
    assert calls[0][1]["session_key"].startswith(probe.PREFIX)
    with Session(db) as session:
        actions = session.exec(
            select(FactoryAudit.action).order_by(FactoryAudit.id)
        ).all()
    assert actions == ["quota_probe_started", "quota_probe_" + outcome]


def test_total_deadline_cancels_stalled_creation(db, monkeypatch):
    from factory.execution import execution_api, provider_quota

    cancelled = []

    async def fetch(**kwargs):
        return payload(observed=False)

    async def run(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(provider_quota, "fetch_provider_quota", fetch)
    monkeypatch.setattr(execution_api, "run_synthetic_session", run)
    monkeypatch.setattr(probe, "TURN_TIMEOUT_SECONDS", 0.01)
    asyncio.run(probe.tick())
    assert cancelled == [True]
    with Session(db) as session:
        assert session.exec(
            select(FactoryAudit.action).order_by(FactoryAudit.id)
        ).all() == [
            "quota_probe_started",
            "quota_probe_failed",
        ]
    assert probe.claim(payload(observed=False)) is None


@pytest.mark.parametrize("prefix", [probe.PREFIX, "synthetic:factory-review:"])
def test_unconfirmed_cleanup_blocks_until_exact_guest_confirmed(
    db, monkeypatch, prefix
):
    from factory.execution import execution_api, mcp

    with Session(db) as session:
        session.add(
            AgentSession(
                local_session_id=prefix + "old",
                workspace="<guest>",
                branch="main",
                status="idle",
                ember_session_id="guest",
            )
        )
        session.commit()
    confirmations = [False, True]
    deleted = []

    async def destroy(guest):
        assert guest == "guest"
        return confirmations.pop(0)

    def clear(guest):
        deleted.append(guest)
        with Session(db) as session:
            row = session.exec(select(AgentSession)).one()
            row.ember_session_id = None
            session.add(row)
            session.commit()

    monkeypatch.setattr(execution_api, "destroy_and_confirm", destroy)
    monkeypatch.setattr(mcp, "_clear_ember_bindings_for", clear)
    asyncio.run(probe._cleanup())
    assert deleted == []
    if prefix == probe.PREFIX:
        assert probe.claim(payload(observed=False)) is None
    asyncio.run(probe._cleanup())
    assert deleted == ["guest"]
    assert probe.claim(payload(observed=False))
