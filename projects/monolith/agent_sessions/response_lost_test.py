"""Recovering a turn whose invoke response was lost, through the real executor.

Every case drives ``mcp._execute_pending_message`` with the actual transport,
store, admission and receipt modules against a file-backed database. Only the
HTTP boundary is faked, so a recovered result has travelled the same parser,
diff, artifact and permit path a synchronous response would have.
"""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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
from agent_sessions.constants import RESPONSE_LOST, UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from core import db as core_db

ARTIFACT_PATH = ".factory/result.json"


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'response-lost.db'}",
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
    monkeypatch.setenv("AGENT_RESULT_RECEIPT_ADOPTION_ENABLED", "false")
    monkeypatch.setenv("AGENT_RESPONSE_LOST_RECOVERY_ENABLED", "true")
    monkeypatch.setattr(transport, "RECEIPT_POLL_SECONDS", 0.005)
    observer_executor = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(transport, "_receipt_db_executor", observer_executor)
    monkeypatch.setattr(transport, "_receipt_db_slots", BoundedSemaphore(2))
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _sid: None)
    monkeypatch.setattr(execution_api, "_schedule_next_message", lambda _sid: None)
    mcp._negative_oracle_verdicts.clear()

    async def notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mcp, "_notify_terminal", notify)
    try:
        yield engine
    finally:
        observer_executor.shutdown(wait=True, cancel_futures=True)
        mcp._negative_oracle_verdicts.clear()
        engine.dispose()


def queue(engine, key="response-lost-project", *, tier="project"):
    with Session(engine) as db:
        agent = store.create_session(
            db, key, "<guest>", "main", "luna", admission_tier=tier
        )
        sid = agent.id
        store.set_ember_session(db, sid, f"guest-{sid}", "guest-token", None)
        store.create_pending_message(db, sid, "implement the bounded task", "luna")
        return sid


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


def native_record(cost=0.125, artifact=True):
    record = {
        "result": "Implemented the bounded task.",
        "terminal_reason": "end_turn",
        "stop_reason": "end_turn",
        "is_error": False,
        "session_id": "native-cli",
        "num_turns": 1,
        "total_cost_usd": cost,
        "duration_ms": 23,
        "activities": [],
        "usage": {"input_tokens": 3},
        "diff": {
            "base_sha": "a" * 40,
            "truncated": False,
            "zlib_b64": base64.b64encode(zlib.compress(b"native diff bytes")).decode(),
        },
    }
    if artifact:
        record["artifact"] = {
            "path": ARTIFACT_PATH,
            "outcome": "ok",
            "content_b64": base64.b64encode(b'{"status": "ok"}').decode(),
        }
    return record


def fake_http(monkeypatch, post_handler, guest_state=None):
    """Fake only the HTTP boundary; every other owner is the real one."""
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
            return await post_handler(request)

        async def get(self, url, **kwargs):
            request = httpx.Request("GET", url, **kwargs)
            if guest_state is None:
                pytest.fail("this case must not read the control plane")
            return httpx.Response(200, json=guest_state(), request=request)

        async def delete(self, *args, **kwargs):
            pytest.fail("a held turn must never destroy its working guest")

    monkeypatch.setattr(transport.httpx, "AsyncClient", Client)
    monkeypatch.setattr(transport, "EMBERVM_URL", "https://ember.test")
    monkeypatch.setattr(mcp, "_transport", transport.EmberVmShimTransport())
    return requests


STARTED_AT = 1788874047685


def working_guest(sid, generation=0, started=STARTED_AT, last=None):
    """A control-plane view of a guest with an invoke still in progress."""

    def view():
        return {
            "session_id": f"guest-{sid}",
            "state": "running",
            "generation": generation,
            "invoke_started_at": started,
            "last_invoke_at": last,
        }

    return view


def publish(record, request):
    """Publish the native record the guest produced for this exact POST."""
    receipt = json.loads(request.content)["result_receipt"]
    body = json.dumps(record).encode()
    result_receipts.capture_result(receipt["id"], receipt["token"], body)
    return receipt


