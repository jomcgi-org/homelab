import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest
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
