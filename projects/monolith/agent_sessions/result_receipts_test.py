import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
from threading import Event
from types import SimpleNamespace
import tracemalloc

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from agent_sessions import result_receipts as receipts
from agent_sessions import admission, store, transport
from agent_sessions import result_receipts_router
from agent_sessions.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from agent_sessions.progress_ingest import app


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'receipts.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={"schema_translate_map": {"agent_sessions": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                AgentSession,
                AgentTurn,
                PendingMessage,
                AgentCapacityPool,
                AgentCapacityReservation,
                AgentResultReceipt,
            )
        ],
    )
    monkeypatch.setattr(receipts, "get_engine", lambda: engine)
    monkeypatch.setattr(store, "get_engine", lambda: engine)
    monkeypatch.setattr(admission, "get_engine", lambda: engine)
    with Session(engine) as db, db.begin():
        db.add(
            AgentSession(
                id=1,
                local_session_id="factory:task:node:1",
                workspace="<guest>",
                branch="factory/task",
                ember_session_id="guest-one",
                ember_session_token="original-guest-token",
                status="running",
                admission_tier="project",
            )
        )
        db.add(
            PendingMessage(
                session_id=1,
                seq=1,
                message_text="implement task",
                claimed_by_replica="executor-one",
                claimed_at=datetime.now(timezone.utc),
                last_dispatch_at=datetime.now(timezone.utc),
                dispatch_count=1,
            )
        )
        db.add(
            AgentCapacityReservation(
                session_id=1,
                local_session_id="factory:task:node:1",
                pending_seq=1,
                tier="project",
                state="running",
                owner="executor-one",
            )
        )
    yield engine
    engine.dispose()


def prepare(**changes):
    args = dict(
        session_id=1,
        claim_owner="executor-one",
        dispatch_count=1,
        guest_id="guest-one",
        request_body=b'{"message":"implement task"}',
    )
    return receipts.prepare_receipt(**(args | changes))


def capture(receipt, body=b'{"result":"native","terminal_reason":"end_turn"}'):
    return receipts.capture_result(receipt["id"], receipt["token"], body)


def read_active(receipt, **changes):
    return receipts.read_active_result(
        **(
            dict(
                receipt_id=receipt["id"],
                session_id=1,
                claim_owner="executor-one",
                dispatch_count=1,
                guest_id="guest-one",
                request_sha256=hashlib.sha256(
                    b'{"message":"implement task"}'
                ).hexdigest(),
            )
            | changes
        )
    )


def validate_active(db, receipt, **changes):
    store._lock_session(db, 1)
    return receipts.validate_active_result(
        db,
        **(
            dict(
                receipt_id=receipt["id"],
                result_sha256=capture_digest(),
                session_id=1,
                seq=1,
                claim_owner="executor-one",
                dispatch_count=1,
                guest_id="guest-one",
            )
            | changes
        ),
    )


def capture_digest():
    return hashlib.sha256(
        b'{"result":"native","terminal_reason":"end_turn"}'
    ).hexdigest()


def observe_response(receipt, **changes):
    return receipts.mark_response_observed(
        **(
            dict(
                receipt_id=receipt["id"],
                session_id=1,
                claim_owner="executor-one",
                dispatch_count=1,
                guest_id="guest-one",
            )
            | changes
        )
    )


def release_response_observer(receipt, **changes):
    return receipts.mark_response_observer_released(
        **(
            dict(
                receipt_id=receipt["id"],
                session_id=1,
                claim_owner="executor-one",
                dispatch_count=1,
                guest_id="guest-one",
            )
            | changes
        )
    )


def make_ownerless_discord_session(engine):
    with Session(engine) as db, db.begin():
        agent = db.get(AgentSession, 1)
        agent.local_session_id = "discord-thread-session"
        agent.discord_thread = "discord-thread-6055"
        agent.admission_tier = "interactive"
        db.add(agent)
        permit = db.exec(select(AgentCapacityReservation)).one()
        permit.local_session_id = agent.local_session_id
        permit.tier = "interactive"
        db.add(permit)


def execution_state(engine):
    with Session(engine) as db:
        return {
            model.__name__: [row.model_dump() for row in db.exec(select(model)).all()]
            for model in (
                AgentSession,
                PendingMessage,
                AgentTurn,
                AgentCapacityReservation,
            )
        }


def test_receipt_commits_exact_body_without_changing_execution(database):
    before = execution_state(database)
    receipt = prepare()
    result = capture(receipt)
    with Session(database) as db:
        row = db.get(AgentResultReceipt, receipt["id"])
        assert result == {
            "receipt_id": row.id,
            "result_sha256": hashlib.sha256(row.result_body).hexdigest(),
        }
        assert row.token_sha256 == hashlib.sha256(receipt["token"].encode()).hexdigest()
        assert receipt["token"] not in repr(row.model_dump())
        assert row.local_session_id == "factory:task:node:1"
        assert row.guest_id == "guest-one"
        assert (
            row.request_sha256
            == hashlib.sha256(b'{"message":"implement task"}').hexdigest()
        )
    assert execution_state(database) == before


def test_callback_survives_actual_unknown_writer_and_pending_deletion(database):
    receipt = prepare()
    assert store.release_pending_message_claim_sync(
        1, 1, "executor-one", "executor_cancelled"
    )
    before = execution_state(database)
    assert before["PendingMessage"] == []
    assert before["AgentCapacityReservation"][0]["state"] == "uncertain"
    assert before["AgentTurn"][0]["stop_reason"] == store.UNKNOWN_INVOCATION
    capture(receipt)
    assert execution_state(database) == before