def hold_of(sid):
    return store.read_response_lost_hold_sync(sid)


def expire_hold(engine, sid):
    """Push one hold past its own bound without touching anything else."""
    with Session(engine) as db:
        turn = db.exec(select(AgentTurn).where(AgentTurn.session_id == sid)).one()
        usage = json.loads(turn.usage_json)
        usage["recovery"]["response_lost"]["hold_until"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        turn.usage_json = json.dumps(usage)
        db.add(turn)
        db.commit()


def age_claim(engine, sid, seconds=120):
    with Session(engine) as db:
        pending = store.get_pending_message(db, sid, 1)
        pending.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        db.add(pending)
        db.commit()


def lose_the_response(database, monkeypatch, *, record=None, guest_state=None):
    """Run one real turn whose POST fails after the guest starts working."""
    sid = queue(database)

    async def handler(request):
        if record is not None:
            publish(record, request)
        raise httpx.ReadError("original response lost", request=request)

    requests = fake_http(monkeypatch, handler, guest_state or working_guest(sid))
    asyncio.run(asyncio.wait_for(mcp._execute_pending_message(sid), 10))
    return sid, requests


def assert_held(database, sid, reason):
    state = snapshot(database, sid)
    assert len(state["pending"]) == 1, "a held attempt keeps its queued prompt"
    assert state["pending"][0]["claimed_by_replica"] is not None
    assert state["pending"][0]["dispatch_count"] == 1
    assert len(state["turns"]) == 1
    turn = state["turns"][0]
    assert turn["terminal_reason"] == "interrupted"
    assert turn["stop_reason"] == RESPONSE_LOST
    assert turn["cost_usd"] is None
    recovery = json.loads(turn["usage_json"])["recovery"]
    assert recovery["invocation_phase"] == RESPONSE_LOST
    assert recovery["response_lost"]["reason"] == reason
    # The permit is neither released nor made uncertain: the guest is still
    # holding the capacity this attempt was admitted for.
    assert [row["state"] for row in state["permits"]] == ["running"]
    assert state["permits"][0]["outcome"] is None
    assert state["session"]["status"] == "recovering"
    assert state["session"]["result_receipt_fence_id"] is None
    assert state["session"]["ember_session_id"] == f"guest-{sid}"
    return state


def assert_unknown(database, sid):
    state = snapshot(database, sid)
    assert state["pending"] == []
    assert len(state["turns"]) == 1
    assert state["turns"][0]["terminal_reason"] == "error"
    assert state["turns"][0]["stop_reason"] == UNKNOWN_INVOCATION
    assert [row["state"] for row in state["permits"]] == ["uncertain"]
    assert state["session"]["status"] == "failed"
    return state


def test_lost_response_recovers_the_exact_result_without_re_executing(
    database, monkeypatch
):
    record = native_record()
    sid, requests = lose_the_response(database, monkeypatch)
    held = assert_held(database, sid, "invoke_response_lost")
    assert len(requests) == 1

    # The guest finishes and publishes long after its observer is gone.
    receipt = json.loads(requests[0].content)["result_receipt"]
    result_receipts.capture_result(
        receipt["id"], receipt["token"], json.dumps(record).encode()
    )
    assert snapshot(database, sid) == held, "capture alone changes no execution state"

    outcome = store.adopt_response_lost_result(sid, ARTIFACT_PATH)
    assert outcome == {"status": "adopted", "seq": 1, "receipt_id": receipt["id"]}
    after = snapshot(database, sid)
    assert len(requests) == 1, "recovery must not invoke the model a second time"
    assert after["pending"] == []
    assert len(after["turns"]) == 1
    turn = after["turns"][0]
    assert turn["terminal_reason"] == "end_turn"
    assert turn["stop_reason"] == "end_turn"
    assert turn["result_text"] == record["result"]
    assert turn["cost_usd"] == 0.125
    assert zlib.decompress(turn["diff_blob"]) == b"native diff bytes"
    assert turn["diff_base_sha"] == "a" * 40
    assert turn["artifact_path"] == ARTIFACT_PATH
    assert bytes(turn["artifact_blob"]) == b'{"status": "ok"}'
    assert turn["artifact_outcome"] == "ok"
    provenance = json.loads(turn["usage_json"])["native_result_receipt"]
    assert provenance["receipt_id"] == receipt["id"]
    assert provenance["response_lost_recovery"] is True
    assert provenance["session_id"] == sid
    assert provenance["guest_id"] == f"guest-{sid}"
    assert provenance["seq"] == provenance["dispatch_count"] == 1
    assert receipt["token"] not in turn["usage_json"]
    assert [row["state"] for row in after["permits"]] == ["settled"]
    assert after["permits"][0]["outcome"] == "end_turn"
    assert after["session"]["status"] == "completed"
    assert after["session"]["cli_session_id"] == "native-cli"
    # No observer is left to clear a fence, so adoption must not set one.
    assert after["session"]["result_receipt_fence_id"] is None


def test_a_held_attempt_is_not_released_reclaimed_or_supervised(database, monkeypatch):
    sid, _requests = lose_the_response(database, monkeypatch)
    held = assert_held(database, sid, "invoke_response_lost")
    owner = held["pending"][0]["claimed_by_replica"]

    assert not store.release_pending_message_claim_sync(
        sid, 1, owner, "observer_released"
    )
    assert snapshot(database, sid) == held

    age_claim(database, sid)
    assert store.reclaim_stale_claims_sync() == 0
    aged = snapshot(database, sid)
    assert aged["pending"][0]["claimed_by_replica"] == owner
    assert [row["state"] for row in aged["permits"]] == ["running"]

    # Nothing here is a supervision candidate: supervision selects uncertain
    # permits, and the zombie sweeps select sessions with no turn at all.
    with Session(database) as db:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=mcp.ZOMBIE_SESSION_THRESHOLD_SECONDS)
        assert store.find_zombie_session_ids(db, cutoff, now) == []
        hung = now - timedelta(seconds=mcp.HUNG_CLAIM_THRESHOLD_SECONDS)
        assert store.find_hung_claim_session_ids(db, hung, now) == []
        assert not store.has_unknown_outcome(db, sid)
    # A follow-up message may be queued but never dispatched over the hold.
    assert store.claim_pending_message_for_session_sync(sid, "another-replica") is None
    assert snapshot(database, sid) == aged


