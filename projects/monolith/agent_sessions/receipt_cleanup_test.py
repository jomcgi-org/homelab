"""Hermetic coverage for durable cleanup of receipt-won guest holds."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine

from agent_sessions import admission, execution_api, result_receipts, store
from agent_sessions.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
    PendingMessage,
)

NOW = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'receipt-cleanup.db'}",
        connect_args={"check_same_thread": False, "timeout": 3},
        execution_options={"schema_translate_map": {"agent_sessions": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            AgentCapacityPool.__table__,
            AgentCapacityReservation.__table__,
            AgentSession.__table__,
            AgentTurn.__table__,
            PendingMessage.__table__,
            AgentResultReceipt.__table__,
        ],
    )
    for module in (admission, execution_api, result_receipts, store):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def held_receipt(
    engine,
    *,
    session_id=1,
    received=True,
    fenced=True,
    observed=False,
    accept_until=NOW + timedelta(hours=13),
):
    receipt_id = f"{session_id:032x}"
    guest_id = f"guest-{session_id}"
    with Session(engine) as db, db.begin():
        db.add(
            AgentSession(
                id=session_id,
                local_session_id=f"factory:cleanup:{session_id}",
                workspace="<guest>",
                branch="factory/cleanup",
                workflow_id="workflow-cleanup",
                ember_session_id=guest_id,
                ember_session_token="guest-token",
                ember_lineage_id="lineage-one",
                cli_session_id="cli-one",
                result_receipt_fence_id=receipt_id if fenced else None,
            )
        )
        db.add(
            AgentResultReceipt(
                id=receipt_id,
                token_sha256=f"token-{session_id}",
                session_id=session_id,
                local_session_id=f"factory:cleanup:{session_id}",
                seq=1,
                dispatch_count=1,
                claim_owner="executor-one",
                guest_id=guest_id,
                request_sha256="a" * 64,
                created_at=NOW,
                accept_until=accept_until,
                retain_until=NOW + timedelta(days=7),
                received_at=NOW if received else None,
                response_observed_at=NOW if observed else None,
                result_sha256="b" * 64 if received else None,
                result_body=b"{}" if received else None,
            )
        )
    return receipt_id, guest_id


def state(engine, session_id=1):
    with Session(engine) as db:
        session = db.get(AgentSession, session_id)
        receipt = db.get(AgentResultReceipt, f"{session_id:032x}")
        return session.model_dump(), receipt.model_dump()


def terminal_transport(monkeypatch, *, mutate=None):
    destroyed = []

    async def destroy(guest_id):
        destroyed.append(guest_id)
        if mutate is not None:
            mutate()

    async def terminal(guest_id):
        return {"session_id": guest_id, "state": "destroyed"}

    monkeypatch.setattr(execution_api._transport, "destroy_session", destroy)
    monkeypatch.setattr(execution_api._transport, "get_session", terminal)
    return destroyed


def test_never_observed_response_reaps_at_deadline_without_faking_observation(
    database, monkeypatch
):
    receipt_id, guest_id = held_receipt(database)
    destroyed = terminal_transport(monkeypatch)

    before = state(database)
    assert asyncio.run(
        execution_api.reap_held_receipt_guests(now=NOW + timedelta(hours=12))
    ) == {"reaped": [], "pending": [], "failed": []}
    assert state(database) == before
    assert destroyed == []

    assert asyncio.run(
        execution_api.reap_held_receipt_guests(now=NOW + timedelta(hours=13))
    ) == {"reaped": [1], "pending": [], "failed": []}
    assert destroyed == [guest_id]
    session, receipt = state(database)
    assert session["ember_session_id"] is None
    assert session["result_receipt_fence_id"] is None
    assert session["prior_ember_lineage_id"] == "lineage-one"
    assert session["prior_cli_session_id"] == "cli-one"
    assert receipt["response_observed_at"] is None

    assert asyncio.run(
        execution_api.reap_held_receipt_guests(now=NOW + timedelta(days=1))
    ) == {"reaped": [], "pending": [], "failed": []}
    assert destroyed == [guest_id]
    assert receipt_id == f"{1:032x}"


def test_workflow_reaper_can_release_an_expired_received_fence(database, monkeypatch):
    _receipt_id, guest_id = held_receipt(database, accept_until=NOW)
    destroyed = terminal_transport(monkeypatch)

    assert asyncio.run(
        execution_api.reap_sessions_for_workflow(
            "workflow-cleanup", now=NOW + timedelta(seconds=1)
        )
    ) == {
        "reaped": [1],
        "pending": [],
        "failed": [],
        "skipped": [],
    }
    assert destroyed == [guest_id]


def test_unreceived_live_invoke_is_never_a_cleanup_candidate(database, monkeypatch):
    _receipt_id, guest_id = held_receipt(database, received=False, fenced=False)
    with Session(database) as db, db.begin():
        db.add(
            PendingMessage(
                session_id=1,
                seq=1,
                message_text="still invoking",
                claimed_by_replica="executor-one",
                claimed_at=NOW,
                last_dispatch_at=NOW,
                dispatch_count=1,
            )
        )
    destroyed = terminal_transport(monkeypatch)

    assert asyncio.run(
        execution_api.reap_held_receipt_guests(now=NOW + timedelta(days=1))
    ) == {"reaped": [], "pending": [], "failed": []}
    assert asyncio.run(
        execution_api.reap_sessions_for_workflow("workflow-cleanup")
    ) == {
        "reaped": [],
        "pending": [1],
        "failed": [],
        "skipped": [],
    }
    assert destroyed == []
    assert state(database)[0]["ember_session_id"] == guest_id


def test_failed_destroy_retains_exact_hold_for_idempotent_retry(database, monkeypatch):
    receipt_id, guest_id = held_receipt(database, observed=True)

    async def fail(_guest_id):
        raise RuntimeError("control plane unavailable")

    monkeypatch.setattr(execution_api._transport, "destroy_session", fail)
    held = state(database)
    assert asyncio.run(
        execution_api.reap_held_receipt_guests(receipt_id=receipt_id, now=NOW)
    ) == {
        "reaped": [],
        "pending": [],
        "failed": [{"session_id": 1, "error": "RuntimeError"}],
    }
    assert state(database) == held

    destroyed = terminal_transport(monkeypatch)
    assert asyncio.run(
        execution_api.reap_held_receipt_guests(receipt_id=receipt_id, now=NOW)
    )["reaped"] == [1]
    assert destroyed == [guest_id]


def test_confirmation_never_clears_a_replacement_guest(database, monkeypatch):
    receipt_id, old_guest = held_receipt(database, observed=True)

    def replace_binding():
        with Session(database) as db, db.begin():
            session = db.get(AgentSession, 1)
            session.ember_session_id = "replacement-guest"
            session.ember_session_token = "replacement-token"
            session.result_receipt_fence_id = "replacement-receipt"
            db.add(session)

    destroyed = terminal_transport(monkeypatch, mutate=replace_binding)
    assert asyncio.run(
        execution_api.reap_held_receipt_guests(receipt_id=receipt_id, now=NOW)
    ) == {"reaped": [], "pending": [1], "failed": []}
    session, _receipt = state(database)
    assert destroyed == [old_guest]
    assert session["ember_session_id"] == "replacement-guest"
    assert session["ember_session_token"] == "replacement-token"
    assert session["result_receipt_fence_id"] == "replacement-receipt"
