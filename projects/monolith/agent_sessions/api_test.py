import asyncio

import pytest
from faas.embervm_client import EmberVMTransportError
from knowledge import recall
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine

import agent_sessions.execution_api as api
from agent_sessions import mcp
from agent_sessions.models import AgentSession
from agent_sessions.transport import EmberSessionGone, Turn


def _completed_synthetic_turn() -> Turn:
    return Turn(
        result="synthetic ok",
        terminal_reason="completed",
        stop_reason="end_turn",
        is_error=False,
        permission_denials=[],
        num_turns=1,
        session_id="cli-session",
        usage={},
        total_cost_usd=0.01,
        duration_ms=100,
        activities=[],
        model=None,
    )


@pytest.fixture
def synthetic_claim(monkeypatch):
    monkeypatch.setattr(api, "_synthetic_dispatch_count_sync", lambda *_args: 3)


def test_run_synthetic_session_claims_pending_before_deliver(
    monkeypatch, synthetic_claim
):
    row = AgentSession(
        id=41,
        local_session_id="codex-synthetic-test",
        workspace="<guest>",
        branch="main",
    )
    turn = _completed_synthetic_turn()
    delivered = []
    deleted = []
    released = []

    async def deliver(*args, **kwargs):
        delivered.append((args, kwargs))
        return turn, None

    monkeypatch.setattr(api, "_persist_session", lambda *args, **kwargs: row)
    monkeypatch.setattr(api, "_persist_pending_message", lambda *args: 1)
    monkeypatch.setattr(
        api, "_claim_pending_message_sync", lambda session_id, claim_owner: 1
    )
    monkeypatch.setattr(api._transport, "deliver", deliver)
    monkeypatch.setattr(api, "_persist_turn_from_pending_sync", lambda *args: None)
    monkeypatch.setattr(
        api,
        "_delete_pending_message_sync",
        lambda session_id, turn_seq: deleted.append((session_id, turn_seq)),
    )
    monkeypatch.setattr(
        api,
        "_release_pending_message_claim_sync",
        lambda session_id, turn_seq, claim_owner, cause="observer_released", **_kwargs: (
            released.append((session_id, turn_seq))
        ),
    )

    result = asyncio.run(api.run_synthetic_session("probe"))

    assert result is turn
    assert len(delivered) == 1
    assert callable(delivered[0][1]["admission_check"])
    assert delivered[0][1]["agent_session_id"] == 41
    assert delivered[0][1]["dispatch_count"] == 3
    assert deleted == [(41, 1)]
    assert released == [(41, 1)]


def test_run_synthetic_session_persists_actual_guest_model(
    monkeypatch, synthetic_claim
):
    row = AgentSession(
        id=46,
        local_session_id="codex-synthetic-test",
        workspace="<guest>",
        branch="main",
    )
    turn = _completed_synthetic_turn()._replace(model="terra")
    persisted = []

    async def deliver(*args, **kwargs):
        return turn, None

    monkeypatch.setattr(api, "_persist_session", lambda *args, **kwargs: row)
    monkeypatch.setattr(api, "_persist_pending_message", lambda *args: 1)
    monkeypatch.setattr(
        api, "_claim_pending_message_sync", lambda session_id, claim_owner: 1
    )
    monkeypatch.setattr(api._transport, "deliver", deliver)
    monkeypatch.setattr(
        api,
        "_persist_turn_from_pending_sync",
        lambda *args: persisted.append(args),
    )
    monkeypatch.setattr(api, "_delete_pending_message_sync", lambda *args: None)
    monkeypatch.setattr(
        api, "_release_pending_message_claim_sync", lambda *args, **kwargs: None
    )

    result = asyncio.run(api.run_synthetic_session("probe", model="luna"))

    assert result is turn
    assert persisted[0][7] == "terra"
    assert persisted[0][-1] == 3


