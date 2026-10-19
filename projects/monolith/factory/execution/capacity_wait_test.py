import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from factory.execution import admission, capacity_wait, store, transport
from factory.execution.constants import exact_dispatch_id
from factory.execution.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
    PendingMessage,
)


@pytest.fixture
def database(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'capacity.db'}",
        connect_args={"check_same_thread": False},
        execution_options={"schema_translate_map": {"agent_sessions": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                AgentCapacityPool,
                AgentCapacityReservation,
                AgentResultReceipt,
                AgentSession,
                AgentTurn,
                PendingMessage,
            )
        ],
    )
    for module in (capacity_wait, store, admission):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setenv("FACTORY_ASYNC_RESIDENCY_ENABLED", "true")
    deadline = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp() * 1000)
    monkeypatch.setattr(capacity_wait, "_task_deadline", lambda _: deadline)
    with Session(engine) as db:
        agent = store.create_session(
            db, "factory:t-task:implement:1", "guest", "branch", "luna"
        )
        agent.ember_session_id = "guest"
        agent.ember_session_token = "secret-capability"
        agent.ember_lineage_id = "lineage"
        agent.ember_session_expires_at = deadline
        db.add(agent)
        db.commit()
        store.create_pending_message(db, agent.id, "private prompt", "luna")
        sid = agent.id
    yield engine, sid, deadline
    engine.dispose()


def pending(engine):
    with Session(engine) as db:
        row = db.exec(select(PendingMessage)).one()
        db.expunge(row)
        return row


def allow_poll(engine):
    with Session(engine) as db:
        row = db.exec(select(PendingMessage)).one()
        row.capacity_next_poll_at = None
        db.add(row)
        db.commit()


class FakeEmber:
    def __init__(self, engine, deadline):
        self.engine = engine
        self.deadline = deadline
        self.calls = []
        self.state = "queued"
        self.generation = 3
        self.reason = "capacity"
        self.lose_enqueue = False
        self.unsupported = False

    async def get_session(self, guest, token):
        assert guest == "guest" and token == "secret-capability"
        return {
            "session_id": guest,
            "lineage_id": "lineage",
            "generation": self.generation,
            "expires_at": self.deadline,
        }

    async def residency_request(self, guest, token, method, **kwargs):
        row = pending(self.engine)
        context = json.loads(row.capacity_intent_json)
        assert row.claimed_by_replica is None
        assert row.dispatch_count == 0
        assert row.last_dispatch_at is None
        assert token not in row.capacity_intent_json
        assert row.message_text not in row.capacity_intent_json
        self.calls.append((method, kwargs))
        if method == "POST":
            assert context["key"] == kwargs["key"]
            assert context["enqueue_attempted"]
        if self.lose_enqueue:
            self.lose_enqueue = False
            raise httpx.ReadTimeout("lost enqueue acknowledgement")
        if self.unsupported:
            response = httpx.Response(
                501,
                json={"error": "async_residency_unsupported"},
                request=httpx.Request(method, "https://ember.test"),
            )
            raise httpx.HTTPStatusError(
                "unsupported", request=response.request, response=response
            )
        return {
            "id": "op-" + context["key"],
            "key": context["key"],
            "session_id": guest,
            "generation": context["generation"],
            "state": self.state,
            "reason": self.reason,
            "residency_epoch": "epoch" if self.state == "ready" else None,
        }


def test_queue_wait_survives_reconstruction_without_attempt_or_receipt(database):
    engine, sid, deadline = database
    ember = FakeEmber(engine, deadline)
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    original = json.loads(pending(engine).capacity_intent_json)
    # A fresh poller reads only the DB, with no process-local queue or caller.
    ember = FakeEmber(engine, deadline)
    allow_poll(engine)
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    row = pending(engine)
    current = json.loads(row.capacity_intent_json)
    assert current["key"] == original["key"]
    assert current["operation_id"] == original["operation_id"]
    assert current["first_wait_at"] == original["first_wait_at"]
    assert current["deadline"] == deadline
    assert row.claimed_by_replica is None and row.dispatch_count == 0
    assert store.claim_pending_message_for_session_sync(sid, "premature") is None
    with Session(engine) as db:
        assert not db.exec(select(AgentResultReceipt)).all()
        assert not db.exec(select(AgentTurn)).all()
    ember.state, ember.reason = "ready", "resident"
    allow_poll(engine)
    assert asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    assert store.claim_pending_message_for_session_sync(sid, "owner") == 1


def test_lost_enqueue_replays_persisted_key_and_forbids_legacy_fallback(database):
    engine, sid, deadline = database
    ember = FakeEmber(engine, deadline)
    ember.lose_enqueue = True
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    first_key = ember.calls[0][1]["key"]
    allow_poll(engine)
    ember.unsupported = True
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    assert ember.calls[1][1]["key"] == first_key
    assert pending(engine).capacity_state == "waiting_capacity"
    ember.unsupported = False
    allow_poll(engine)
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    assert (
        json.loads(pending(engine).capacity_intent_json)["operation_id"]
        == "op-" + first_key
    )