def test_replica_shutdown_holds_the_turn_instead_of_recording_unknown(
    database, monkeypatch
):
    sid = queue(database)
    posted = asyncio.Event()

    async def handler(_request):
        posted.set()
        await asyncio.Event().wait()

    requests = fake_http(monkeypatch, handler)

    async def run():
        task = asyncio.create_task(mcp._execute_pending_message(sid))
        await asyncio.wait_for(posted.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(run(), 10))
    assert len(requests) == 1
    held = assert_held(database, sid, "replica_shutdown")
    assert hold_of(sid)["source"] == "executor"
    assert held["turns"][0]["seq"] == 1


def test_conflicting_and_duplicate_receipts_cannot_finish_a_held_turn(
    database, monkeypatch
):
    record = native_record()
    sid, requests = lose_the_response(database, monkeypatch)
    held = assert_held(database, sid, "invoke_response_lost")
    hold = hold_of(sid)
    receipt = json.loads(requests[0].content)["result_receipt"]

    # A different guest, an older attempt and a newer attempt are all refused,
    # and none of them may be substituted for the receipt the hold names.
    for field, value in (
        ("guest_id", "some-other-guest"),
        ("dispatch_count", 2),
        ("claim_owner", "a-later-replica"),
        ("session_id", sid + 1),
    ):
        with pytest.raises(result_receipts.ReceiptRejected):
            result_receipts.read_held_result({**hold, field: value})
    assert snapshot(database, sid) == held

    # A superseded receipt is refused even carrying a valid body.
    result_receipts.capture_result(
        receipt["id"], receipt["token"], json.dumps(record).encode()
    )
    with Session(database) as db:
        row = db.get(AgentResultReceipt, receipt["id"])
        row.superseded_at = datetime.now(timezone.utc)
        db.add(row)
        db.commit()
    assert store.adopt_response_lost_result(sid, ARTIFACT_PATH)["status"] == "refused"
    assert snapshot(database, sid) == held

    # A second capture with different bytes conflicts rather than overwriting.
    with Session(database) as db:
        row = db.get(AgentResultReceipt, receipt["id"])
        row.superseded_at = None
        db.add(row)
        db.commit()
    with pytest.raises(
        result_receipts.ReceiptRejected, match="receipt_result_conflict"
    ):
        result_receipts.capture_result(
            receipt["id"],
            receipt["token"],
            json.dumps(native_record(cost=99.0)).encode(),
        )
    # The original bytes are still the only ones that can be adopted.
    assert store.adopt_response_lost_result(sid, ARTIFACT_PATH)["status"] == "adopted"
    with Session(database) as db:
        turn = db.exec(select(AgentTurn).where(AgentTurn.session_id == sid)).one()
        assert turn.cost_usd == 0.125
    assert len(requests) == 1