def test_run_synthetic_session_does_not_deliver_when_claim_lost(monkeypatch):
    row = AgentSession(
        id=42,
        local_session_id="codex-synthetic-test",
        workspace="<guest>",
        branch="main",
    )
    delivered = []
    deleted = []

    async def deliver(*args, **kwargs):
        delivered.append((args, kwargs))
        return _completed_synthetic_turn(), None

    monkeypatch.setattr(api, "_persist_session", lambda *args, **kwargs: row)
    monkeypatch.setattr(api, "_persist_pending_message", lambda *args: 1)
    monkeypatch.setattr(
        api, "_claim_pending_message_sync", lambda session_id, claim_owner: None
    )
    monkeypatch.setattr(api._transport, "deliver", deliver)
    monkeypatch.setattr(
        api,
        "_delete_pending_message_sync",
        lambda session_id, turn_seq: deleted.append((session_id, turn_seq)),
    )

    result = asyncio.run(api.run_synthetic_session("probe"))

    assert result is None
    assert delivered == []
    assert deleted == []


def test_run_synthetic_session_aborts_when_claim_stolen_mid_deliver(
    monkeypatch, synthetic_claim
):
    row = AgentSession(
        id=45,
        local_session_id="codex-synthetic-test",
        workspace="<guest>",
        branch="main",
    )
    turn = _completed_synthetic_turn()
    delivered = []
    deleted = []
    persisted = []
    refresh_calls = []
    real_sleep = asyncio.sleep

    async def sleep(delay):
        assert delay == 10
        await real_sleep(0)

    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    def refresh_claim(session_id, turn_seq, claim_owner):
        refresh_calls.append((session_id, turn_seq, claim_owner))
        return False

    async def deliver(*args, **kwargs):
        delivered.append((args, kwargs))
        while not refresh_calls:
            await real_sleep(0)
        await real_sleep(0)
        return turn, None

    monkeypatch.setattr(api, "_persist_session", lambda *args, **kwargs: row)
    monkeypatch.setattr(api, "_persist_pending_message", lambda *args: 1)
    monkeypatch.setattr(
        api, "_claim_pending_message_sync", lambda session_id, claim_owner: 1
    )
    monkeypatch.setattr(api, "_refresh_claim_sync", refresh_claim)
    monkeypatch.setattr(api.asyncio, "sleep", sleep)
    monkeypatch.setattr(api.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(api._transport, "deliver", deliver)
    monkeypatch.setattr(
        api,
        "_persist_turn_from_pending_sync",
        lambda *args: persisted.append(args),
    )
    monkeypatch.setattr(
        api,
        "_delete_pending_message_sync",
        lambda session_id, turn_seq: deleted.append((session_id, turn_seq)),
    )
    monkeypatch.setattr(
        api,
        "_release_pending_message_claim_sync",
        lambda session_id, turn_seq, claim_owner, cause="observer_released", **_kwargs: (
            None
        ),
    )

    result = asyncio.run(api.run_synthetic_session("probe"))

    assert result is None
    assert len(delivered) == 1
    assert len(refresh_calls) == 1
    assert persisted == []
    assert deleted == []


def test_run_synthetic_session_does_not_assume_integrity_error_means_duplicate(
    monkeypatch,
    synthetic_claim,
):
    row = AgentSession(
        id=43,
        local_session_id="codex-synthetic-test",
        workspace="<guest>",
        branch="main",
    )
    turn = _completed_synthetic_turn()
    deleted = []
    released = []

    async def deliver(*args, **kwargs):
        return turn, None

    def persist_turn(*args):
        raise IntegrityError("INSERT", {}, Exception("duplicate seq"))

    monkeypatch.setattr(api, "_persist_session", lambda *args, **kwargs: row)
    monkeypatch.setattr(api, "_persist_pending_message", lambda *args: 1)
    monkeypatch.setattr(
        api, "_claim_pending_message_sync", lambda session_id, claim_owner: 1
    )
    monkeypatch.setattr(api._transport, "deliver", deliver)
    monkeypatch.setattr(api, "_persist_turn_from_pending_sync", persist_turn)
    monkeypatch.setattr(
        api,
        "_delete_pending_message_sync",
        lambda session_id, turn_seq: deleted.append((session_id, turn_seq)),
    )
    monkeypatch.setattr(
        api,
        "_release_pending_message_claim_sync",
        lambda session_id, turn_seq, claim_owner, cause="observer_released", **_kwargs: (
            released.append((session_id, turn_seq))
        ),
    )

    with pytest.raises(IntegrityError):
        asyncio.run(api.run_synthetic_session("probe"))

    assert deleted == []
    assert released == [(43, 1)]


def test_run_synthetic_session_refreshes_claim_and_delivers_once_when_lease_would_expire(
    monkeypatch,
    synthetic_claim,
):
    row = AgentSession(
        id=44,
        local_session_id="codex-synthetic-test",
        workspace="<guest>",
        branch="main",
    )
    turn = _completed_synthetic_turn()
    delivered = []
    deleted = []
    refresh_calls = []
    sleep_calls = 0
    elapsed = 0
    fourth_sleep = asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep(delay):
        nonlocal elapsed, sleep_calls
        assert delay == 10
        sleep_calls += 1
        if sleep_calls > 3:
            await fourth_sleep.wait()
        else:
            elapsed += delay
            await real_sleep(0)

    def refresh_claim(session_id, turn_seq, replica_id):
        refresh_calls.append((session_id, turn_seq, replica_id))
        return True

    async def deliver(*args, **kwargs):
        delivered.append((args, kwargs))
        while len(refresh_calls) < 3:
            await real_sleep(0)
        return turn, None

    monkeypatch.setattr(api, "_persist_session", lambda *args, **kwargs: row)
    monkeypatch.setattr(api, "_persist_pending_message", lambda *args: 1)
    monkeypatch.setattr(
        api, "_claim_pending_message_sync", lambda session_id, claim_owner: 1
    )
    monkeypatch.setattr(api, "_refresh_claim_sync", refresh_claim)
    monkeypatch.setattr(api.asyncio, "sleep", sleep)
    monkeypatch.setattr(api._transport, "deliver", deliver)
    monkeypatch.setattr(api, "_persist_turn_from_pending_sync", lambda *args: None)
    monkeypatch.setattr(
        api,
        "_delete_pending_message_sync",
        lambda session_id, turn_seq: deleted.append((session_id, turn_seq)),
    )
    monkeypatch.setattr(
        api,
        "_release_pending_message_claim_sync",
        lambda session_id, turn_seq, claim_owner, cause="observer_released", **_kwargs: (
            None
        ),
    )

    result = asyncio.run(api.run_synthetic_session("probe"))

    assert result is turn
    assert elapsed == 30
    assert len(delivered) == 1
    assert len(refresh_calls) == 3
    assert {(session_id, turn_seq) for session_id, turn_seq, _ in refresh_calls} == {
        (44, 1)
    }
    owners = {owner for _, _, owner in refresh_calls}
    assert len(owners) == 1
    assert owners.pop().startswith(f"{api._REPLICA_ID}:")
    assert deleted == [(44, 1)]


def test_start_session_for_swarm_retry_preserves_original_workflow_id(
    monkeypatch, tmp_path
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api_test.db'}",
        connect_args={"check_same_thread": False},
    )
    schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(api, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp, "get_engine", lambda: engine)
    monkeypatch.setattr(api, "_schedule_next_message", lambda session_id: None)

    try:
        first_id = api.start_session_for_swarm(
            "test-key",
            "prompt1",
            "luna",
            "jomcgi-org/homelab",
            "main",
            workflow_id="wf-1",
            node_key="implement",
            node_attempt=2,
            reasoning=True,
        )
        second_id = api.start_session_for_swarm(
            "test-key",
            "prompt1",
            "luna",
            "jomcgi-org/homelab",
            "main",
            workflow_id="wf-2",
        )

        assert second_id == first_id
        with Session(engine) as session:
            row = session.get(AgentSession, first_id)
            assert row is not None
            assert row.workflow_id == "wf-1"
            assert row.node_key == "implement"
            assert row.node_attempt == 2
            assert row.reasoning is True
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in schemas:
                table.schema = schemas[table.name]


def test_persist_session_attaches_recall_to_system_prompt(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recall_attach_test.db'}",
        connect_args={"check_same_thread": False},
    )
    captured = {}

    def create_session(_session, *args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return AgentSession(
            id=51,
            local_session_id=args[0],
            workspace=args[1],
            branch=args[2],
        )

    monkeypatch.setattr(mcp, "get_engine", lambda: engine)
    monkeypatch.setattr(recall, "recall_block", lambda _prompt: "fixed recall")
    monkeypatch.setattr(mcp.store, "create_session", create_session)

    mcp._persist_session(
        "local-recall",
        "<guest>",
        "main",
        "luna",
        "jomcgi-org/homelab",
        system_prompt="base prompt",
        prompt="the first user task is long enough",
        node_key="implement",
    )

    assert captured["kwargs"]["system_prompt"] == "base prompt\n\nfixed recall"
    assert captured["kwargs"]["node_key"] == "implement"


def test_start_session_for_swarm_passes_first_prompt_to_persistence(
    monkeypatch, tmp_path
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'swarm_recall_test.db'}",
        connect_args={"check_same_thread": False},
    )
    captured = {}

    def persist(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return AgentSession(
            id=52,
            local_session_id=args[0],
            workspace=args[1],
            branch=args[2],
        )

    monkeypatch.setattr(api, "_persist_session", persist)
    monkeypatch.setattr(api, "_persist_pending_message", lambda *_args: 1)
    monkeypatch.setattr(api, "_schedule_next_message", lambda _session_id: None)
    monkeypatch.setattr(api, "get_engine", lambda: engine)
    monkeypatch.setattr(api.store, "get_session_by_local_id", lambda *_args: None)

    session_id = api.start_session_for_swarm(
        "swarm-recall",
        "the first swarm task prompt",
        "luna",
        "jomcgi-org/homelab",
        "main",
    )

    assert session_id == 52
    assert captured["kwargs"]["prompt"] == "the first swarm task prompt"


@pytest.mark.asyncio
async def test_reap_sessions_for_workflow_skips_reaps_and_continues_on_failure(
    monkeypatch,
):
    rows = [
        AgentSession(id=1, local_session_id="skip", workspace="w", branch="b"),
        AgentSession(
            id=2,
            local_session_id="good",
            workspace="w",
            branch="b",
            ember_session_id="ember-good",
        ),
        AgentSession(
            id=3,
            local_session_id="bad",
            workspace="w",
            branch="b",
            ember_session_id="ember-bad",
        ),
    ]
    destroyed = []
    cleared = []

    async def destroy(ember_id):
        destroyed.append(ember_id)
        if ember_id == "ember-bad":
            raise EmberVMTransportError("control plane unavailable")
        return {"session_id": ember_id, "state": "destroying"}

    async def get_session(ember_id):
        return {"session_id": ember_id, "state": "destroyed"}

    monkeypatch.setattr(
        api, "_begin_guest_cleanup", lambda *_args: {"claim_id": "cleanup-claim"}
    )
    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: rows)
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(api._transport, "get_session", get_session)
    monkeypatch.setattr(
        api,
        "_finish_guest_cleanup",
        lambda sid, guest, workflow, claim: cleared.append(guest) or sid == 2,
    )

    assert await api.reap_sessions_for_workflow("wf-1") == {
        "reaped": [2],
        "failed": [{"session_id": 3, "error": "control plane unavailable"}],
        "skipped": [1],
        "pending": [],
    }
    assert destroyed == ["ember-good", "ember-bad"]
    assert cleared == ["ember-good"]


@pytest.mark.asyncio
async def test_reap_sessions_for_workflow_treats_404_as_reaped(monkeypatch):
    row = AgentSession(
        id=1,
        local_session_id="gone",
        workspace="w",
        branch="b",
        ember_session_id="ember-gone",
    )

    async def destroy(_ember_id):
        raise EmberSessionGone("404 session not found")

    async def get_session(_ember_id):
        raise AssertionError("no confirmation read after DELETE Gone")

    cleared = []
    monkeypatch.setattr(
        api, "_begin_guest_cleanup", lambda *_args: {"claim_id": "cleanup-claim"}
    )
    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: [row])
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(api._transport, "get_session", get_session)
    monkeypatch.setattr(
        api,
        "_finish_guest_cleanup",
        lambda sid, guest, workflow, claim: cleared.append(guest) or sid == 1,
    )

    assert await api.reap_sessions_for_workflow("wf-1") == {
        "reaped": [1],
        "failed": [],
        "skipped": [],
        "pending": [],
    }
    assert cleared == ["ember-gone"]