def test_identical_retry_returns_original_ack_after_acceptance_deadline(
    database, monkeypatch
):
    receipt = prepare()
    result = capture(receipt)
    with Session(database) as db:
        row = db.get(AgentResultReceipt, receipt["id"])
        received = row.received_at
        later = receipts._aware(row.accept_until) + timedelta(seconds=1)
    monkeypatch.setattr(receipts, "_now", lambda: later)
    assert capture(receipt) == result
    with Session(database) as db:
        assert db.get(AgentResultReceipt, receipt["id"]).received_at == received


def test_conflicting_duplicate_preserves_original(database):
    receipt = prepare()
    capture(receipt, b'{"result":"first"}')
    with pytest.raises(receipts.ReceiptRejected, match="conflict") as caught:
        capture(receipt, b'{"result":"second"}')
    assert caught.value.status == 409
    with Session(database) as db:
        assert (
            db.get(AgentResultReceipt, receipt["id"]).result_body
            == b'{"result":"first"}'
        )


@pytest.mark.parametrize("stored_body", [b'{"result":"changed"}', None])
def test_duplicate_checks_exact_stored_bytes_even_when_digest_matches(
    database, stored_body
):
    receipt = prepare()
    result = capture(receipt)
    with Session(database) as db, db.begin():
        row = db.get(AgentResultReceipt, receipt["id"])
        received_at = row.received_at
        row.result_body = stored_body
        db.add(row)
    with pytest.raises(receipts.ReceiptRejected, match="conflict") as caught:
        capture(receipt)
    assert caught.value.status == 409
    with Session(database) as db:
        row = db.get(AgentResultReceipt, receipt["id"])
        assert row.result_body == stored_body
        assert row.result_sha256 == result["result_sha256"]
        assert row.received_at == received_at


def test_duplicate_callback_does_not_transfer_stored_body(database):
    receipt = prepare()
    result = capture(receipt)
    result_columns = []

    def recorded(conn, cursor, statement, parameters, context, executemany):
        if cursor.description is not None:
            result_columns.extend(column[0] for column in cursor.description)

    event.listen(database, "after_cursor_execute", recorded)
    try:
        assert capture(receipt) == result
    finally:
        event.remove(database, "after_cursor_execute", recorded)
    assert result_columns
    assert "result_body" not in result_columns


def test_parallel_identical_callbacks_return_one_immutable_result(database):
    receipt = prepare()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: capture(receipt), range(3)))
    assert results == [results[0]] * 3
    with Session(database) as db:
        row = db.get(AgentResultReceipt, receipt["id"])
        assert row.result_sha256 == results[0]["result_sha256"]
        assert hashlib.sha256(row.result_body).hexdigest() == row.result_sha256


def test_parallel_conflicting_callbacks_commit_only_one_body(database):
    receipt = prepare()

    def write(body):
        try:
            return capture(receipt, body)
        except receipts.ReceiptRejected as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [b'{"n":1}', b'{"n":2}']))
    assert sum(isinstance(x, dict) for x in results) == 1
    assert results.count(409) == 1
    with Session(database) as db:
        row = db.get(AgentResultReceipt, receipt["id"])
        assert row.result_body in (b'{"n":1}', b'{"n":2}')


@pytest.mark.parametrize(
    "field,value",
    [
        ("claim_owner", "different-executor"),
        ("dispatch_count", 2),
        ("dispatch_count", True),
        ("guest_id", "new-guest"),
        ("session_id", 2),
    ],
)
def test_mint_rejects_changed_invocation_identity(database, field, value):
    before = execution_state(database)
    with pytest.raises(receipts.ReceiptRejected):
        prepare(**{field: value})
    assert execution_state(database) == before
    with Session(database) as db:
        assert db.exec(select(AgentResultReceipt)).all() == []


def test_mint_rejects_expired_claim_and_uncertain_permit(database):
    with Session(database) as db, db.begin():
        row = db.exec(select(PendingMessage)).one()
        row.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=31)
        db.add(row)
    with pytest.raises(receipts.ReceiptRejected, match="ownership"):
        prepare()
    with Session(database) as db, db.begin():
        row = db.exec(select(PendingMessage)).one()
        row.claimed_at = datetime.now(timezone.utc)
        permit = db.exec(select(AgentCapacityReservation)).one()
        permit.state = "uncertain"
        db.add_all([row, permit])
    with pytest.raises(receipts.ReceiptRejected, match="not_admitted"):
        prepare()


def test_mint_preserves_interrupted_history_for_admitted_preemption_retry(database):
    store.mark_turn_interrupted_sync(1, 1, "executor-one")
    assert not store.release_pending_message_claim_sync(1, 1, "executor-one")
    assert store.claim_pending_message_for_session_sync(1, "executor-two") == 1
    assert admission.recheck(1, 1, "executor-two")

    before = execution_state(database)
    assert before["AgentSession"][0]["status"] == "recovering"
    assert before["PendingMessage"][0]["dispatch_count"] == 2
    interrupted = before["AgentTurn"][0]
    assert interrupted["terminal_reason"] == "interrupted"
    assert interrupted["stop_reason"] == "brick_preempted"
    # Claiming consumes the one retry grant, but keeps the interrupted record
    # until the existing result writer can replace it with the native result.
    assert "retry_dispatch_count" not in json.loads(interrupted["usage_json"])

    receipt = prepare(claim_owner="executor-two", dispatch_count=2)

    assert execution_state(database) == before
    with Session(database) as db:
        row = db.get(AgentResultReceipt, receipt["id"])
        assert row.session_id == 1
        assert row.seq == 1
        assert row.dispatch_count == 2
        assert row.claim_owner == "executor-two"
        assert row.guest_id == "guest-one"


