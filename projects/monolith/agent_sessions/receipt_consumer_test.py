"""Hermetic native receipt consumption through the actual MCP executor."""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import BoundedSemaphore
import zlib

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from agent_sessions import (
    admission,
    execution_api,
    mcp,
    result_receipts,
    store,
    transport,
)
from agent_sessions.constants import UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from core import db as core_db


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'receipt-consumer.db'}",
        connect_args={"check_same_thread": False, "timeout": 3},
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
    for module in (admission, store, mcp, execution_api, result_receipts, core_db):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setenv("AGENT_RESULT_RECEIPTS_ENABLED", "true")
    monkeypatch.setenv("AGENT_RESULT_RECEIPT_ADOPTION_ENABLED", "true")
    monkeypatch.setattr(transport, "RECEIPT_POLL_SECONDS", 0.005)
    # Use the actual bounded executor with a fixture-owned lifetime. Cancelling
    # its asyncio waiter need not stop a running database thread, so join those
    # threads before disposing this test's database or restoring its bindings.
    observer_executor = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(transport, "_receipt_db_executor", observer_executor)
    monkeypatch.setattr(transport, "_receipt_db_slots", BoundedSemaphore(2))
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _sid: None)
    mcp._negative_oracle_verdicts.clear()

    async def notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mcp, "_notify_terminal", notify)
    try:
        yield engine
    finally:
        observer_executor.shutdown(wait=True, cancel_futures=True)
        mcp._negative_oracle_verdicts.clear()
        assert not transport._receipt_observers
        engine.dispose()


def queue(engine, key="receipt-project"):
    with Session(engine) as db:
        agent = store.create_session(
            db, key, "<guest>", "main", "luna", admission_tier="project"
        )
        sid = agent.id
        store.set_ember_session(db, sid, f"guest-{sid}", "guest-token", None)
        store.create_pending_message(db, sid, "implement the bounded task", "luna")
        return sid


def test_cleanup_rechecks_atomic_pending_to_receipt_handoff(database, monkeypatch):
    sid = queue(database)
    guest_id = f"guest-{sid}"
    assert store.claim_pending_message_for_session_sync(sid, "original-owner") == 1
    assert admission.recheck(sid, 1, "original-owner")
    receipt = result_receipts.prepare_receipt(
        sid, "original-owner", 1, guest_id, b'{"message":"original request"}'
    )
    with Session(database) as db:
        old_view = db.get(AgentSession, sid)
        assert old_view.result_receipt_fence_id is None
        assert store.guest_cleanup_hold(db, sid, guest_id) == "observer_pending"
    # The reaper's detached list predates the receipt writer's atomic handoff.
    with Session(database) as db, db.begin():
        agent = store._lock_session(db, sid)
        db.delete(store.get_pending_message(db, sid, 1))
        agent.result_receipt_fence_id = receipt["id"]
        db.add(agent)
    assert old_view.result_receipt_fence_id is None
    monkeypatch.setattr(execution_api, "_sessions_for_workflow", lambda _wf: [old_view])

    async def unexpected_destroy(_guest):
        pytest.fail("a stale list cannot authorize destroying a receipt-owned guest")

    monkeypatch.setattr(execution_api._transport, "destroy_session", unexpected_destroy)
    assert asyncio.run(execution_api.reap_sessions_for_workflow("completed-node")) == {
        "reaped": [],
        "pending": [sid],
        "failed": [],
        "skipped": [],
    }
    with Session(database) as db:
        with pytest.raises(store.PendingClaimLost, match="transport observer"):
            store.clear_ember_session(db, sid)
        db.rollback()
        assert store.clear_ember_bindings_by_ember_id(db, guest_id) == []
        assert db.get(AgentSession, sid).ember_session_id == guest_id
        assert db.get(AgentSession, sid).result_receipt_fence_id == receipt["id"]