@pytest.mark.asyncio
async def test_reap_does_not_treat_a_500_mentioning_404_as_gone(monkeypatch):
    """A live session must never be reported reaped.

    The transport decides "gone" from the STATUS CODE. A plain transport error
    whose text merely contains "404" (the session id itself can, and error
    bodies routinely mention other not-found resources) has to count as a
    FAILURE, otherwise the binding is cleared while the VM keeps burning a
    live-capacity slot with nothing pointing at it.
    """
    row = AgentSession(
        id=7,
        local_session_id="live",
        workspace="w",
        branch="b",
        ember_session_id="s-404ABCDEF",
    )
    cleared = []

    async def destroy(_ember_id):
        raise EmberVMTransportError(
            "Server error '500 Internal Server Error' for url "
            "'http://embervm/v1/sessions/s-404ABCDEF' not found upstream"
        )

    monkeypatch.setattr(
        api, "_begin_guest_cleanup", lambda *_args: {"claim_id": "cleanup-claim"}
    )
    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: [row])
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(
        api,
        "_finish_guest_cleanup",
        lambda sid, guest, workflow, claim: cleared.append(guest) or sid == 1,
    )

    result = await api.reap_sessions_for_workflow("wf-1")

    assert result["reaped"] == []
    assert result["failed"][0]["session_id"] == 7
    assert result["pending"] == []
    assert cleared == []