def test_first_definitive_unsupported_response_is_only_legacy_escape(database):
    engine, sid, deadline = database
    ember = FakeEmber(engine, deadline)
    ember.unsupported = True
    assert asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    assert pending(engine).capacity_state == "legacy_unsupported"


def test_disabling_producer_keeps_accepted_operation_on_async_path(
    database, monkeypatch
):
    engine, sid, deadline = database
    ember = FakeEmber(engine, deadline)
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    monkeypatch.setenv("FACTORY_ASYNC_RESIDENCY_ENABLED", "false")
    allow_poll(engine)
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    assert ember.calls[-1][0] == "GET"
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=False))
    assert ember.calls[-1][0] == "DELETE"


def test_stale_generation_mints_new_key_preserving_pending_identity_and_age(database):
    engine, sid, deadline = database
    ember = FakeEmber(engine, deadline)
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    original = json.loads(pending(engine).capacity_intent_json)
    ember.state, ember.reason = "failed", "stale_generation"
    allow_poll(engine)
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    ember.generation, ember.state, ember.reason = 4, "queued", "capacity"
    allow_poll(engine)
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    current = json.loads(pending(engine).capacity_intent_json)
    assert current["generation"] == 4 and current["key"] != original["key"]
    for field in ("first_wait_at", "deadline", "task_deadline"):
        assert current[field] == original[field]


def test_exact_negative_proof_releases_only_provisional_dispatch(database):
    engine, sid, deadline = database
    ember = FakeEmber(engine, deadline)
    ember.state = "ready"
    assert asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    assert store.claim_pending_message_for_session_sync(sid, "owner") == 1
    row = pending(engine)
    context = json.loads(row.capacity_intent_json)
    proof = {
        "outcome": "not_dispatched",
        "operation_id": context["operation_id"],
        "session_id": "guest",
        "dispatch_id": exact_dispatch_id(sid, "guest", 1, "owner", row.dispatch_count),
        "reason": "residency_changed",
    }
    assert not capacity_wait.resolve_not_dispatched(
        sid, 1, "owner", 1, {**proof, "dispatch_id": "wrong"}, None
    )
    assert capacity_wait.resolve_not_dispatched(sid, 1, "owner", 1, proof, None)
    row = pending(engine)
    assert (
        row.claimed_by_replica is None
        and row.dispatch_count == 0
        and row.last_dispatch_at is None
    )
    assert (
        row.message_text == "private prompt"
        and row.capacity_state == "waiting_capacity"
    )
    with Session(engine) as db:
        permit = db.exec(select(AgentCapacityReservation)).one()
        assert permit.state == "reserved" and permit.owner is None
        assert not db.exec(select(AgentTurn)).all()
    assert not capacity_wait.resolve_not_dispatched(sid, 1, "owner", 1, proof, None)


def test_handoff_clock_needs_exact_consumed_evidence(database, monkeypatch):
    engine, sid, deadline = database
    ember = FakeEmber(engine, deadline)
    ember.state = "ready"
    assert asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    assert store.claim_pending_message_for_session_sync(sid, "owner") == 1
    context = json.loads(pending(engine).capacity_intent_json)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    response = {
        "id": context["operation_id"],
        "session_id": "guest",
        "state": "ready",
        "updated_at": now,
    }
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: httpx.Response(
            200, json=response, request=httpx.Request("GET", "https://ember.test")
        ),
    )
    capacity_wait.observe_handoff(sid)
    assert pending(engine).capacity_started_at is None
    response.update(state="consumed", dispatch_id="wrong")
    capacity_wait.observe_handoff(sid)
    assert pending(engine).capacity_started_at is None
    response["dispatch_id"] = exact_dispatch_id(sid, "guest", 1, "owner", 1)
    capacity_wait.observe_handoff(sid)
    assert pending(engine).capacity_started_at is not None


def test_capacity_handoff_clock_excludes_queue_time_from_hung_alarm(database):
    engine, sid, deadline = database
    ember = FakeEmber(engine, deadline)
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=3)
    with Session(engine) as db:
        row = db.exec(select(PendingMessage)).one()
        row.claimed_by_replica = "worker"
        row.claimed_at = now
        row.last_dispatch_at = now - timedelta(minutes=10)
        row.dispatch_count = 1
        db.add(row)
        db.commit()
        assert store.find_hung_claim_session_ids(db, cutoff, now) == []
        row.capacity_started_at = now - timedelta(minutes=1)
        db.add(row)
        db.commit()
        assert store.find_hung_claim_session_ids(db, cutoff, now) == []
        row.capacity_started_at = now - timedelta(minutes=4)
        db.add(row)
        db.commit()
        assert store.find_hung_claim_session_ids(db, cutoff, now) == [sid]


def test_pause_after_lost_enqueue_reattaches_by_read_and_cancels_only_interest(
    database,
):
    engine, sid, deadline = database
    ember = FakeEmber(engine, deadline)
    ember.lose_enqueue = True
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=True))
    assert not asyncio.run(capacity_wait.poll(sid, ember, allowed=False))
    assert [method for method, _ in ember.calls] == ["POST", "GET", "DELETE"]
    row = pending(engine)
    assert row.dispatch_count == 0 and row.claimed_by_replica is None
    assert json.loads(row.capacity_intent_json)["operation_id"]