@pytest.mark.parametrize("claimed", [False, True])
def test_flags_off_preserve_workflow_cleanup_without_a_receipt(
    database, monkeypatch, claimed
):
    monkeypatch.setenv("AGENT_RESULT_RECEIPTS_ENABLED", "false")
    monkeypatch.setenv("AGENT_RESULT_RECEIPT_ADOPTION_ENABLED", "false")
    sid = queue(database)
    if claimed:
        assert (
            store.claim_pending_message_for_session_sync(sid, "cancelled-workflow") == 1
        )
        assert admission.recheck(sid, 1, "cancelled-workflow")
    before = snapshot(database, sid)
    with Session(database) as db:
        row = db.get(AgentSession, sid)
        assert store.guest_cleanup_hold(db, sid, row.ember_session_id) is None
        assert db.exec(select(AgentResultReceipt)).all() == []
    destroyed = []

    async def destroy(guest_id):
        destroyed.append(guest_id)
        return {"state": "destroying"}

    async def observed(guest_id):
        return {"session_id": guest_id, "state": "destroyed"}

    monkeypatch.setattr(execution_api, "_sessions_for_workflow", lambda _wf: [row])
    monkeypatch.setattr(execution_api._transport, "destroy_session", destroy)
    monkeypatch.setattr(execution_api._transport, "get_session", observed)
    assert asyncio.run(
        execution_api.reap_sessions_for_workflow("cancelled-workflow")
    ) == {
        "reaped": [sid],
        "pending": [],
        "failed": [],
        "skipped": [],
    }
    after = snapshot(database, sid)
    assert destroyed == [f"guest-{sid}"]
    assert after["session"]["ember_session_id"] is None
    # Workflow cleanup has never settled model permits or rewritten history.
    assert after["pending"] == before["pending"]
    assert after["permits"] == before["permits"]
    assert after["turns"] == before["turns"]


def snapshot(engine, sid):
    with Session(engine) as db:
        return {
            "session": db.get(AgentSession, sid).model_dump(),
            "pending": [
                row.model_dump()
                for row in db.exec(
                    select(PendingMessage).where(PendingMessage.session_id == sid)
                ).all()
            ],
            "turns": [
                row.model_dump()
                for row in db.exec(
                    select(AgentTurn).where(AgentTurn.session_id == sid)
                ).all()
            ],
            "permits": [
                row.model_dump()
                for row in db.exec(
                    select(AgentCapacityReservation)
                    .where(AgentCapacityReservation.session_id == sid)
                    .order_by(AgentCapacityReservation.pending_seq)
                ).all()
            ],
        }


def native_record(cost=0.125):
    return {
        "result": "Implemented the bounded task.",
        "terminal_reason": "end_turn",
        "stop_reason": "end_turn",
        "is_error": False,
        "session_id": "native-cli",
        "num_turns": 1,
        "total_cost_usd": cost,
        "duration_ms": 23,
        "activities": [],
        "usage": {
            "input_tokens": 3,
            "native_result_receipt": {"receipt_id": "guest-forged", "seq": 99},
        },
        "diff": {
            "base_sha": "a" * 40,
            "truncated": False,
            "zlib_b64": base64.b64encode(zlib.compress(b"native diff bytes")).decode(),
        },
    }


def fake_http(monkeypatch, handler):
    requests = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, **kwargs):
            request = httpx.Request("POST", url, **kwargs)
            requests.append(request)
            assert request.url.path.endswith("/invoke"), "unexpected new guest"
            assert request.headers["X-Ember-Guest-Path"] == "/shim/turn"
            return await handler(request)

        async def delete(self, *args, **kwargs):
            pytest.fail("receipt consumption must not destroy its resident guest")

        async def get(self, *args, **kwargs):
            pytest.fail("receipt completion needs no control-plane absence check")

    monkeypatch.setattr(transport.httpx, "AsyncClient", Client)
    monkeypatch.setattr(transport, "EMBERVM_URL", "https://ember.test")
    monkeypatch.setattr(mcp, "_transport", transport.EmberVmShimTransport())
    return requests