@pytest.mark.asyncio
async def test_workflow_reap_retains_guest_with_unknown_outcome(monkeypatch):
    row = AgentSession(
        id=2448,
        local_session_id="held",
        workspace="w",
        branch="main",
        ember_session_id="guest-retain",
    )
    destroyed, cleared = [], []

    async def destroy(guest_id):
        destroyed.append(guest_id)

    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: [row])
    monkeypatch.setattr(api, "_begin_guest_cleanup", lambda *_args: {"hold": "unknown"})
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(
        api,
        "_finish_guest_cleanup",
        lambda sid, guest, workflow, claim: cleared.append(guest) or sid == 1,
    )
    assert await api.reap_sessions_for_workflow("held-workflow") == {
        "reaped": [],
        "failed": [],
        "skipped": [2448],
        "pending": [],
    }
    assert destroyed == []
    assert cleared == []


def _reap_row(session_id=1, ember_id="ember-1"):
    return AgentSession(
        id=session_id,
        local_session_id=f"local-{session_id}",
        workspace="w",
        branch="b",
        ember_session_id=ember_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["destroying", "parked", "running", "DESTROYED", " destroyed"]
)
async def test_reap_retains_binding_when_confirmation_state_is_unconfirmed(
    monkeypatch, state
):
    row = _reap_row()
    destroyed, cleared, reads = [], [], []

    async def destroy(ember_id):
        destroyed.append(ember_id)
        return {"session_id": ember_id, "state": "destroying"}

    async def get_session(ember_id):
        reads.append(ember_id)
        return {"session_id": ember_id, "state": state}

    monkeypatch.setattr(
        api, "_begin_guest_cleanup", lambda *_args: {"claim_id": "cleanup-claim"}
    )
    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: [row])
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(api._transport, "get_session", get_session)
    monkeypatch.setattr(
        api,
        "_finish_guest_cleanup",
        lambda sid, guest, workflow, claim: cleared.append(guest) or sid == 1,
    )

    assert await api.reap_sessions_for_workflow("wf-1") == {
        "reaped": [],
        "failed": [],
        "skipped": [],
        "pending": [1],
    }
    assert destroyed == ["ember-1"]
    assert reads == ["ember-1"]
    assert cleared == []