def test_adoption_is_idempotent_across_a_restart_between_receipt_and_turn(
    database, monkeypatch
):
    record = native_record()
    sid, requests = lose_the_response(database, monkeypatch, record=record)
    assert_held(database, sid, "invoke_response_lost")

    assert store.adopt_response_lost_result(sid, ARTIFACT_PATH)["status"] == "adopted"
    first = snapshot(database, sid)
    # A second recovering replica finds no hold and changes nothing.
    assert store.adopt_response_lost_result(sid, ARTIFACT_PATH) is None
    assert store.find_response_lost_session_ids() == []
    assert snapshot(database, sid) == first
    assert len(requests) == 1


def test_callback_outage_keeps_the_hold_and_then_settles_uncertain(
    database, monkeypatch
):
    sid, _requests = lose_the_response(database, monkeypatch)
    held = assert_held(database, sid, "invoke_response_lost")
    # No receipt body ever arrives, so the hold waits rather than resolving.
    for _ in range(3):
        assert store.adopt_response_lost_result(sid, ARTIFACT_PATH) == {
            "status": "waiting",
            "seq": 1,
        }
    assert snapshot(database, sid) == held

    expire_hold(database, sid)
    assert hold_of(sid) is None
    assert store.adopt_response_lost_result(sid, ARTIFACT_PATH) is None
    age_claim(database, sid)
    assert store.reclaim_stale_claims_sync() == 1
    assert_unknown(database, sid)


def test_guest_that_ceased_without_a_receipt_settles_uncertain_as_today(
    database, monkeypatch
):
    from swarm import node_workflows

    sid, _requests = lose_the_response(database, monkeypatch)
    assert_held(database, sid, "invoke_response_lost")
    pin = {"artifact_path": ARTIFACT_PATH}
    monkeypatch.setattr(
        node_workflows,
        "_observe_held_guest",
        lambda guest: {
            "session_id": guest,
            "state": "destroyed",
            "generation": 0,
            "invoke_started_at": 1788874047685,
            "last_invoke_at": 1788874050000,
        },
    )
    assert node_workflows._recover_response_lost(pin, sid) == {
        "status": "settled",
        "reason": "response_lost_guest_ceased",
    }
    assert_unknown(database, sid)