def test_mint_accepts_admitted_queued_turn_after_previous_turn_completes(database):
    with Session(database) as db:
        pending = store.create_pending_message(db, 1, "queued follow-up")
        assert pending.seq == 2
    store.persist_turn_from_pending_sync(
        1,
        1,
        "implement task",
        transport.Turn(
            result="done",
            terminal_reason="end_turn",
            stop_reason="end_turn",
            is_error=False,
            permission_denials=[],
            num_turns=1,
            session_id="native-cli",
            usage={},
            total_cost_usd=None,
            duration_ms=10,
            activities=[],
        ),
        "done",
        "completed",
        claim_owner="executor-one",
        dispatch_count=1,
    )
    assert store.claim_pending_message_for_session_sync(1, "executor-two") == 2
    assert admission.recheck(1, 2, "executor-two")
    before = execution_state(database)
    assert before["AgentSession"][0]["status"] == "completed"

    receipt = prepare(
        claim_owner="executor-two", request_body=b'{"message":"queued follow-up"}'
    )

    assert execution_state(database) == before
    with Session(database) as db:
        row = db.get(AgentResultReceipt, receipt["id"])
        assert row.seq == 2
        assert row.dispatch_count == 1
        assert row.claim_owner == "executor-two"


@pytest.mark.parametrize("missing", ["grant", "claim", "admission"])
def test_mint_rejects_preemption_retry_without_authority(database, missing):
    store.mark_turn_interrupted_sync(1, 1, "executor-one")
    if missing == "grant":
        with Session(database) as db, db.begin():
            interrupted = db.exec(select(AgentTurn)).one()
            usage = json.loads(interrupted.usage_json)
            usage.pop("retry_dispatch_count")
            interrupted.usage_json = json.dumps(usage)
            db.add(interrupted)
    store.release_pending_message_claim_sync(1, 1, "executor-one")
    if missing == "grant":
        assert store.claim_pending_message_for_session_sync(1, "executor-two") is None
    elif missing == "admission":
        assert store.claim_pending_message_for_session_sync(1, "executor-two") == 1
        with Session(database) as db, db.begin():
            permit = db.exec(select(AgentCapacityReservation)).one()
            permit.state = "uncertain"
            db.add(permit)
    assert not admission.recheck(1, 1, "executor-two")
    before = execution_state(database)

    with pytest.raises(receipts.ReceiptRejected):
        prepare(claim_owner="executor-two", dispatch_count=2)

    assert execution_state(database) == before
    with Session(database) as db:
        assert db.exec(select(AgentResultReceipt)).all() == []


def test_each_physical_invoke_has_fresh_identity_and_preserves_late_evidence(database):
    first = prepare()
    second = prepare()
    assert first != second
    with Session(database) as db, db.begin():
        agent = db.get(AgentSession, 1)
        agent.ember_session_id = "guest-two"
        db.add(agent)
    third = prepare(guest_id="guest-two")
    capture(first)
    with Session(database) as db:
        rows = {row.id: row for row in db.exec(select(AgentResultReceipt)).all()}
        assert rows[first["id"]].superseded_at is not None
        assert rows[second["id"]].superseded_at is not None
        assert rows[third["id"]].superseded_at is None
        assert rows[first["id"]].guest_id == "guest-one"
        assert rows[first["id"]].result_body is not None
        assert rows[third["id"]].result_body is None


@pytest.mark.parametrize("body", [b"[]", b"null", b"{invalid", b'"text"'])
def test_malformed_result_is_never_acknowledged(database, body):
    receipt = prepare()
    with pytest.raises(receipts.ReceiptRejected) as caught:
        capture(receipt, body)
    assert caught.value.status == 422
    with Session(database) as db:
        assert db.get(AgentResultReceipt, receipt["id"]).result_body is None


def test_expired_receipt_acceptance_and_retention_never_release_capacity(
    database, monkeypatch
):
    receipt = prepare()
    with Session(database) as db:
        row = db.get(AgentResultReceipt, receipt["id"])
        accept_until = receipts._aware(row.accept_until)
        retain_until = receipts._aware(row.retain_until)
    before = execution_state(database)
    monkeypatch.setattr(receipts, "_now", lambda: accept_until)
    with pytest.raises(receipts.ReceiptRejected) as caught:
        capture(receipt)
    assert caught.value.status == 410
    assert receipts.prune_expired_receipts() == 0
    monkeypatch.setattr(receipts, "_now", lambda: retain_until)
    assert receipts.prune_expired_receipts() == 1
    assert execution_state(database) == before


def test_http_acknowledges_committed_result_and_rejects_invalid_token(database):
    receipt = prepare()
    client = TestClient(app)
    url = "/ingest/results/" + receipt["id"]
    wrong = client.post(
        url, content=b"{}", headers={"Authorization": "Bearer " + "x" * 43}
    )
    assert wrong.status_code == 401
    response = client.post(
        url,
        content=b'{"result":"native"}',
        headers={"Authorization": "Bearer " + receipt["token"]},
    )
    assert response.status_code == 200
    with Session(database) as db:
        row = db.get(AgentResultReceipt, receipt["id"])
        assert (
            response.json()["result_sha256"]
            == hashlib.sha256(row.result_body).hexdigest()
        )


@pytest.mark.parametrize("headers", [{"content-length": "1"}, {}])
def test_http_bounds_actual_body_even_with_lying_or_missing_length(
    database, monkeypatch, headers
):
    receipt = prepare()
    monkeypatch.setattr(receipts, "MAX_RESULT_BYTES", 16)
    response = TestClient(app).post(
        "/ingest/results/" + receipt["id"],
        content=iter([b'{"result":"', b"x" * 32, b'"}']),
        headers={**headers, "Authorization": "Bearer " + receipt["token"]},
    )
    assert response.status_code == 413
    with Session(database) as db:
        assert db.get(AgentResultReceipt, receipt["id"]).result_body is None