async def publish(request, record):
    payload = json.loads(request.content)
    receipt = payload["result_receipt"]
    body = json.dumps(record).encode()
    await asyncio.to_thread(
        result_receipts.capture_result, receipt["id"], receipt["token"], body
    )
    return receipt, body


async def drain_observers():
    while transport._receipt_observers:
        await asyncio.gather(
            *list(transport._receipt_observers), return_exceptions=True
        )
        await asyncio.sleep(0)


@pytest.mark.parametrize("cost", [None, 0.125])
def test_receipt_completes_once_and_fences_only_its_guest_until_response(
    database, monkeypatch, cost
):
    sid = queue(database)
    record = native_record(cost)
    release_response = asyncio.Event()
    captured = {}

    async def handler(request):
        assert not captured, "the model POST must never be replayed"
        receipt, body = await publish(request, record)
        captured.update(receipt=receipt, body=body, request=request)
        await release_response.wait()
        return httpx.Response(200, json=record, request=request)

    requests = fake_http(monkeypatch, handler)

    async def run():
        try:
            await asyncio.wait_for(mcp._execute_pending_message(sid), 5)
            first = snapshot(database, sid)
            assert first["pending"] == []
            assert len(first["turns"]) == len(first["permits"]) == 1
            turn = first["turns"][0]
            assert turn["result_text"] == record["result"]
            assert turn["cost_usd"] == cost
            assert zlib.decompress(turn["diff_blob"]) == b"native diff bytes"
            assert turn["diff_base_sha"] == "a" * 40
            assert first["permits"][0]["state"] == "settled"
            assert first["permits"][0]["outcome"] == "end_turn"
            assert first["session"]["ember_session_id"] == f"guest-{sid}"
            assert first["session"]["cli_session_id"] == "native-cli"
            rid = captured["receipt"]["id"]
            assert first["session"]["result_receipt_fence_id"] == rid
            assert transport._receipt_observers
            provenance = json.loads(turn["usage_json"])["native_result_receipt"]
            assert provenance["receipt_id"] == rid
            assert provenance["seq"] == provenance["dispatch_count"] == 1
            assert provenance["session_id"] == sid
            assert provenance["guest_id"] == f"guest-{sid}"
            assert provenance["claim_owner"] == first["permits"][0]["owner"]
            assert (
                provenance["result_sha256"]
                == hashlib.sha256(captured["body"]).hexdigest()
            )
            unsigned_request = json.loads(captured["request"].content)
            unsigned_request.pop("result_receipt")
            assert (
                provenance["request_sha256"]
                == hashlib.sha256(json.dumps(unsigned_request).encode()).hexdigest()
            )
            assert "guest-forged" not in json.dumps(provenance)
            assert captured["receipt"]["token"] not in turn["usage_json"]

            with Session(database) as db:
                assert (
                    store.create_pending_message(db, sid, "follow-up", "luna").seq == 2
                )
            assert (
                store.claim_pending_message_for_session_sync(sid, "follow-up-owner")
                is None
            )
            independent = queue(database, "independent-project")
            assert (
                store.claim_pending_message_for_session_sync(
                    independent, "independent-owner"
                )
                == 1
            )
            assert admission.recheck(
                independent, 1, "independent-owner", "claude-runtime"
            )

            release_response.set()
            await asyncio.wait_for(drain_observers(), 3)
            after = snapshot(database, sid)
            assert after["turns"] == first["turns"]
            assert after["permits"] == first["permits"]
            assert after["session"]["result_receipt_fence_id"] is None
            assert after["session"]["ember_session_id"] == f"guest-{sid}"
            with Session(database) as db:
                assert db.get(AgentResultReceipt, rid).response_observed_at is not None
            assert (
                store.claim_pending_message_for_session_sync(sid, "follow-up-owner")
                == 2
            )
            assert admission.recheck(sid, 2, "follow-up-owner", "claude-runtime")
            assert len(requests) == 1
        finally:
            release_response.set()
            await asyncio.wait_for(drain_observers(), 3)

    asyncio.run(asyncio.wait_for(run(), 12))


