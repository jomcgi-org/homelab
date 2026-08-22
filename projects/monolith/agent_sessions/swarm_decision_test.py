from __future__ import annotations

import asyncio

import pytest
from sqlmodel import Session, create_engine

from agent_sessions import mcp
from auth.dependencies import reset_current_principal, set_current_principal
from auth.principal import Authority, Principal, PrincipalKind, anonymous_principal
from swarm import store as swarm_store
from swarm.models import SwarmDecision


@pytest.fixture(name="engine")
def engine_fixture(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent_run_decide_test.db'}",
        connect_args={"check_same_thread": False},
    )
    table = SwarmDecision.__table__
    schema = table.schema
    table.schema = None
    try:
        table.create(engine)
        monkeypatch.setattr(mcp, "get_engine", lambda: engine)
        yield engine
    finally:
        table.schema = schema


def _principal() -> Principal:
    return Principal(
        subject="user:alice",
        actor=(),
        scope=(),
        groups=(),
        email="alice@example.com",
        kind=PrincipalKind.HUMAN,
        authority=Authority.DELEGATED,
    )


def _run_as(principal: Principal, awaitable):
    token = set_current_principal(principal)
    try:
        return asyncio.run(awaitable)
    finally:
        reset_current_principal(token)


def _open_decision(engine):
    with Session(engine) as session:
        swarm_store.open_decision(
            session,
            "wf-1",
            "push_gate",
            "push_gate",
            ["approve", "send_back"],
            "Approve the unverified branch?",
        )


def test_agent_run_decide_records_principal_and_returns_row_shape(engine):
    _open_decision(engine)

    result = _run_as(
        _principal(),
        mcp.monolith_agent_run_decide(
            "wf-1", "push_gate", "approve", "Verified manually."
        ),
    )

    assert result == {
        "workflow_id": "wf-1",
        "node_key": "push_gate",
        "decision": "approve",
        "decided_at": result["decided_at"],
        "actor_subject": "user:alice",
        "idempotent": False,
    }
    assert result["decided_at"] is not None
    with Session(engine) as session:
        row = session.get(SwarmDecision, 1)
        assert row.decision_note == "Verified manually."
        assert row.actor_subject == "user:alice"
        assert row.actor_authority == "delegated"


def test_agent_run_decide_repeat_is_idempotent(engine):
    _open_decision(engine)

    first = _run_as(
        _principal(),
        mcp.monolith_agent_run_decide("wf-1", "push_gate", "send_back"),
    )
    second = _run_as(
        _principal(),
        mcp.monolith_agent_run_decide("wf-1", "push_gate", "send_back"),
    )

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["decided_at"] == first["decided_at"]


def test_agent_run_decide_returns_rejection_without_open_row(engine):
    result = _run_as(
        _principal(),
        mcp.monolith_agent_run_decide("wf-1", "push_gate", "approve"),
    )

    assert result == {
        "accepted": False,
        "error": "no open decision for this node",
    }


def test_agent_run_decide_returns_allowed_options_for_invalid_value(engine):
    _open_decision(engine)

    result = _run_as(
        _principal(),
        mcp.monolith_agent_run_decide("wf-1", "push_gate", "maybe"),
    )

    assert result["accepted"] is False
    assert "approve" in result["error"]
    assert "send_back" in result["error"]


def test_agent_run_decide_refuses_anonymous_before_store_access(monkeypatch):
    monkeypatch.setattr(
        mcp,
        "_record_swarm_decision",
        lambda *args: (_ for _ in ()).throw(AssertionError("store was touched")),
    )

    result = _run_as(
        anonymous_principal(),
        mcp.monolith_agent_run_decide("wf-1", "push_gate", "approve"),
    )

    assert result == {
        "accepted": False,
        "error": "an identified caller is required to decide",
    }


def test_agent_run_decide_rejects_note_over_2000_characters(engine):
    _open_decision(engine)

    result = _run_as(
        _principal(),
        mcp.monolith_agent_run_decide("wf-1", "push_gate", "approve", "x" * 2001),
    )

    assert result == {
        "accepted": False,
        "error": "note must be at most 2000 characters",
    }
    with Session(engine) as session:
        assert swarm_store.get_open_decision(session, "wf-1", "push_gate") is not None