def test_result_size_boundary_is_exact_without_truncation(database, monkeypatch):
    receipt = prepare()
    body = b'{"result":"native"}'
    monkeypatch.setattr(receipts, "MAX_RESULT_BYTES", len(body))
    assert capture(receipt, body)["result_sha256"] == hashlib.sha256(body).hexdigest()
    with pytest.raises(receipts.ReceiptRejected) as caught:
        capture(receipt, body + b" ")
    assert caught.value.status == 413


def test_transport_commits_receipt_before_physical_post(database, monkeypatch):
    monkeypatch.setattr(transport, "EMBERVM_URL", "https://ember.example")
    posts = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, content, headers):
            payload = json.loads(content)
            receipt = payload.pop("result_receipt")
            with Session(database) as db:
                row = db.get(AgentResultReceipt, receipt["id"])
                assert row.guest_id == "guest-one"
                assert (
                    row.request_sha256
                    == hashlib.sha256(json.dumps(payload).encode()).hexdigest()
                )
            posts.append(receipt)
            capture(receipt)
            return httpx.Response(
                200,
                json={"result": "native", "terminal_reason": "end_turn"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(transport.httpx, "AsyncClient", Client)
    guest = transport.EmberSession(
        session_id="guest-one", session_token="guest-token", expires_at=None
    )

    async def run():
        for _ in range(2):
            turn, returned = await transport.EmberVmShimTransport().deliver(
                guest,
                None,
                "implement task",
                agent_session_id=1,
                dispatch_count=1,
                receipt_claim_owner="executor-one",
            )
            assert turn.result == "native"
            assert returned == guest

    asyncio.run(run())
    assert len(posts) == 2 and posts[0]["id"] != posts[1]["id"]


def test_storage_failure_has_no_ack_or_payload_log(database, monkeypatch, caplog):
    receipt = prepare()

    def failed_commit(*args):
        raise RuntimeError("native-result-secret-must-not-leak")

    monkeypatch.setattr(receipts, "capture_result", failed_commit)
    response = TestClient(app).post(
        "/ingest/results/" + receipt["id"],
        content=b'{"result":"native-result-secret-must-not-leak"}',
        headers={"Authorization": "Bearer " + receipt["token"]},
    )
    assert response.status_code == 503
    assert "native-result-secret" not in response.text + caplog.text
    assert receipt["token"] not in response.text + caplog.text
    assert "RuntimeError" in caplog.text
    with Session(database) as db:
        assert db.get(AgentResultReceipt, receipt["id"]).result_body is None


def test_receipt_capture_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_RESULT_RECEIPTS_ENABLED", raising=False)
    assert not receipts.enabled()
    monkeypatch.setenv("AGENT_RESULT_RECEIPTS_ENABLED", "true")
    assert receipts.enabled()


def test_receiver_authenticates_before_reading_body(database):
    receipt = prepare()

    class Request:
        async def stream(self):
            pytest.fail("unauthenticated body must not be read")
            yield b""

    async def run():
        with pytest.raises(HTTPException) as caught:
            await result_receipts_router.ingest_result(
                receipt["id"], Request(), "Bearer " + "z" * 43
            )
        assert caught.value.status_code == 401

    asyncio.run(run())


def test_receiver_retains_only_one_boundary_body_when_capture_starts(
    database, monkeypatch
):
    receipt = prepare()
    size = receipts.MAX_RESULT_BYTES
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]

    class Request:
        async def stream(self):
            yield b'{"result":"'
            remaining = size - len(b'{"result":""}')
            while remaining:
                length = min(remaining, 64 * 1024)
                yield b"x" * length
                remaining -= length
            yield b'"}'

    def capture_with_memory_check(receipt_id, token, body):
        retained = tracemalloc.get_traced_memory()[0] - baseline
        assert len(body) == size
        # Leave room for request/thread bookkeeping, but not another copy of
        # the uploaded chunks. This is Python retention, not a cgroup proof.
        assert retained < size + 1024 * 1024
        return {
            "receipt_id": receipt_id,
            "result_sha256": hashlib.sha256(body).hexdigest(),
        }

    monkeypatch.setattr(receipts, "capture_result", capture_with_memory_check)
    try:
        result = asyncio.run(
            result_receipts_router.ingest_result(
                receipt["id"], Request(), "Bearer " + receipt["token"]
            )
        )
        assert result["receipt_id"] == receipt["id"]
    finally:
        if not was_tracing:
            tracemalloc.stop()


def test_receiver_bounds_concurrent_uploads_and_releases_slot(database):
    receipt = prepare()

    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()

        class SlowRequest:
            async def stream(self):
                started.set()
                await finish.wait()
                yield b'{"result":"complete"}'

        class UnreadRequest:
            async def stream(self):
                pytest.fail("excess upload must not be read")
                yield b""

        first = asyncio.create_task(
            result_receipts_router.ingest_result(
                receipt["id"], SlowRequest(), "Bearer " + receipt["token"]
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        try:
            with pytest.raises(HTTPException) as caught:
                await result_receipts_router.ingest_result(
                    receipt["id"], UnreadRequest(), "Bearer " + receipt["token"]
                )
            assert caught.value.status_code == 503
            assert caught.value.detail == "receipt_receiver_busy"
        finally:
            finish.set()
            ack = await first
        assert ack["receipt_id"] == receipt["id"]
        assert result_receipts_router._capture_slots.acquire(blocking=False)
        result_receipts_router._capture_slots.release()

    asyncio.run(run())


def test_slow_body_expires_without_capture_and_releases_slot(database, monkeypatch):
    receipt = prepare()
    monkeypatch.setattr(result_receipts_router, "BODY_TIMEOUT_SECONDS", 0.01)

    class Request:
        async def stream(self):
            await asyncio.Event().wait()
            yield b""

    async def run():
        with pytest.raises(HTTPException) as caught:
            await result_receipts_router.ingest_result(
                receipt["id"], Request(), "Bearer " + receipt["token"]
            )
        assert caught.value.status_code == 408
        assert result_receipts_router._capture_slots.acquire(blocking=False)
        result_receipts_router._capture_slots.release()

    asyncio.run(run())
    with Session(database) as db:
        assert db.get(AgentResultReceipt, receipt["id"]).result_body is None


def test_poll_without_capture_reads_metadata_without_body_or_credential(database):
    receipt = prepare()
    statements = []

    def recorded(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(database, "before_cursor_execute", recorded)
    try:
        assert read_active(receipt) is None
    finally:
        event.remove(database, "before_cursor_execute", recorded)
    assert statements
    assert all("result_body" not in sql for sql in statements)
    assert all("token_sha256" not in sql for sql in statements)
    assert read_active({"id": "0" * 32}) is None


def test_active_result_provenance_is_server_owned_and_body_is_immutable(database):
    receipt = prepare()
    body = b'{"usage":{"native_result_receipt":{"receipt_id":"forged"}}}'
    capture(receipt, body)
    before = execution_state(database)
    result = read_active(receipt)
    assert result["result_body"] == body
    assert result["result_sha256"] == hashlib.sha256(body).hexdigest()
    assert result["provenance"] == {
        "receipt_id": receipt["id"],
        "session_id": 1,
        "local_session_id": "factory:task:node:1",
        "seq": 1,
        "claim_owner": "executor-one",
        "dispatch_count": 1,
        "guest_id": "guest-one",
        "request_sha256": hashlib.sha256(b'{"message":"implement task"}').hexdigest(),
        "result_sha256": hashlib.sha256(body).hexdigest(),
        "received_at": result["provenance"]["received_at"],
    }
    assert "+00:00" in result["provenance"]["received_at"]
    assert "forged" not in repr(result["provenance"])
    assert receipt["token"] not in repr(result)
    assert execution_state(database) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"claim_owner": "other"},
        {"session_id": 2},
        {"dispatch_count": 2},
        {"dispatch_count": True},
        {"guest_id": "replacement"},
        {"request_sha256": "0" * 64},
    ],
)
def test_poll_rejects_wrong_exact_request_even_before_capture(database, changes):
    receipt = prepare()
    with pytest.raises(receipts.ReceiptRejected) as caught:
        read_active(receipt, **changes)
    assert caught.value.status == 409


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "count",
        "expired_lease",
        "future_lease",
        "binding",
        "local_id",
        "permit_owner",
        "permit_local_id",
        "permit_uncertain",
        "permit_settled",
        "permit_missing",
        "newer_dispatch",
        "newer_turn",
        "superseded",
    ],
)
def test_read_and_writer_reject_changed_execution_authority(database, change):
    receipt = prepare()
    capture(receipt)
    assert read_active(receipt) is not None
    with Session(database) as db, db.begin():
        agent = db.get(AgentSession, 1)
        pending = db.exec(select(PendingMessage)).one()
        permit = db.exec(select(AgentCapacityReservation)).one()
        if change == "owner":
            pending.claimed_by_replica = "new-executor"
        elif change == "count":
            pending.dispatch_count = 2
        elif change == "expired_lease":
            pending.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=31)
        elif change == "future_lease":
            pending.claimed_at = datetime.now(timezone.utc) + timedelta(seconds=31)
        elif change == "binding":
            agent.ember_session_id = "new-guest"
        elif change == "local_id":
            agent.local_session_id = "new-owner"
        elif change == "permit_owner":
            permit.owner = "new-executor"
        elif change == "permit_local_id":
            permit.local_session_id = "new-owner"
        elif change in {"permit_uncertain", "permit_settled"}:
            permit.state = change.removeprefix("permit_")
        elif change == "newer_dispatch":
            db.add(
                PendingMessage(
                    session_id=1,
                    seq=2,
                    message_text="newer",
                    dispatch_count=1,
                    claimed_by_replica="new-executor",
                    claimed_at=datetime.now(timezone.utc),
                )
            )
        elif change == "newer_turn":
            db.add(AgentTurn(session_id=1, seq=2, prompt="newer", result_text="done"))
        elif change == "superseded":
            row = db.get(AgentResultReceipt, receipt["id"])
            row.superseded_at = datetime.now(timezone.utc)
            db.add(row)
        db.add_all([agent, pending, permit])
        if change == "permit_missing":
            db.delete(permit)
    before = execution_state(database)
    with pytest.raises(receipts.ReceiptRejected) as caught:
        read_active(receipt)
    assert caught.value.status == 409
    with Session(database) as db, pytest.raises(receipts.ReceiptRejected), db.begin():
        validate_active(db, receipt)
    assert execution_state(database) == before