@pytest.mark.parametrize(
    "response", ["disconnect", "retryable_503", "simultaneous_success"]
)
def test_committed_receipt_and_http_race_never_reinvoke_or_double_settle(
    database, monkeypatch, response
):
    sid = queue(database)
    record = native_record()
    captured = {}

    async def handler(request):
        assert not captured, "a captured native result must suppress retry"
        receipt, body = await publish(request, record)
        captured.update(receipt=receipt, body=body)
        if response == "disconnect":
            raise httpx.ReadError("native response connection lost", request=request)
        if response == "retryable_503":
            return httpx.Response(503, json={"retryable": True}, request=request)
        return httpx.Response(200, json=record, request=request)

    requests = fake_http(monkeypatch, handler)

    async def run():
        try:
            await asyncio.wait_for(mcp._execute_pending_message(sid), 5)
            await asyncio.wait_for(drain_observers(), 3)
            state = snapshot(database, sid)
            assert len(state["turns"]) == len(state["permits"]) == 1
            assert state["pending"] == []
            assert state["permits"][0]["state"] == "settled"
            assert state["permits"][0]["outcome"] == "end_turn"
            assert state["turns"][0]["result_text"] == record["result"]
            assert state["turns"][0]["cost_usd"] == record["total_cost_usd"]
            if response == "simultaneous_success":
                assert state["session"]["result_receipt_fence_id"] is None
                usage = json.loads(state["turns"][0]["usage_json"])
                assert (
                    usage.get("native_result_receipt", {}).get("receipt_id")
                    != "guest-forged"
                )
            else:
                assert (
                    state["session"]["result_receipt_fence_id"]
                    == captured["receipt"]["id"]
                )
            assert len(requests) == 1
        finally:
            await asyncio.wait_for(drain_observers(), 3)

    asyncio.run(asyncio.wait_for(run(), 10))


def test_executor_cancellation_before_capture_retains_unknown_and_late_history(
    database, monkeypatch
):
    sid = queue(database)
    posted = asyncio.Event()
    credentials = {}

    async def handler(request):
        credentials.update(json.loads(request.content)["result_receipt"])
        posted.set()
        await asyncio.Event().wait()
        pytest.fail("cancelled POST unexpectedly completed")

    requests = fake_http(monkeypatch, handler)

    async def run():
        executor = asyncio.create_task(mcp._execute_pending_message(sid))
        try:
            await asyncio.wait_for(posted.wait(), 3)
            executor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(executor, 3)
            before = snapshot(database, sid)
            assert before["pending"] == []
            assert before["turns"][0]["stop_reason"] == UNKNOWN_INVOCATION
            assert before["turns"][0]["cost_usd"] is None
            assert before["permits"][0]["state"] == "uncertain"
            assert before["session"]["ember_session_id"] == f"guest-{sid}"
            await asyncio.to_thread(
                result_receipts.capture_result,
                credentials["id"],
                credentials["token"],
                json.dumps(native_record()).encode(),
            )
            await asyncio.wait_for(drain_observers(), 3)
            assert snapshot(database, sid) == before
            with Session(database) as db:
                assert (
                    db.get(AgentResultReceipt, credentials["id"]).result_body
                    is not None
                )
            assert len(requests) == 1
        finally:
            executor.cancel()
            await asyncio.gather(executor, return_exceptions=True)
            await asyncio.wait_for(drain_observers(), 3)

    asyncio.run(asyncio.wait_for(run(), 10))


