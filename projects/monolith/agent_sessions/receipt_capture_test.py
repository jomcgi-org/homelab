"""Capture-only contracts through the real claimed-message executor and writer."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import zlib

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from agent_sessions import admission, mcp, result_receipts, store, transport
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
        f"sqlite:///{tmp_path / 'receipt-capture.db'}",
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
    for module in (admission, mcp, result_receipts, store, core_db):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setenv("AGENT_RESULT_RECEIPTS_ENABLED", "true")
    monkeypatch.setenv("AGENT_RESULT_RECEIPT_ADOPTION_ENABLED", "false")
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _sid: None)
    mcp._negative_oracle_verdicts.clear()

    async def notify(*_args, **_kwargs):
        return None

    async def unexpected_adoption(*_args, **_kwargs):
        pytest.fail("capture-only delivery cannot start a receipt observer")

    monkeypatch.setattr(mcp, "_notify_terminal", notify)
    monkeypatch.setattr(transport, "_observe_native_result", unexpected_adoption)
    try:
        yield engine
    finally:
        mcp._negative_oracle_verdicts.clear()
        engine.dispose()


def queue(engine, *, bound=True):
    with Session(engine) as db:
        agent = store.create_session(
            db, "capture-project", "<guest>", "main", "luna", admission_tier="project"
        )
        sid = agent.id
        if bound:
            store.set_ember_session(db, sid, f"guest-{sid}", "guest-token", None)
        store.create_pending_message(db, sid, "implement the bounded task", "luna")
        return sid


def snapshot(engine, sid):
    with Session(engine) as db:
        return {
            "session": db.get(AgentSession, sid).model_dump(),
            **{
                key: [
                    row.model_dump()
                    for row in db.exec(
                        select(model).where(model.session_id == sid)
                    ).all()
                ]
                for key, model in (
                    ("pending", PendingMessage),
                    ("turns", AgentTurn),
                    ("permits", AgentCapacityReservation),
                    ("receipts", AgentResultReceipt),
                )
            },
        }


def native_record():
    return {
        "result": "Implemented the bounded task.",
        "terminal_reason": "end_turn",
        "stop_reason": "end_turn",
        "is_error": False,
        "session_id": "native-cli",
        "num_turns": 1,
        "total_cost_usd": 0.125,
        "duration_ms": 23,
        "activities": [],
        "usage": {"input_tokens": 3},
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
            return await handler(request)

        async def get(self, *_args, **_kwargs):
            pytest.fail("capture-only delivery requires no guest-state observation")

        async def delete(self, *_args, **_kwargs):
            pytest.fail("capture-only delivery must preserve the guest")

    monkeypatch.setattr(transport.httpx, "AsyncClient", Client)
    monkeypatch.setattr(transport, "EMBERVM_URL", "https://ember.test")
    monkeypatch.setattr(mcp, "_transport", transport.EmberVmShimTransport())
    return requests


async def publish(request, record):
    receipt = json.loads(request.content)["result_receipt"]
    # The native callback uses compact JSON; the synchronous response does not.
    body = json.dumps(record, separators=(",", ":")).encode()
    ack = await asyncio.to_thread(
        result_receipts.capture_result, receipt["id"], receipt["token"], body
    )
    assert ack == {
        "receipt_id": receipt["id"],
        "result_sha256": hashlib.sha256(body).hexdigest(),
    }
    return receipt, body


class HeartbeatClock:
    """Advance the database clock only when the actual heartbeat is asleep."""

    def __init__(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.sleepers = asyncio.Queue()

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, seconds):
        assert seconds == 10
        wake = asyncio.Event()
        await self.sleepers.put(wake)
        await wake.wait()

    def install(self, engine, monkeypatch):
        def connect(connection, _record):
            connection.create_function(
                "current_timestamp", 0, lambda: self.now.strftime("%Y-%m-%d %H:%M:%S")
            )

        # Claim and renewal use SQL CURRENT_TIMESTAMP, not Python datetime.now.
        # Reopen the file-backed pool so every connection sees the same clock.
        engine.dispose()
        event.listen(engine, "connect", connect)
        clock = self

        class ControlledDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return (
                    clock.now.astimezone(tz) if tz else clock.now.replace(tzinfo=None)
                )

        monkeypatch.setattr(store, "datetime", ControlledDatetime)
        monkeypatch.setattr(result_receipts, "_now", lambda: self.now)
        # Replace only this module's asyncio reference, not global asyncio.sleep.
        monkeypatch.setattr(mcp, "asyncio", self)


def test_slow_boot_keeps_actual_executor_lease_alive_before_receipt_mint(
    database, monkeypatch
):
    sid = queue(database, bound=False)
    clock = HeartbeatClock()
    clock.install(database, monkeypatch)
    boot_started = asyncio.Event()
    boot_ready = asyncio.Event()
    captured = {}

    async def handler(request):
        if request.url.path.endswith("/sessions"):
            boot_started.set()
            await boot_ready.wait()
            return httpx.Response(
                200,
                json={"session_id": f"guest-{sid}", "session_token": "guest-token"},
                request=request,
            )
        assert request.url.path.endswith("/invoke")
        captured["receipt"], _body = await publish(request, native_record())
        return httpx.Response(200, json=native_record(), request=request)

    requests = fake_http(monkeypatch, handler)

    async def run():
        executor = asyncio.create_task(mcp._execute_pending_message(sid))
        try:
            await boot_started.wait()
            first = snapshot(database, sid)
            assert first["receipts"] == []
            first_claim = first["pending"][0]["claimed_at"]
            wake = await clock.sleepers.get()
            for _ in range(4):
                clock.now += timedelta(seconds=10)
                wake.set()
                # The next sleep starts only after the real DB refresh returns.
                wake = await clock.sleepers.get()
                pending = snapshot(database, sid)["pending"][0]
                assert pending["claimed_at"] == clock.now.replace(tzinfo=None)
                assert (
                    pending["claimed_by_replica"]
                    == first["pending"][0]["claimed_by_replica"]
                )
                assert (
                    pending["last_dispatch_at"]
                    == first["pending"][0]["last_dispatch_at"]
                )
            assert (clock.now.replace(tzinfo=None) - first_claim).total_seconds() == 40
            assert await asyncio.to_thread(store.reclaim_stale_claims_sync) == 0
            assert snapshot(database, sid)["receipts"] == []
            boot_ready.set()
            await executor
        finally:
            boot_ready.set()
            executor.cancel()
            await asyncio.gather(executor, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 5))
    after = snapshot(database, sid)
    assert len(requests) == 2
    assert len(after["turns"]) == len(after["receipts"]) == 1
    assert after["pending"] == []
    assert after["permits"][0]["state"] == "settled"
    assert after["turns"][0]["terminal_reason"] == "end_turn"
    assert after["receipts"][0]["id"] == captured["receipt"]["id"]


def test_mint_storage_failure_before_post_settles_typed_never_invoked(
    database, monkeypatch
):
    sid = queue(database)
    mint_failures = []
    typed_failures = []

    def fail_receipt_insert(_conn, _cursor, _statement, _parameters, context, _many):
        statement = context.compiled.statement if context.compiled is not None else None
        if (
            getattr(statement, "is_insert", False)
            and statement.table.name == "result_receipts"
        ):
            mint_failures.append(True)
            raise sqlite3.OperationalError("receipt storage unavailable")

    event.listen(database, "before_cursor_execute", fail_receipt_insert)

    async def unexpected_post(_request):
        pytest.fail("a failed receipt mint must not reach a physical POST")

    requests = fake_http(monkeypatch, unexpected_post)
    deliver = mcp._transport.deliver

    async def observe_typed_failure(*args, **kwargs):
        try:
            return await deliver(*args, **kwargs)
        except transport.EmberTurnNotInvoked as exc:
            typed_failures.append(type(exc))
            raise

    monkeypatch.setattr(mcp._transport, "deliver", observe_typed_failure)
    asyncio.run(asyncio.wait_for(mcp._execute_pending_message(sid), 5))
    after = snapshot(database, sid)
    assert mint_failures == [True]
    assert typed_failures == [transport.EmberTurnNotInvoked]
    assert requests == []
    assert after["receipts"] == after["pending"] == []
    assert len(after["turns"]) == len(after["permits"]) == 1
    turn = after["turns"][0]
    assert turn["terminal_reason"] == "error"
    assert turn["stop_reason"] != UNKNOWN_INVOCATION
    assert turn["cost_usd"] is None
    assert (
        json.loads(turn["usage_json"])["recovery"]["invocation_phase"] == "not_invoked"
    )
    assert after["permits"][0]["state"] == "settled"
    assert after["permits"][0]["outcome"] == "not_invoked"
    assert after["session"]["ember_session_id"] == f"guest-{sid}"
    assert after["session"]["result_receipt_fence_id"] is None


def test_owner_stolen_at_mint_denies_post_without_overwriting_new_owner(
    database, monkeypatch
):
    sid = queue(database)
    expected = {}
    denials = []
    prepare_receipt = result_receipts.prepare_receipt

    def steal_then_mint(*args, **kwargs):
        with Session(database) as db, db.begin():
            admission.lock_pool(db)
            store._lock_session(db, sid)
            pending = store.get_pending_message(db, sid, 1)
            pending.claimed_by_replica = "new-executor"
            pending.claimed_at = datetime.now(timezone.utc)
            pending.dispatch_count += 1
            permit = db.exec(select(AgentCapacityReservation)).one()
            permit.owner = "new-executor"
            db.add_all([pending, permit])
        expected.update(snapshot(database, sid))
        try:
            return prepare_receipt(*args, **kwargs)
        except result_receipts.ReceiptRejected as exc:
            denials.append((exc.status, str(exc)))
            raise

    monkeypatch.setattr(result_receipts, "prepare_receipt", steal_then_mint)

    async def unexpected_post(_request):
        pytest.fail("a stolen owner cannot dispatch after receipt mint denial")

    requests = fake_http(monkeypatch, unexpected_post)
    asyncio.run(asyncio.wait_for(mcp._execute_pending_message(sid), 5))
    assert denials == [(409, "executor_ownership_changed")]
    assert requests == []
    assert snapshot(database, sid) == expected
    assert expected["receipts"] == expected["turns"] == []
    assert expected["pending"][0]["claimed_by_replica"] == "new-executor"
    assert expected["permits"][0]["state"] == "running"


def test_capture_only_waits_for_native_response_and_preserves_compact_receipt(
    database, monkeypatch
):
    sid = queue(database)
    record = native_record()
    captured = {}
    callback_committed = asyncio.Event()
    response_ready = asyncio.Event()

    async def handler(request):
        assert request.url.path.endswith("/invoke")
        assert request.headers["X-Ember-Guest-Path"] == "/shim/turn"
        assert not captured, "the native invocation must remain unique"
        receipt, body = await publish(request, record)
        captured.update(receipt=receipt, body=body, request=request)
        callback_committed.set()
        await response_ready.wait()
        return httpx.Response(
            200,
            content=json.dumps(record).encode(),
            headers={"Content-Type": "application/json"},
            request=request,
        )

    requests = fake_http(monkeypatch, handler)

    async def run():
        executor = asyncio.create_task(mcp._execute_pending_message(sid))
        try:
            await callback_committed.wait()
            before_response = snapshot(database, sid)
            assert not executor.done()
            assert before_response["turns"] == []
            assert len(before_response["pending"]) == 1
            assert before_response["permits"][0]["state"] == "running"
            assert before_response["session"]["result_receipt_fence_id"] is None
            response_ready.set()
            await executor
        finally:
            response_ready.set()
            executor.cancel()
            await asyncio.gather(executor, return_exceptions=True)

    asyncio.run(asyncio.wait_for(run(), 5))
    after = snapshot(database, sid)
    assert len(requests) == 1
    assert after["pending"] == []
    assert len(after["turns"]) == len(after["receipts"]) == len(after["permits"]) == 1
    turn = after["turns"][0]
    assert turn["result_text"] == record["result"]
    assert turn["terminal_reason"] == "end_turn"
    assert turn["cost_usd"] == record["total_cost_usd"]
    assert zlib.decompress(turn["diff_blob"]) == b"native diff bytes"
    assert turn["diff_base_sha"] == "a" * 40
    assert "native_result_receipt" not in json.loads(turn["usage_json"])
    assert after["permits"][0]["state"] == "settled"
    assert after["permits"][0]["outcome"] == "end_turn"
    assert after["session"]["result_receipt_fence_id"] is None
    assert after["session"]["ember_session_id"] == f"guest-{sid}"
    assert after["session"]["cli_session_id"] == "native-cli"
    receipt = after["receipts"][0]
    assert receipt["id"] == captured["receipt"]["id"]
    assert receipt["result_body"] == captured["body"]
    assert receipt["result_sha256"] == hashlib.sha256(captured["body"]).hexdigest()
    assert receipt["received_at"] is not None
    assert receipt["response_observed_at"] is None
    assert captured["body"] != json.dumps(record).encode()
    assert json.loads(captured["body"]) == record
    unsigned_request = json.loads(captured["request"].content)
    unsigned_request.pop("result_receipt")
    assert (
        receipt["request_sha256"]
        == hashlib.sha256(json.dumps(unsigned_request).encode()).hexdigest()
    )
    assert receipt["claim_owner"] == after["permits"][0]["owner"]
    assert receipt["guest_id"] == f"guest-{sid}"
    assert receipt["session_id"] == sid
    assert receipt["seq"] == receipt["dispatch_count"] == 1