def test_node_recovery_adopts_a_still_running_guests_published_result(
    database, monkeypatch
):
    from swarm import node_workflows

    record = native_record()
    sid, requests = lose_the_response(database, monkeypatch, record=record)
    assert_held(database, sid, "invoke_response_lost")
    observed = []

    def observe(guest):
        observed.append(guest)
        return {
            "session_id": guest,
            "state": "running",
            "generation": 0,
            "invoke_started_at": 1788874047685,
            "last_invoke_at": None,
        }

    monkeypatch.setattr(node_workflows, "_observe_held_guest", observe)
    outcome = node_workflows._recover_response_lost(
        {"artifact_path": ARTIFACT_PATH}, sid
    )
    assert outcome["status"] == "adopted"
    # A published result is adopted without ever asking the control plane.
    assert observed == []
    with Session(database) as db:
        turn = db.exec(select(AgentTurn).where(AgentTurn.session_id == sid)).one()
        assert turn.artifact_path == ARTIFACT_PATH
        assert turn.terminal_reason == "end_turn"
    assert len(requests) == 1


def test_a_working_guest_with_no_published_result_keeps_its_hold(database, monkeypatch):
    from swarm import node_workflows

    sid, _requests = lose_the_response(database, monkeypatch)
    held = assert_held(database, sid, "invoke_response_lost")
    monkeypatch.setattr(
        node_workflows,
        "_observe_held_guest",
        lambda guest: {
            "session_id": guest,
            "state": "running",
            "generation": 0,
            "invoke_started_at": 1788874047685,
            "last_invoke_at": None,
        },
    )
    assert node_workflows._recover_response_lost(
        {"artifact_path": ARTIFACT_PATH}, sid
    ) == {
        "status": "waiting",
        "seq": 1,
    }
    assert snapshot(database, sid) == held


def test_a_dead_guest_records_unknown_without_a_hold(database, monkeypatch):
    """The control plane refusing the liveness shape keeps today's behaviour."""
    sid, requests = lose_the_response(
        database,
        monkeypatch,
        guest_state=lambda: {
            "session_id": "guest-1",
            "state": "destroyed",
            "generation": 0,
            "invoke_started_at": 1788874047685,
            "last_invoke_at": 1788874050000,
        },
    )
    assert len(requests) == 1
    state = snapshot(database, sid)
    assert state["pending"] == []
    assert state["turns"][0]["terminal_reason"] == "error"
    assert state["turns"][0]["stop_reason"] is None
    assert [row["state"] for row in state["permits"]] == ["uncertain"]
    assert store.read_response_lost_hold_sync(sid) is None


@pytest.mark.parametrize("outage", ["refused", "reset"])
def test_control_plane_outage_holds_when_the_liveness_read_also_fails(
    database, monkeypatch, outage
):
    from swarm import node_workflows

    sid = queue(database)

    async def handler(request):
        if outage == "refused":
            raise httpx.ConnectError(
                "control plane refused connection", request=request
            )
        raise httpx.ReadError("control plane reset connection", request=request)

    def unavailable():
        raise httpx.ConnectError("control plane still unavailable")

    requests = fake_http(monkeypatch, handler, unavailable)
    asyncio.run(asyncio.wait_for(mcp._execute_pending_message(sid), 10))
    assert len(requests) == 1
    assert_held(database, sid, "control_plane_unavailable")
    hold = hold_of(sid)
    assert hold["generation"] is None
    assert hold["invoke_started_at"] is None

    observed = []

    def recovered(guest_id):
        observed.append(guest_id)
        return working_guest(sid)()

    monkeypatch.setattr(node_workflows, "_observe_held_guest", recovered)
    assert node_workflows._recover_response_lost(
        {"artifact_path": ARTIFACT_PATH}, sid
    ) == {"status": "waiting", "seq": 1}
    assert observed == [f"guest-{sid}"]
    assert snapshot(database, sid)["pending"][0]["dispatch_count"] == 1
    assert len(requests) == 1

    publish(native_record(), requests[0])
    assert (
        node_workflows._recover_response_lost({"artifact_path": ARTIFACT_PATH}, sid)[
            "status"
        ]
        == "adopted"
    )
    assert len(requests) == 1