@pytest.mark.asyncio
async def test_reap_clears_binding_when_confirmation_read_is_gone(monkeypatch):
    row = _reap_row()
    cleared = []

    async def destroy(_ember_id):
        return {"session_id": "ember-1", "state": "destroying"}

    async def get_session(_ember_id):
        raise EmberSessionGone("404 session not found")

    monkeypatch.setattr(
        api, "_begin_guest_cleanup", lambda *_args: {"claim_id": "cleanup-claim"}
    )
    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: [row])
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(api._transport, "get_session", get_session)
    monkeypatch.setattr(
        api,
        "_finish_guest_cleanup",
        lambda sid, guest, workflow, claim: cleared.append(guest) or sid == 1,
    )

    assert await api.reap_sessions_for_workflow("wf-1") == {
        "reaped": [1],
        "failed": [],
        "skipped": [],
        "pending": [],
    }
    assert cleared == ["ember-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["destroyed", "expired", "evicted", "failed"])
async def test_reap_clears_binding_when_confirmation_state_is_terminal(
    monkeypatch, state
):
    row = _reap_row()
    cleared = []

    async def destroy(_ember_id):
        return {"session_id": "ember-1", "state": "destroying"}

    async def get_session(ember_id):
        return {"session_id": ember_id, "state": state}

    monkeypatch.setattr(
        api, "_begin_guest_cleanup", lambda *_args: {"claim_id": "cleanup-claim"}
    )
    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: [row])
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(api._transport, "get_session", get_session)
    monkeypatch.setattr(
        api,
        "_finish_guest_cleanup",
        lambda sid, guest, workflow, claim: cleared.append(guest) or sid == 1,
    )

    assert await api.reap_sessions_for_workflow("wf-1") == {
        "reaped": [1],
        "failed": [],
        "skipped": [],
        "pending": [],
    }
    assert cleared == ["ember-1"]


