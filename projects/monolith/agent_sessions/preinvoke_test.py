"""Hermetic production-path checks for failures before model dispatch."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from agent import routine_jobs
from core import db as core_db
from knowledge import burst
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select
from swarm import drainer

from agent_sessions import (
    admission,
    execution_api,
    mcp,
    result_receipts,
    store,
    transport,
)
from agent_sessions.constants import KG_NODE_KEY, UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
    PendingMessage,
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'preinvoke.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
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
    with Session(engine) as db, db.begin():
        db.execute(
            text("""
                CREATE TABLE routine_jobs (
                    name TEXT PRIMARY KEY, routine_kind TEXT, interval_secs INTEGER,
                    next_run_at TEXT, last_run_at TEXT, last_status TEXT,
                    last_summary TEXT, locked_by TEXT, locked_at TEXT,
                    ttl_secs INTEGER, payload TEXT, created_by TEXT, created_at TEXT
                )
            """)
        )
        db.execute(
            text("CREATE TABLE raw_inputs (raw_id TEXT PRIMARY KEY, source TEXT)")
        )
    for module in (
        admission,
        store,
        mcp,
        core_db,
        routine_jobs,
        execution_api,
        result_receipts,
    ):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    # This fixture has no operator burst grant. Eligibility and admission still
    # execute against the real database, including the rolling daily count.
    monkeypatch.setattr(burst, "kg_burst_state", lambda _db: burst.KGBurstState())
    monkeypatch.setattr(drainer, "_turn_has_unknown_outcome_lookup", None)
    monkeypatch.setenv("AGENT_RESULT_RECEIPTS_ENABLED", "false")
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _sid: None)
    mcp._negative_oracle_verdicts.clear()

    async def unexpected_notify(*_args, **_kwargs):
        pytest.fail("a failed delivery must not emit a successful turn notification")

    monkeypatch.setattr(mcp.agent_api, "notify", unexpected_notify)
    yield engine
    mcp._negative_oracle_verdicts.clear()
    engine.dispose()


def _queue(engine, *, key="queued", bound=False):
    with Session(engine) as db:
        agent = store.create_session(db, key, "<guest>", "main", "luna")
        sid = agent.id
        if bound:
            store.set_ember_session(db, sid, "existing-guest", "guest-token", None)
        store.create_pending_message(db, sid, "original work", "luna")
        return sid


def _claim(engine, sid, owner="original-executor"):
    assert store.claim_pending_message_for_session_sync(sid, owner) == 1
    assert admission.recheck(sid, 1, owner, "claude-runtime")
    with Session(engine) as db:
        pending = store.get_pending_message(db, sid, 1)
        return pending.dispatch_count


def _snapshot(engine, sid):
    with Session(engine) as db:
        agent = db.get(AgentSession, sid)
        permit = admission.reservation(db, agent.local_session_id)
        return {
            "session": agent.model_dump(),
            "pending": [
                row.model_dump()
                for row in db.exec(
                    select(PendingMessage).where(PendingMessage.session_id == sid)
                )
            ],
            "turns": [
                row.model_dump()
                for row in db.exec(select(AgentTurn).where(AgentTurn.session_id == sid))
            ],
            "permit": permit.model_dump() if permit is not None else None,
        }


def _http(monkeypatch, handler):
    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            return await handler(httpx.Request("POST", url, **kwargs))

        async def delete(self, url, **kwargs):
            return await handler(httpx.Request("DELETE", url, **kwargs))

    monkeypatch.setattr(transport.httpx, "AsyncClient", Client)
    monkeypatch.setattr(transport, "EMBERVM_URL", "https://ember.test")
    monkeypatch.setattr(transport, "auth_headers", dict)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(mcp, "_transport", client)
    monkeypatch.setattr(execution_api, "_transport", client)


def _claim_scout(workflow, *, cap=40):
    return drainer.claim_drainer_job.__wrapped__(2100, ["kg-drain"], workflow, cap, 0)


def _synthetic_id(engine):
    with Session(engine) as db:
        agent = db.exec(select(AgentSession)).one()
        assert agent.local_session_id.startswith("synthetic:")
        assert agent.admission_tier == "probe"
        return agent.id


def _probe_response(request):
    if request.url.path == "/v1/workloads/claude-runtime/sessions":
        return httpx.Response(
            201,
            json={"session_id": "probe-guest", "session_token": "probe-token"},
            request=request,
        )
    if request.method == "DELETE":
        return httpx.Response(200, json={"state": "destroyed"}, request=request)
    assert request.url.path == "/v1/sessions/probe-guest/invoke"
    assert request.headers["X-Ember-Guest-Path"] == "/shim/turn"
    return httpx.Response(
        200,
        json={
            "result": "synthetic ok",
            "terminal_reason": "completed",
            "session_id": "probe-cli",
            "total_cost_usd": 0.01,
        },
        request=request,
    )


@pytest.mark.parametrize("failure", ["create", "callback", "admission"])
def test_synthetic_preinvoke_failure_settles_without_guest_cleanup(
    database, monkeypatch, failure
):
    requests = []

    async def handler(request):
        requests.append((request.method, request.url.path))
        assert request.url.path == "/v1/workloads/claude-runtime/sessions"
        if failure == "create":
            return httpx.Response(
                503,
                json={"error": "create refused", "retryable": False},
                request=request,
            )
        if failure == "admission":
            # Change the real permit's workload fence after VM creation. The
            # next real admission recheck must refuse the model POST.
            with Session(database) as db, db.begin():
                permit = db.exec(select(AgentCapacityReservation)).one()
                permit.workload = "another-runtime"
                db.add(permit)
        return _probe_response(request)

    if failure == "callback":

        def failed_database_write(*_args):
            raise OSError("binding storage unavailable")

        monkeypatch.setattr(
            execution_api, "_persist_synthetic_binding_sync", failed_database_write
        )
    _http(monkeypatch, handler)
    with pytest.raises(transport.EmberTurnNotInvoked):
        asyncio.run(execution_api.run_synthetic_session("probe"))
    state = _snapshot(database, _synthetic_id(database))
    assert requests == [("POST", "/v1/workloads/claude-runtime/sessions")]
    assert state["pending"] == []
    assert state["permit"]["state"] == "settled"
    assert state["permit"]["outcome"] == "not_invoked"
    turn = state["turns"][0]
    assert turn["terminal_reason"] == "error"
    assert turn["cost_usd"] is None
    recovery = json.loads(turn["usage_json"])["recovery"]
    assert recovery["invocation_phase"] == "not_invoked"
    assert recovery["dispatch_count"] == 1
    assert recovery["claim_owner"] == state["permit"]["owner"]
    assert state["session"]["ember_session_id"] == (
        "probe-guest" if failure == "admission" else None
    )


@pytest.mark.parametrize("stage", ["create", "model_response", "create_failure"])
@pytest.mark.parametrize("changed", ["owner", "count"])
def test_synthetic_stale_attempt_never_overwrites_binding_result_or_cleanup(
    database, monkeypatch, stage, changed
):
    before = None
    requests = []

    async def handler(request):
        nonlocal before
        requests.append((request.method, request.url.path))
        assert request.method != "DELETE"
        creating = request.url.path.endswith("/sessions")
        if creating == (stage != "model_response"):
            sid = _synthetic_id(database)
            with Session(database) as db, db.begin():
                pending = store.get_pending_message(db, sid, 1)
                permit = db.exec(select(AgentCapacityReservation)).one()
                agent = db.get(AgentSession, sid)
                if changed == "owner":
                    pending.claimed_by_replica = "replacement-executor"
                    permit.owner = "replacement-executor"
                else:
                    pending.dispatch_count += 1
                agent.ember_session_id = "replacement-guest"
                agent.ember_session_token = "replacement-token"
                db.add_all([pending, permit, agent])
            before = _snapshot(database, sid)
            if stage == "create_failure":
                return httpx.Response(
                    503,
                    json={"error": "create refused", "retryable": False},
                    request=request,
                )
        return _probe_response(request)

    _http(monkeypatch, handler)
    if stage == "create_failure":
        with pytest.raises(transport.EmberTurnNotInvoked):
            asyncio.run(execution_api.run_synthetic_session("probe"))
    else:
        assert asyncio.run(execution_api.run_synthetic_session("probe")) is None
    assert before is not None
    assert _snapshot(database, _synthetic_id(database)) == before
    assert len(requests) == (2 if stage == "model_response" else 1)


@pytest.mark.parametrize("cancel", [False, True])
def test_synthetic_post_start_failure_retains_binding_and_uncertain_permit(
    database, monkeypatch, cancel
):
    requests = []

    async def handler(request):
        requests.append(request.method)
        assert request.method != "DELETE"
        if request.url.path.endswith("/invoke"):
            if cancel:
                raise asyncio.CancelledError()
            raise httpx.ReadError("model response lost", request=request)
        return _probe_response(request)

    _http(monkeypatch, handler)
    expected = asyncio.CancelledError if cancel else transport.EmberVMTransportError
    with pytest.raises(expected) as raised:
        asyncio.run(execution_api.run_synthetic_session("probe"))
    assert not isinstance(raised.value, transport.EmberTurnNotInvoked)
    state = _snapshot(database, _synthetic_id(database))
    assert requests == ["POST", "POST"]
    assert state["permit"]["state"] == "uncertain"
    assert state["session"]["ember_session_id"] == "probe-guest"
    assert state["pending"] == []
    assert state["turns"][0]["cost_usd"] is None
    if cancel:
        assert state["turns"][0]["stop_reason"] == UNKNOWN_INVOCATION


def test_synthetic_cancelled_creation_retains_unknown_hold(database, monkeypatch):
    async def handler(request):
        assert request.url.path == "/v1/workloads/claude-runtime/sessions"
        raise asyncio.CancelledError()

    _http(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(execution_api.run_synthetic_session("probe"))
    state = _snapshot(database, _synthetic_id(database))
    assert state["permit"]["state"] == "uncertain"
    assert state["turns"][0]["stop_reason"] == UNKNOWN_INVOCATION
    assert state["session"]["ember_session_id"] is None


@pytest.mark.parametrize("error_type", [OSError, transport.EmberTurnNotInvoked])
def test_synthetic_failed_result_persistence_does_not_destroy_guest(
    database, monkeypatch, error_type
):
    requests = []

    async def handler(request):
        requests.append(request.method)
        assert request.method != "DELETE"
        return _probe_response(request)

    def failed_database_write(*_args):
        raise error_type("result storage unavailable")

    _http(monkeypatch, handler)
    monkeypatch.setattr(
        execution_api, "_persist_turn_from_pending_sync", failed_database_write
    )
    with pytest.raises(error_type, match="result storage unavailable"):
        asyncio.run(execution_api.run_synthetic_session("probe"))
    state = _snapshot(database, _synthetic_id(database))
    assert requests == ["POST", "POST"]
    assert state["permit"]["state"] == "uncertain"
    assert state["session"]["ember_session_id"] == "probe-guest"
    assert state["turns"][0]["stop_reason"] == UNKNOWN_INVOCATION
    assert json.loads(state["turns"][0]["usage_json"])["recovery"]["cause"] == (
        "result_persistence_failed"
    )


@pytest.mark.parametrize("capture", [False, True])
def test_synthetic_success_persists_before_normal_cleanup_and_pins_receipt(
    database, monkeypatch, capture
):
    requests = []
    receipt = None
    monkeypatch.setenv("AGENT_RESULT_RECEIPTS_ENABLED", str(capture).lower())

    async def handler(request):
        nonlocal receipt
        requests.append(request.method)
        if request.url.path.endswith("/invoke"):
            payload = json.loads(request.content)
            if capture:
                receipt = payload["result_receipt"]
            else:
                assert "result_receipt" not in payload
        if request.method == "DELETE":
            state = _snapshot(database, _synthetic_id(database))
            assert state["pending"] == []
            assert state["permit"]["state"] == "settled"
            assert state["turns"][0]["result_text"] == "synthetic ok"
        return _probe_response(request)

    _http(monkeypatch, handler)
    turn = asyncio.run(execution_api.run_synthetic_session("probe"))
    assert turn.result == "synthetic ok"
    state = _snapshot(database, _synthetic_id(database))
    assert requests == ["POST", "POST", "DELETE"]
    assert state["session"]["ember_session_id"] is None
    assert state["turns"][0]["cost_usd"] == 0.01
    with Session(database) as db:
        rows = db.exec(select(AgentResultReceipt)).all()
        assert len(rows) == int(capture)
        if capture:
            assert rows[0].id == receipt["id"]
            assert rows[0].session_id == state["session"]["id"]
            assert rows[0].seq == rows[0].dispatch_count == 1
            assert rows[0].claim_owner == state["permit"]["owner"]
            assert rows[0].guest_id == "probe-guest"


def test_synthetic_receipt_mint_failure_before_model_post_is_not_invoked(
    database, monkeypatch
):
    monkeypatch.setenv("AGENT_RESULT_RECEIPTS_ENABLED", "true")
    requests = []

    async def handler(request):
        requests.append(request.method)
        assert request.url.path.endswith("/sessions")
        with Session(database) as db, db.begin():
            pending = db.exec(select(PendingMessage)).one()
            pending.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            db.add(pending)
        return _probe_response(request)

    _http(monkeypatch, handler)
    with pytest.raises(transport.EmberTurnNotInvoked):
        asyncio.run(execution_api.run_synthetic_session("probe"))
    state = _snapshot(database, _synthetic_id(database))
    assert requests == ["POST"]
    assert state["permit"]["outcome"] == "not_invoked"
    assert state["permit"]["state"] == "settled"
    assert state["session"]["ember_session_id"] == "probe-guest"


def test_real_create_failure_settles_exact_scout_and_preserves_interval(
    database, monkeypatch
):
    with Session(database) as db, db.begin():
        db.execute(
            text("""
                INSERT INTO routine_jobs
                    (name, routine_kind, interval_secs, next_run_at, ttl_secs, payload)
                VALUES ('kg-repo-diff', 'kg-drain', 3600,
                        datetime(CURRENT_TIMESTAMP, '-1 second'), 2100, :payload)
            """),
            {
                "payload": json.dumps(
                    {"mode": "repo-diff", "last_sha": "original-cursor"}
                )
            },
        )
    job = _claim_scout("first-cycle")
    assert job is not None
    local_id = drainer._session_key("first-cycle", job["name"], KG_NODE_KEY)
    with Session(database) as db:
        agent = store.create_session(
            db,
            local_id,
            "<guest>",
            "main",
            "luna",
            workflow_id="first-cycle",
            node_key=KG_NODE_KEY,
            admission_tier="kg",
        )
        sid = agent.id
        store.create_pending_message(db, sid, "inspect repository changes", "luna")
    requests = []

    async def create_fails(request):
        requests.append(request.url.path)
        assert request.url.path == "/v1/workloads/claude-runtime/sessions"
        return httpx.Response(
            503,
            json={"error": "no slot available", "retryable": False},
            request=request,
        )

    _http(monkeypatch, create_fails)
    asyncio.run(mcp._execute_pending_message(sid))
    state = _snapshot(database, sid)
    assert requests == ["/v1/workloads/claude-runtime/sessions"]
    assert state["pending"] == []
    assert state["session"]["ember_session_id"] is None
    assert state["permit"]["state"] == "settled"
    assert state["permit"]["outcome"] == "not_invoked"
    turn = state["turns"][0]
    assert turn["terminal_reason"] == "error"
    assert turn["cost_usd"] is None
    assert "no slot available" in turn["result_text"]
    recovery = json.loads(turn["usage_json"])["recovery"]
    assert recovery["invocation_phase"] == "not_invoked"
    assert recovery["dispatch_count"] == 1
    assert recovery["claim_owner"] == state["permit"]["owner"]
    assert not drainer._turn_has_unknown_outcome(turn, sid)
    with pytest.raises(RuntimeError) as raised:
        drainer._completed_output(turn, sid)
    assert not isinstance(raised.value, drainer.InvocationOutcomeUnknown)

    assert drainer.finish_drainer_job.__wrapped__(
        job["name"], "error", turn["result_text"], expected_holder=job["locked_by"]
    )
    with Session(database) as db:
        scout = db.execute(text("SELECT * FROM routine_jobs")).mappings().one()
        assert scout["last_status"] == "error"
        assert scout["locked_by"] is None
        assert json.loads(scout["payload"])["last_sha"] == "original-cursor"
        assert (
            datetime.fromisoformat(scout["next_run_at"])
            - datetime.fromisoformat(scout["last_run_at"])
        ) == timedelta(seconds=3600)
        assert admission.reserved_routine_jobs(db) == set()
    assert _claim_scout("too-early") is None
    with Session(database) as db, db.begin():
        db.execute(
            text("""
                UPDATE routine_jobs SET next_run_at=datetime(CURRENT_TIMESTAMP, '-1 second'),
                    last_run_at=datetime(CURRENT_TIMESTAMP, '-3601 seconds')
            """)
        )
    # A recovered scout still cannot spend beyond the normal rolling daily cap.
    assert _claim_scout("daily-bound", cap=1) is None
    again = _claim_scout("next-cycle")
    assert again is not None and again["name"] == job["name"]
    assert _claim_scout("duplicate-cycle") is None


def test_real_model_post_failure_keeps_hold_even_if_binding_disappears(
    database, monkeypatch
):
    sid = _queue(database, bound=True)
    requests = []

    async def response_lost(request):
        requests.append(request.url.path)
        assert request.headers["X-Ember-Guest-Path"] == "/shim/turn"
        with Session(database) as db, db.begin():
            agent = db.get(AgentSession, sid)
            agent.ember_session_id = None
            agent.ember_session_token = None
            db.add(agent)
        raise httpx.ReadError("no slot available", request=request)

    _http(monkeypatch, response_lost)
    asyncio.run(mcp._execute_pending_message(sid))
    state = _snapshot(database, sid)
    assert requests == ["/v1/sessions/existing-guest/invoke"]
    assert state["session"]["ember_session_id"] is None
    assert state["permit"]["state"] == "uncertain"
    assert state["permit"]["outcome"] == "delivery_error"
    assert state["turns"][0]["cost_usd"] is None
    assert (
        "invocation_phase"
        not in json.loads(state["turns"][0]["usage_json"])["recovery"]
    )
    assert drainer._turn_has_unknown_outcome(state["turns"][0], sid)


def test_exact_not_invoked_writer_preserves_bound_guest_and_partial_evidence(database):
    sid = _queue(database, bound=True)
    count = _claim(database, sid)
    with Session(database) as db, db.begin():
        pending = store.get_pending_message(db, sid, 1)
        pending.partial_text = "original partial evidence"
        pending.partial_activities = '[{"verb":"checking"}]'
        db.add(pending)
    before = _snapshot(database, sid)
    store.mark_turn_error_sync(
        sid,
        1,
        "binding persistence failed",
        "original-executor",
        invocation_not_attempted=True,
        dispatch_count=count,
    )
    after = _snapshot(database, sid)
    assert after["session"]["ember_session_id"] == before["session"]["ember_session_id"]
    assert (
        after["session"]["ember_session_token"]
        == before["session"]["ember_session_token"]
    )
    assert after["permit"]["state"] == "settled"
    assert after["permit"]["outcome"] == "not_invoked"
    assert after["turns"][0]["result_text"] == "original partial evidence"
    assert after["turns"][0]["cost_usd"] is None
    recovery = json.loads(after["turns"][0]["usage_json"])["recovery"]
    assert recovery["partial_activities"] == '[{"verb":"checking"}]'
    assert recovery["invocation_phase"] == "not_invoked"


@pytest.mark.parametrize(
    "mismatch",
    [
        "claim_owner",
        "dispatch_count",
        "permit_owner",
        "permit_session",
        "permit_uncertain",
        "permit_settled",
        "permit_missing",
    ],
)
def test_not_invoked_evidence_cannot_settle_a_different_owner(database, mismatch):
    sid = _queue(database, bound=True)
    count = _claim(database, sid)
    owner = "original-executor"
    if mismatch == "claim_owner":
        owner = "stale-executor"
    elif mismatch == "dispatch_count":
        count += 1
    else:
        with Session(database) as db, db.begin():
            agent = db.get(AgentSession, sid)
            permit = admission.reservation(db, agent.local_session_id)
            if mismatch == "permit_owner":
                permit.owner = "new-permit-owner"
            elif mismatch == "permit_session":
                permit.session_id = sid + 1
            elif mismatch == "permit_missing":
                db.delete(permit)
            else:
                permit.state = mismatch.removeprefix("permit_")
            if mismatch != "permit_missing":
                db.add(permit)
    before = _snapshot(database, sid)
    store.mark_turn_error_sync(
        sid,
        1,
        "no slot available",
        owner,
        invocation_not_attempted=True,
        dispatch_count=count,
    )
    assert _snapshot(database, sid) == before


@pytest.mark.parametrize(
    ("owner", "count"), [(None, 1), ("", 1), ("original-executor", None)]
)
def test_not_invoked_evidence_requires_explicit_dispatch_identity(
    database, owner, count
):
    sid = _queue(database)
    _claim(database, sid)
    before = _snapshot(database, sid)
    with pytest.raises(ValueError):
        store.mark_turn_error_sync(
            sid,
            1,
            "create failed",
            owner,
            invocation_not_attempted=True,
            dispatch_count=count,
        )
    assert _snapshot(database, sid) == before


def test_prior_unknown_turn_cannot_be_overwritten_or_settled(database):
    sid = _queue(database, bound=True)
    count = _claim(database, sid)
    assert store.finish_unknown_pending_sync(
        sid, 1, "original-executor", count, "executor_cancelled"
    )
    before = _snapshot(database, sid)
    assert before["turns"][0]["stop_reason"] == UNKNOWN_INVOCATION
    store.mark_turn_error_sync(
        sid,
        1,
        "create failed",
        "original-executor",
        invocation_not_attempted=True,
        dispatch_count=count,
    )
    assert _snapshot(database, sid) == before

    # Even an inconsistent leftover pending row must not turn historical UNKNOWN
    # evidence into a new grant to settle the original permit.
    with Session(database) as db, db.begin():
        db.add(
            PendingMessage(
                session_id=sid,
                seq=1,
                message_text="original work",
                claimed_by_replica="original-executor",
                dispatch_count=count,
                claimed_at=datetime.now(timezone.utc),
            )
        )
    store.mark_turn_error_sync(
        sid,
        1,
        "create failed",
        "original-executor",
        invocation_not_attempted=True,
        dispatch_count=count,
    )
    after = _snapshot(database, sid)
    assert after["turns"] == before["turns"]
    assert after["permit"] == before["permit"]
    assert after["session"] == before["session"]


def test_executor_cancellation_before_create_retains_unknown_hold(
    database, monkeypatch
):
    sid = _queue(database)

    async def cancelled_create(request):
        assert request.url.path == "/v1/workloads/claude-runtime/sessions"
        raise asyncio.CancelledError()

    _http(monkeypatch, cancelled_create)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mcp._execute_pending_message(sid))
    state = _snapshot(database, sid)
    assert state["permit"]["state"] == "uncertain"
    assert state["turns"][0]["stop_reason"] == UNKNOWN_INVOCATION
    assert state["turns"][0]["cost_usd"] is None
    assert state["pending"] == []