def test_guest_http_failure_is_not_reclassified_when_liveness_is_unavailable(
    database, monkeypatch
):
    sid = queue(database)

    async def handler(request):
        return httpx.Response(
            502,
            json={"error": "guest process failed", "retryable": False},
            request=request,
        )

    def unavailable():
        raise httpx.ConnectError("control plane unavailable during follow-up")

    requests = fake_http(monkeypatch, handler, unavailable)
    asyncio.run(asyncio.wait_for(mcp._execute_pending_message(sid), 10))
    state = snapshot(database, sid)
    assert len(requests) == 1
    assert state["pending"] == []
    assert state["turns"][0]["terminal_reason"] == "error"
    assert "guest process failed" in state["turns"][0]["result_text"]
    assert state["turns"][0]["stop_reason"] is None
    assert store.read_response_lost_hold_sync(sid) is None


def test_recovery_disabled_records_unknown_exactly_as_before(database, monkeypatch):
    monkeypatch.setenv("AGENT_RESPONSE_LOST_RECOVERY_ENABLED", "false")
    sid, _requests = lose_the_response(database, monkeypatch, guest_state=None)
    state = snapshot(database, sid)
    assert state["pending"] == []
    assert state["turns"][0]["stop_reason"] is None
    assert [row["state"] for row in state["permits"]] == ["uncertain"]


def test_lease_backstop_holds_only_a_stale_claim_with_an_unconsumed_result(
    database, monkeypatch
):
    record = native_record()
    sid = queue(database)
    owner = "killed-replica"
    assert store.claim_pending_message_for_session_sync(sid, owner) == 1
    assert admission.recheck(sid, 1, owner)

    # A replica killed outright before its guest published anything is unknown,
    # exactly as it is today.
    age_claim(database, sid)
    assert store.reclaim_stale_claims_sync() == 1
    assert_unknown(database, sid)

    other = queue(database, "second-response-lost")
    assert store.claim_pending_message_for_session_sync(other, owner) == 1
    assert admission.recheck(other, 1, owner)
    receipt = result_receipts.prepare_receipt(
        other, owner, 1, f"guest-{other}", b'{"message":"request"}'
    )
    result_receipts.capture_result(
        receipt["id"], receipt["token"], json.dumps(record).encode()
    )
    age_claim(database, other)
    assert store.reclaim_stale_claims_sync() == 0
    held = assert_held(database, other, "lease_expired")
    assert hold_of(other)["source"] == "lease_backstop"
    # This result carries an artifact and the hold records no declaration, so
    # the sweep refuses rather than adopting it without one.
    assert mcp._adopt_response_lost_results() == []
    assert snapshot(database, other) == held
    assert store.adopt_response_lost_result(other, ARTIFACT_PATH)["status"] == "adopted"
    with Session(database) as db:
        turn = db.exec(select(AgentTurn).where(AgentTurn.session_id == other)).one()
        assert turn.artifact_path == ARTIFACT_PATH
        assert turn.terminal_reason == "end_turn"


def test_the_sweep_finishes_executor_written_holds(database, monkeypatch):
    record = native_record(artifact=False)
    sid, requests = lose_the_response(database, monkeypatch, record=record)
    assert_held(database, sid, "invoke_response_lost")
    assert store.find_response_lost_session_ids() == [sid]
    assert mcp._adopt_response_lost_results() == [sid]
    after = snapshot(database, sid)
    assert after["pending"] == []
    assert after["turns"][0]["terminal_reason"] == "end_turn"
    assert [row["state"] for row in after["permits"]] == ["settled"]
    assert len(requests) == 1
    assert mcp._adopt_response_lost_results() == []