@pytest.mark.asyncio
async def test_reap_retains_binding_on_mismatched_identity(monkeypatch):
    row = _reap_row()
    cleared = []

    async def destroy(_ember_id):
        return {"session_id": "ember-1", "state": "destroying"}

    async def get_session(_ember_id):
        return {"session_id": "ember-other", "state": "destroyed"}

    monkeypatch.setattr(
        api, "_begin_guest_cleanup", lambda *_args: {"claim_id": "cleanup-claim"}
    )
    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: [row])
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(api._transport, "get_session", get_session)
    monkeypatch.setattr(
        api,
        "_finish_guest_cleanup",
        lambda sid, guest, workflow, claim: cleared.append(guest) or sid == 1,
    )

    assert await api.reap_sessions_for_workflow("wf-1") == {
        "reaped": [],
        "failed": [],
        "skipped": [],
        "pending": [1],
    }
    assert cleared == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observed",
    [None, [], {"unexpected": "shape"}, {"session_id": "ember-1", "state": []}],
)
async def test_reap_retains_binding_on_malformed_confirmation(monkeypatch, observed):
    row = _reap_row()
    cleared = []

    async def destroy(_ember_id):
        return {"session_id": "ember-1", "state": "destroying"}

    async def get_session(_ember_id):
        return observed

    monkeypatch.setattr(
        api, "_begin_guest_cleanup", lambda *_args: {"claim_id": "cleanup-claim"}
    )
    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: [row])
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(api._transport, "get_session", get_session)
    monkeypatch.setattr(
        api,
        "_finish_guest_cleanup",
        lambda sid, guest, workflow, claim: cleared.append(guest) or sid == 1,
    )

    assert await api.reap_sessions_for_workflow("wf-1") == {
        "reaped": [],
        "failed": [],
        "skipped": [],
        "pending": [1],
    }
    assert cleared == []