def test_actual_unknown_writer_remains_held_after_capture_and_observation(database):
    receipt = prepare()
    assert store.release_pending_message_claim_sync(
        1, 1, "executor-one", "executor_cancelled", dispatch_count=1
    )
    before = execution_state(database)
    capture(receipt)
    assert observe_response(receipt)
    with pytest.raises(receipts.ReceiptRejected):
        read_active(receipt)
    with Session(database) as db, pytest.raises(receipts.ReceiptRejected), db.begin():
        validate_active(db, receipt)
    assert execution_state(database) == before


def test_consumption_fence_and_turn_evidence_rollback_together(database):
    receipt = prepare()
    capture(receipt)
    before = execution_state(database)
    with Session(database) as db, pytest.raises(RuntimeError, match="writer failed"):
        with db.begin():
            result = validate_active(db, receipt)
            db.add(
                AgentTurn(
                    session_id=1,
                    seq=1,
                    prompt="original",
                    result_text="native",
                    usage_json=json.dumps(
                        {"native_result_receipt": result["provenance"]}
                    ),
                )
            )
            db.flush()
            assert db.get(AgentSession, 1).result_receipt_fence_id == receipt["id"]
            raise RuntimeError("writer failed")
    assert execution_state(database) == before


def test_active_validation_is_idempotent_until_normal_writer_finishes(database):
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        first = validate_active(db, receipt)
        assert validate_active(db, receipt) == first
    with Session(database) as db:
        assert db.get(AgentSession, 1).result_receipt_fence_id == receipt["id"]
    with pytest.raises(receipts.ReceiptRejected):
        prepare()