def test_recovery_disabled_leaves_every_owner_on_todays_behaviour(
    database, monkeypatch
):
    """The flag off is the whole machinery off, whatever the receipt flags say."""
    from swarm import node_workflows

    record = native_record(artifact=False)
    sid, _requests = lose_the_response(database, monkeypatch, record=record)
    held = assert_held(database, sid, "invoke_response_lost")
    monkeypatch.setenv("AGENT_RESPONSE_LOST_RECOVERY_ENABLED", "false")

    assert store.adopt_response_lost_result(sid) is None
    assert mcp._adopt_response_lost_results() == []
    assert node_workflows._recover_response_lost({"artifact_path": None}, sid) is None
    assert snapshot(database, sid) == held

    # The lease backstop is off too, so a stale claim with an unconsumed
    # receipt settles unknown exactly as it did before this change.
    other = queue(database, "disabled-backstop")
    owner = "killed-replica"
    assert store.claim_pending_message_for_session_sync(other, owner) == 1
    assert admission.recheck(other, 1, owner)
    receipt = result_receipts.prepare_receipt(
        other, owner, 1, f"guest-{other}", b'{"message":"request"}'
    )
    result_receipts.capture_result(
        receipt["id"], receipt["token"], json.dumps(record).encode()
    )
    age_claim(database, other)
    assert store.reclaim_stale_claims_sync() == 1
    assert_unknown(database, other)


def test_an_expired_hold_is_never_held_again_and_settles_unknown(database, monkeypatch):
    """One hold per dispatch: the twelve-hour bound has to actually bound it."""
    record = native_record(artifact=False)
    sid, _requests = lose_the_response(database, monkeypatch, record=record)
    assert_held(database, sid, "invoke_response_lost")
    expire_hold(database, sid)
    age_claim(database, sid)
    # The receipt is still committed and unconsumed, which is exactly what the
    # lease backstop looks for, but the attempt has already had its hold.
    assert store.reclaim_stale_claims_sync() == 1
    assert_unknown(database, sid)
    assert store.find_response_lost_session_ids() == []


def test_a_committed_body_that_can_never_be_adopted_settles_unknown(
    database, monkeypatch
):
    sid, requests = lose_the_response(database, monkeypatch)
    assert_held(database, sid, "invoke_response_lost")
    receipt = json.loads(requests[0].content)["result_receipt"]
    # A committed receipt whose body carries no native terminal outcome can
    # never become one, so waiting out the bound would only pin the permit.
    result_receipts.capture_result(
        receipt["id"],
        receipt["token"],
        json.dumps({"result": "", "terminal_reason": None}).encode(),
    )
    outcome = store.adopt_response_lost_result(sid, ARTIFACT_PATH)
    assert outcome["status"] == "settled"
    assert_unknown(database, sid)


def test_the_sweep_finishes_a_lease_backstop_hold_for_a_lane_with_no_artifact(
    database, monkeypatch
):
    """A drainer or chat turn has no node workflow to recover it."""
    record = native_record(artifact=False)
    sid = queue(database, "kg-lease-backstop", tier="kg")
    owner = "killed-replica"
    assert store.claim_pending_message_for_session_sync(sid, owner) == 1
    assert admission.recheck(sid, 1, owner)
    receipt = result_receipts.prepare_receipt(
        sid, owner, 1, f"guest-{sid}", b'{"message":"request"}'
    )
    result_receipts.capture_result(
        receipt["id"], receipt["token"], json.dumps(record).encode()
    )
    age_claim(database, sid)
    assert store.reclaim_stale_claims_sync() == 0
    assert hold_of(sid)["source"] == "lease_backstop"
    assert mcp._adopt_response_lost_results() == [sid]
    after = snapshot(database, sid)
    assert after["pending"] == []
    assert after["turns"][0]["terminal_reason"] == "end_turn"
    assert after["turns"][0]["result_text"] == record["result"]
    assert [row["state"] for row in after["permits"]] == ["settled"]