def test_stolen_dispatch_cannot_adopt_or_change_new_owner_state(database, monkeypatch):
    sid = queue(database)
    expected = {}
    credentials = {}

    async def handler(request):
        # Do not yield between the callback and the ownership change. The
        # observer may then see either snapshot, but persistence must see the
        # new owner and must leave its state alone.
        credentials.update(json.loads(request.content)["result_receipt"])
        result_receipts.capture_result(
            credentials["id"],
            credentials["token"],
            json.dumps(native_record()).encode(),
        )
        with Session(database) as db, db.begin():
            store._lock_session(db, sid)
            pending = store.get_pending_message(db, sid, 1)
            pending.claimed_by_replica = "new-executor"
            pending.dispatch_count += 1
            permit = db.exec(select(AgentCapacityReservation)).one()
            permit.owner = "new-executor"
            db.add_all([pending, permit])
        expected.update(snapshot(database, sid))
        await asyncio.Event().wait()
        pytest.fail("stolen POST should be cancelled locally")

    requests = fake_http(monkeypatch, handler)

    async def run():
        try:
            await asyncio.wait_for(mcp._execute_pending_message(sid), 5)
            await asyncio.wait_for(drain_observers(), 3)
            assert snapshot(database, sid) == expected
            with Session(database) as db:
                assert (
                    db.get(AgentResultReceipt, credentials["id"]).result_body
                    is not None
                )
            assert len(requests) == 1
        finally:
            for observer in list(transport._receipt_observers):
                observer.cancel()
            await asyncio.wait_for(drain_observers(), 3)

    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.parametrize("tamper", ["cost", "diff", "provenance"])
def test_real_writer_rejects_tampered_receipt_turn_and_rolls_back_fence(
    database, monkeypatch, tamper
):
    sid = queue(database)
    record = native_record()
    release_response = asyncio.Event()
    credentials = {}
    writer_states = {}
    persist = mcp._persist_turn_from_pending_sync

    def altered(*args, **kwargs):
        args = list(args)
        turn = args[3]
        assert turn.native_receipt is not None
        writer_states["before"] = snapshot(database, sid)
        if tamper == "cost":
            args[3] = turn._replace(total_cost_usd=99.0)
        elif tamper == "diff":
            args[3] = turn._replace(diff=None)
        else:
            args[3] = turn._replace(native_receipt={**turn.native_receipt, "seq": 99})
        with pytest.raises(store.PendingClaimLost):
            persist(*args, **kwargs)
        writer_states["after"] = snapshot(database, sid)
        raise store.PendingClaimLost("test confirms transaction refusal")

    monkeypatch.setattr(mcp, "_persist_turn_from_pending_sync", altered)

    async def handler(request):
        receipt, _body = await publish(request, record)
        credentials.update(receipt)
        await release_response.wait()
        return httpx.Response(200, json=record, request=request)

    requests = fake_http(monkeypatch, handler)

    async def run():
        try:
            await asyncio.wait_for(mcp._execute_pending_message(sid), 5)
            assert writer_states["after"] == writer_states["before"]
            refused = writer_states["after"]
            assert refused["turns"] == []
            assert refused["permits"][0]["state"] == "running"
            assert refused["session"]["result_receipt_fence_id"] is None
            final = snapshot(database, sid)
            assert final["turns"][0]["stop_reason"] == UNKNOWN_INVOCATION
            assert final["permits"][0]["state"] == "uncertain"
            assert final["session"]["result_receipt_fence_id"] is None
            release_response.set()
            await asyncio.wait_for(drain_observers(), 3)
            assert snapshot(database, sid) == final
            with Session(database) as db:
                receipt = db.get(AgentResultReceipt, credentials["id"])
                assert receipt.result_body == json.dumps(record).encode()
            assert len(requests) == 1
        finally:
            release_response.set()
            await asyncio.wait_for(drain_observers(), 3)

    asyncio.run(asyncio.wait_for(run(), 10))