@pytest.mark.parametrize("observed_first", [False, True])
def test_response_and_consumption_order_with_queued_unclaimed_followup(
    database, observed_first
):
    receipt = prepare()
    capture(receipt)
    with Session(database) as db:
        assert store.create_pending_message(db, 1, "follow-up").seq == 2
    if observed_first:
        assert observe_response(receipt)
    with Session(database) as db, db.begin():
        result = validate_active(db, receipt)
        db.delete(db.exec(select(PendingMessage).where(PendingMessage.seq == 1)).one())
        db.add(
            AgentTurn(
                session_id=1,
                seq=1,
                prompt="original",
                result_text="native",
                usage_json=json.dumps({"native_result_receipt": result["provenance"]}),
            )
        )
        permit = db.exec(select(AgentCapacityReservation)).one()
        permit.state = "settled"
        db.add(permit)
        db.flush()
        assert db.get(AgentSession, 1).result_receipt_fence_id == (
            None if observed_first else receipt["id"]
        )
    assert observe_response(receipt)
    with Session(database) as db:
        row = db.get(AgentResultReceipt, receipt["id"])
        observed_at = row.response_observed_at
        assert observed_at is not None
        assert db.get(AgentSession, 1).result_receipt_fence_id is None
        assert db.exec(select(PendingMessage)).one().dispatch_count == 0
        assert db.exec(select(AgentTurn)).one().cost_usd is None
    assert observe_response(receipt)
    with Session(database) as db:
        assert (
            db.get(AgentResultReceipt, receipt["id"]).response_observed_at
            == observed_at
        )