@pytest.mark.asyncio
async def test_reap_does_not_clear_when_confirmation_read_fails(monkeypatch):
    row = _reap_row()
    cleared = []

    async def destroy(_ember_id):
        return {"session_id": "ember-1", "state": "destroying"}

    async def get_session(_ember_id):
        raise EmberVMTransportError("500 destroy read failed")

    monkeypatch.setattr(
        api, "_begin_guest_cleanup", lambda *_args: {"claim_id": "cleanup-claim"}
    )
    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: [row])
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(api._transport, "get_session", get_session)
    monkeypatch.setattr(
        api,
        "_finish_guest_cleanup",
        lambda sid, guest, workflow, claim: cleared.append(guest) or sid == 1,
    )

    result = await api.reap_sessions_for_workflow("wf-1")

    assert result["reaped"] == []
    assert result["pending"] == []
    assert result["failed"][0]["session_id"] == 1
    assert cleared == []


@pytest.mark.asyncio
async def test_reap_http_202_retains_binding_until_later_authoritative_absence(
    monkeypatch,
):
    import httpx

    from agent_sessions import transport

    row = _reap_row()
    cleared, requests = [], []
    reads = 0

    async def handler(request):
        nonlocal reads
        requests.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(
                202, json={"session_id": "ember-1", "state": "destroying"}
            )
        reads += 1
        if reads == 1:
            return httpx.Response(
                200, json={"session_id": "ember-1", "state": "destroying"}
            )
        return httpx.Response(404, json={"error": "session not found"})

    real_client = httpx.AsyncClient
    mock_transport = httpx.MockTransport(handler)
    monkeypatch.setattr(transport, "EMBERVM_URL", "https://ember.test")
    monkeypatch.setattr(transport, "auth_headers", dict)
    monkeypatch.setattr(
        transport.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=mock_transport, **kwargs),
    )
    monkeypatch.setattr(
        api, "_begin_guest_cleanup", lambda *_args: {"claim_id": "cleanup-claim"}
    )
    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: [row])
    monkeypatch.setattr(
        api,
        "_finish_guest_cleanup",
        lambda sid, guest, workflow, claim: cleared.append(guest) or sid == 1,
    )

    first = await api.reap_sessions_for_workflow("wf-1")
    assert first["pending"] == [1]
    assert first["reaped"] == []
    assert cleared == []

    second = await api.reap_sessions_for_workflow("wf-1")
    assert second == {"reaped": [1], "pending": [], "failed": [], "skipped": []}
    assert cleared == ["ember-1"]
    assert (
        requests
        == [
            ("DELETE", "/v1/sessions/ember-1"),
            ("GET", "/v1/sessions/ember-1"),
        ]
        * 2
    )


def test_public_capacity_facade_is_lightweight_and_execution_exports_are_identical():
    import os
    import subprocess
    import sys

    script = """
import sys
import agent_sessions.api as public

assert callable(public.lock_capacity_pool)
assert callable(public.lock_cessation_session)
assert callable(public.confirm_reconciled_guest_cessation)
assert public.KG_NODE_KEY == "kg-drain"
for module in (
    "agent_sessions.execution_api", "agent_sessions.mcp", "agent_sessions.store",
    "agent_sessions.transport", "goosecracker.api",
):
    assert module not in sys.modules, module

# Access through the public boundary must retain the real function object,
# including its signature, coroutine kind and implementation globals.
first = public.run_synthetic_session
from agent_sessions import execution_api
assert first is execution_api.run_synthetic_session
for name in (
    "start_session_for_swarm", "send_to_swarm_session", "reap_sessions_for_workflow",
    "start_session_for_thread", "send_to_thread_session", "session_id_for_thread",
):
    assert getattr(public, name) is getattr(execution_api, name), name
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