def test_a_second_turn_is_held_even_though_the_guest_has_a_last_invoke(
    database, monkeypatch
):
    """last_invoke_at is never cleared, so only the stamps' order says invoking."""
    from swarm import node_workflows

    record = native_record(artifact=False)
    sid = queue(database, "multi-turn-project")
    first = native_record(cost=0.5, artifact=False)

    async def first_handler(request):
        return httpx.Response(200, json=first, request=request)

    fake_http(monkeypatch, first_handler)
    asyncio.run(asyncio.wait_for(mcp._execute_pending_message(sid), 10))
    with Session(database) as db:
        assert store.get_turn(db, sid, 1).terminal_reason == "end_turn"
        store.create_pending_message(db, sid, "second turn", "luna")

    # Turn one completed, so the guest carries a last_invoke_at from it. The
    # second invoke starts after that stamp and is the one whose response is
    # lost.
    async def second_handler(request):
        raise httpx.ReadError("original response lost", request=request)

    requests = fake_http(
        monkeypatch,
        second_handler,
        working_guest(sid, started=STARTED_AT + 5, last=STARTED_AT),
    )
    asyncio.run(asyncio.wait_for(mcp._execute_pending_message(sid), 10))
    state = snapshot(database, sid)
    assert len(state["pending"]) == 1 and state["pending"][0]["seq"] == 2
    second = [row for row in state["turns"] if row["seq"] == 2][0]
    assert second["stop_reason"] == RESPONSE_LOST
    hold = hold_of(sid)
    assert hold["seq"] == 2
    assert hold["invoke_started_at"] == STARTED_AT + 5

    # The node owner reads the same shape and must not settle it either.
    monkeypatch.setattr(
        node_workflows,
        "_observe_held_guest",
        lambda guest: working_guest(sid, started=STARTED_AT + 5, last=STARTED_AT)(),
    )
    assert node_workflows._recover_response_lost({"artifact_path": None}, sid) == {
        "status": "waiting",
        "seq": 2,
    }
    receipt = json.loads(requests[0].content)["result_receipt"]
    result_receipts.capture_result(
        receipt["id"], receipt["token"], json.dumps(record).encode()
    )
    assert store.adopt_response_lost_result(sid)["status"] == "adopted"
    with Session(database) as db:
        assert store.get_turn(db, sid, 2).terminal_reason == "end_turn"
        assert store.get_turn(db, sid, 1).cost_usd == 0.5
    assert len(requests) == 1


def test_a_guest_that_moved_to_another_invocation_settles_unknown(
    database, monkeypatch
):
    from swarm import node_workflows

    sid, _requests = lose_the_response(database, monkeypatch)
    assert_held(database, sid, "invoke_response_lost")
    # Banked and relit: the generation moved while the invoke stamp did not, so
    # the process that was running our invoke is gone.
    monkeypatch.setattr(
        node_workflows,
        "_observe_held_guest",
        lambda guest: working_guest(sid, generation=1)(),
    )
    assert node_workflows._recover_response_lost(
        {"artifact_path": ARTIFACT_PATH}, sid
    ) == {
        "status": "settled",
        "reason": "response_lost_invocation_changed",
    }
    assert_unknown(database, sid)


def test_a_receipt_minted_for_another_request_cannot_finish_a_hold(
    database, monkeypatch
):
    record = native_record()
    sid, requests = lose_the_response(database, monkeypatch, record=record)
    held = assert_held(database, sid, "invoke_response_lost")
    hold = hold_of(sid)
    assert hold["request_sha256"]
    with pytest.raises(
        result_receipts.ReceiptRejected, match="receipt_request_changed"
    ):
        result_receipts.read_held_result({**hold, "request_sha256": "0" * 64})
    assert snapshot(database, sid) == held
    assert store.adopt_response_lost_result(sid, ARTIFACT_PATH)["status"] == "adopted"
    assert len(requests) == 1


def test_the_lifespan_drains_in_flight_executors(database, monkeypatch):
    sid = queue(database)
    posted = asyncio.Event()

    async def handler(_request):
        posted.set()
        await asyncio.Event().wait()

    fake_http(monkeypatch, handler)

    async def run():
        # The same registration _schedule_next_message performs.
        task = asyncio.create_task(mcp._execute_pending_message(sid))
        mcp._inflight_tasks.add(task)
        task.add_done_callback(mcp._inflight_tasks.discard)
        await asyncio.wait_for(posted.wait(), 5)
        assert await mcp.drain_inflight_executors() == 1
        assert await mcp.drain_inflight_executors() == 0

    asyncio.run(asyncio.wait_for(run(), 15))
    assert_held(database, sid, "replica_shutdown")