@pytest.mark.parametrize("rollback", [False, True])
def test_response_observer_serializes_with_active_writer(database, rollback):
    receipt = prepare()
    capture(receipt)
    writer_locked = Event()
    release_writer = Event()
    observer_started = Event()

    def consume():
        try:
            with Session(database) as db, db.begin():
                validate_active(db, receipt)
                db.flush()
                writer_locked.set()
                assert release_writer.wait(3)
                if rollback:
                    raise RuntimeError("rollback")
        except RuntimeError:
            if not rollback:
                raise

    def observe():
        observer_started.set()
        return observe_response(receipt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(consume)
        try:
            assert writer_locked.wait(3)
            observer = pool.submit(observe)
            assert observer_started.wait(3)
            assert not observer.done()
        finally:
            release_writer.set()
        writer.result(timeout=3)
        assert observer.result(timeout=3)
    with Session(database) as db:
        assert db.get(AgentSession, 1).result_receipt_fence_id is None
        assert (
            db.get(AgentResultReceipt, receipt["id"]).response_observed_at is not None
        )


@pytest.mark.parametrize(
    "changed",
    ["fence", "binding", "newer_count", "newer_owner", "newer_seq", "superseded"],
)
def test_old_observer_never_clears_a_different_or_newer_execution(database, changed):
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
    with Session(database) as db, db.begin():
        agent = db.get(AgentSession, 1)
        if changed == "fence":
            agent.result_receipt_fence_id = "newer-receipt"
        elif changed == "binding":
            agent.ember_session_id = "newer-guest"
        elif changed == "superseded":
            row = db.get(AgentResultReceipt, receipt["id"])
            row.superseded_at = datetime.now(timezone.utc)
            db.add(row)
        elif changed == "newer_seq":
            db.add(
                PendingMessage(
                    session_id=1,
                    seq=2,
                    message_text="newer",
                    dispatch_count=1,
                )
            )
        else:
            pending = db.exec(select(PendingMessage)).one()
            if changed == "newer_count":
                pending.dispatch_count = 2
            else:
                pending.claimed_by_replica = "new-executor"
            db.add(pending)
        expected = agent.result_receipt_fence_id
        db.add(agent)
    assert observe_response(receipt)
    with Session(database) as db:
        assert db.get(AgentSession, 1).result_receipt_fence_id == expected
        assert (
            db.get(AgentResultReceipt, receipt["id"]).response_observed_at is not None
        )


@pytest.mark.parametrize("release_first", [False, True])
def test_ownerless_fence_releases_when_its_exact_observer_is_gone(
    database, release_first
):
    make_ownerless_discord_session(database)
    receipt = prepare()
    capture(receipt)
    if release_first:
        assert release_response_observer(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
    if not release_first:
        with Session(database) as db:
            assert db.get(AgentSession, 1).result_receipt_fence_id == receipt["id"]
        assert release_response_observer(receipt)
    with Session(database) as db:
        agent = db.get(AgentSession, 1)
        stored = db.get(AgentResultReceipt, receipt["id"])
        assert agent.result_receipt_fence_id is None
        assert stored.response_observer_released_at is not None
        assert stored.response_observed_at is None


def test_observer_release_preserves_factory_and_drainer_cleanup_ownership(database):
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
    assert release_response_observer(receipt)
    with Session(database) as db:
        assert db.get(AgentSession, 1).result_receipt_fence_id == receipt["id"]
        assert (
            db.get(AgentResultReceipt, receipt["id"]).response_observer_released_at
            is not None
        )


def test_binding_cleanup_waits_for_the_live_exact_observer(database):
    make_ownerless_discord_session(database)
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
    with Session(database) as db:
        assert store.clear_ember_bindings_by_ember_id(db, "guest-one") == []
        held = db.get(AgentSession, 1)
        assert held.result_receipt_fence_id == receipt["id"]
        assert held.ember_session_id == "guest-one"

    assert release_response_observer(receipt)
    with Session(database) as db:
        assert store.clear_ember_bindings_by_ember_id(db, "guest-one") == [1]
        cleared = db.get(AgentSession, 1)
        assert cleared.result_receipt_fence_id is None
        assert cleared.ember_session_id is None


@pytest.mark.parametrize("changed", ["fence", "binding", "newer_turn"])
def test_released_old_observer_never_clears_newer_identity(database, changed):
    make_ownerless_discord_session(database)
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
        agent = db.get(AgentSession, 1)
        if changed == "fence":
            agent.result_receipt_fence_id = "newer-receipt"
        elif changed == "binding":
            agent.ember_session_id = "newer-guest"
        else:
            db.add(
                AgentTurn(
                    session_id=1,
                    seq=2,
                    prompt="newer",
                    result_text="newer",
                )
            )
        expected_fence = agent.result_receipt_fence_id
        db.add(agent)
    assert release_response_observer(receipt)
    with Session(database) as db:
        assert db.get(AgentSession, 1).result_receipt_fence_id == expected_fence
        assert (
            db.get(AgentResultReceipt, receipt["id"]).response_observer_released_at
            is not None
        )


@pytest.mark.parametrize("change", ["seq", "digest", "corrupt_body", "other_fence"])
def test_locked_validation_rejects_wrong_result_without_mutation(database, change):
    receipt = prepare()
    capture(receipt)
    changes = {}
    if change in {"corrupt_body", "other_fence"}:
        with Session(database) as db, db.begin():
            if change == "corrupt_body":
                row = db.get(AgentResultReceipt, receipt["id"])
                row.result_body = b'{"result":"different"}'
            else:
                row = db.get(AgentSession, 1)
                row.result_receipt_fence_id = "other"
            db.add(row)
    elif change == "seq":
        changes["seq"] = 2
    else:
        changes["result_sha256"] = "0" * 64
    before = execution_state(database)
    with Session(database) as db, pytest.raises(receipts.ReceiptRejected), db.begin():
        validate_active(db, receipt, **changes)
    assert execution_state(database) == before


def test_retention_deletion_preserves_fence_and_expiry_refuses_consumption(
    database, monkeypatch
):
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
        expiry = receipts._aware(db.get(AgentResultReceipt, receipt["id"]).retain_until)
    monkeypatch.setattr(receipts, "_now", lambda: expiry)
    before = execution_state(database)
    with pytest.raises(receipts.ReceiptRejected):
        read_active(receipt)
    assert receipts.prune_expired_receipts() == 1
    assert observe_response(receipt) is False
    assert execution_state(database) == before
    with Session(database) as db:
        assert db.get(AgentSession, 1).result_receipt_fence_id == receipt["id"]


def test_observer_rejects_forged_identity_without_recording_response(database):
    receipt = prepare()
    with pytest.raises(receipts.ReceiptRejected):
        observe_response(receipt, claim_owner="impostor")
    with Session(database) as db:
        assert db.get(AgentResultReceipt, receipt["id"]).response_observed_at is None


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
@pytest.mark.parametrize("operation", ["read", "observe"])
def test_optional_observers_set_only_postgres_transaction_limits_before_queries(
    monkeypatch, dialect, operation
):
    calls = []

    class ObserverSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("session_closed")

        def begin(self):
            return nullcontext(self)

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name=dialect))

        def execute(self, statement):
            calls.append(str(statement))

    def metadata(db, receipt_id):
        calls.append("metadata_read")
        return None

    def lock_session(db, session_id):
        calls.append("session_lock")
        return None

    monkeypatch.setattr(receipts, "get_engine", lambda: None)
    monkeypatch.setattr(receipts, "Session", lambda engine: ObserverSession())
    monkeypatch.setattr(receipts, "_receipt_metadata", metadata)
    monkeypatch.setattr(store, "_lock_session", lock_session)
    if operation == "read":
        assert read_active({"id": "0" * 32}) is None
    else:
        assert observe_response({"id": "0" * 32}) is False
    limits = [call for call in calls if call.startswith("SET")]
    assert limits == (
        ["SET LOCAL lock_timeout = '1s'", "SET LOCAL statement_timeout = '3s'"]
        if dialect == "postgresql"
        else []
    )
    # Both bounds precede the optional receipt read and, for a late response,
    # the pool/session lock. LOCAL prevents a pooled connection default change.
    assert calls[: len(limits)] == limits
    assert calls[len(limits)] == (
        "metadata_read" if operation == "read" else "session_lock"
    )
    assert calls[-1] == "session_closed"


def release_unobserved(receipt, **changes):
    return receipts.release_unobserved_fence(
        **(
            dict(
                receipt_id=receipt["id"],
                session_id=1,
                claim_owner="executor-one",
                dispatch_count=1,
                guest_id="guest-one",
            )
            | changes
        )
    )


def test_unobserved_release_frees_a_received_receipts_guest(database):
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
    with Session(database) as db:
        assert db.get(AgentSession, 1).result_receipt_fence_id == receipt["id"]
    assert release_unobserved(receipt)
    with Session(database) as db:
        assert db.get(AgentSession, 1).result_receipt_fence_id is None
        # Nothing observed a response, so the stamp every reconciliation and
        # lease owner reads as evidence about the response stays unset.
        assert db.get(AgentResultReceipt, receipt["id"]).response_observed_at is None


def test_observed_response_leaves_nothing_for_the_unobserved_release(database):
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
    assert observe_response(receipt)
    before = execution_state(database)
    assert before["AgentSession"][0]["result_receipt_fence_id"] is None
    assert release_unobserved(receipt) is False
    assert execution_state(database) == before


def test_unreceived_receipt_keeps_its_guest_while_the_post_may_still_land(database):
    receipt = prepare()
    with Session(database) as db, db.begin():
        # The normal writer cannot fence an uncaptured receipt, so build the
        # shape by hand: a guest that is still the only path to the turn.
        agent = store._lock_session(db, 1)
        agent.result_receipt_fence_id = receipt["id"]
        db.add(agent)
    before = execution_state(database)
    assert release_unobserved(receipt) is False
    assert receipts.release_abandoned_fence(1, receipt["id"]) is False
    assert execution_state(database) == before


def test_discord_session_claims_followup_after_receipt_accept_window(
    database, monkeypatch
):
    make_ownerless_discord_session(database)
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
        pending = db.exec(select(PendingMessage)).one()
        db.delete(pending)
        db.add(
            AgentTurn(
                session_id=1,
                seq=1,
                prompt="first Discord message",
                result_text="done",
                terminal_reason="completed",
                stop_reason="end_turn",
            )
        )
        permit = db.exec(select(AgentCapacityReservation)).one()
        permit.state = "settled"
        db.add(permit)
        accept_until = receipts._aware(
            db.get(AgentResultReceipt, receipt["id"]).accept_until
        )
    with Session(database) as db:
        followup = store.create_pending_message(db, 1, "next Discord message")
        assert followup.seq == 2

    # A live original POST retains the exact fence inside its acceptance bound.
    assert store.claim_pending_message_for_session_sync(1, "discord-followup") is None
    with Session(database) as db:
        assert db.get(AgentSession, 1).result_receipt_fence_id == receipt["id"]

    monkeypatch.setattr(receipts, "_now", lambda: accept_until)
    assert store.claim_pending_message_for_session_sync(1, "discord-followup") == 2
    with Session(database) as db:
        agent = db.get(AgentSession, 1)
        assert agent.discord_thread == "discord-thread-6055"
        assert agent.result_receipt_fence_id is None
        assert agent.ember_session_id == "guest-one"


@pytest.mark.parametrize(
    "changes",
    [
        {"dispatch_count": 2},
        {"claim_owner": "newer-executor"},
        {"guest_id": "newer-guest"},
        {"session_id": 2},
    ],
)
def test_stale_dispatch_never_releases_a_live_fence(database, changes):
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
    before = execution_state(database)
    with pytest.raises(receipts.ReceiptRejected):
        release_unobserved(receipt, **changes)
    assert execution_state(database) == before


def test_release_refuses_a_fence_whose_guest_binding_has_moved(database):
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
        agent = db.get(AgentSession, 1)
        agent.ember_session_id = "newer-guest"
        db.add(agent)
    assert release_unobserved(receipt) is False
    assert receipts.release_abandoned_fence(1, receipt["id"]) is False
    with Session(database) as db:
        assert db.get(AgentSession, 1).result_receipt_fence_id == receipt["id"]


def test_reaper_release_frees_received_gone_and_expired_fences(database, monkeypatch):
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
    # A fence naming a receipt whose body has arrived: the turn completed
    # through it and only an unread synchronous response is outstanding.
    assert receipts.release_abandoned_fence(1, receipt["id"])
    with Session(database) as db, db.begin():
        agent = store._lock_session(db, 1)
        agent.result_receipt_fence_id = "e" * 32
        db.add(agent)
    # A fence naming a receipt retention has already deleted.
    assert receipts.release_abandoned_fence(1, "e" * 32)
    with Session(database) as db, db.begin():
        agent = store._lock_session(db, 1)
        agent.result_receipt_fence_id = receipt["id"]
        db.add(agent)
        row = db.get(AgentResultReceipt, receipt["id"])
        row.received_at = None
        row.result_sha256 = None
        row.result_body = None
        db.add(row)
        accepts_until = receipts._aware(row.accept_until)
    # Unreceived and inside the acceptance window: the guest stays.
    assert receipts.release_abandoned_fence(1, receipt["id"]) is False
    monkeypatch.setattr(receipts, "_now", lambda: accepts_until)
    # Past it the body can never be captured, so the fence is dead.
    assert receipts.release_abandoned_fence(1, receipt["id"])
    with Session(database) as db:
        assert db.get(AgentSession, 1).result_receipt_fence_id is None


def test_reaper_release_ignores_a_fence_it_was_not_asked_for(database):
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
    before = execution_state(database)
    assert receipts.release_abandoned_fence(1, "f" * 32) is False
    assert receipts.release_abandoned_fence(2, receipt["id"]) is False
    assert execution_state(database) == before


def test_restore_puts_a_released_fence_back_for_the_same_guest(database):
    receipt = prepare()
    capture(receipt)
    with Session(database) as db, db.begin():
        validate_active(db, receipt)
    fenced = execution_state(database)
    assert receipts.release_abandoned_fence(1, receipt["id"])
    assert receipts.restore_abandoned_fence(1, receipt["id"], "guest-one")
    assert execution_state(database) == fenced
    # A fence that is still held, one another owner has taken, and a binding
    # that has moved on are all left exactly as they are.
    assert receipts.restore_abandoned_fence(1, receipt["id"], "guest-one") is False
    assert receipts.release_abandoned_fence(1, receipt["id"])
    assert receipts.restore_abandoned_fence(1, receipt["id"], "newer-guest") is False
    with Session(database) as db:
        assert db.get(AgentSession, 1).result_receipt_fence_id is None
