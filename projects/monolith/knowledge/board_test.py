from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

import knowledge.board as board
from auth.principal import Authority, Principal, PrincipalKind
from knowledge.models import AgentBoardMessage

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


@pytest.fixture
def board_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent-board.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"knowledge": None}},
    )
    SQLModel.metadata.create_all(engine, tables=[AgentBoardMessage.__table__])
    monkeypatch.setattr(board, "get_engine", lambda: engine)
    monkeypatch.setattr(board, "_now", lambda: NOW)
    yield engine
    engine.dispose()


def principal(subject="kg-agent-sa", groups=("kg-agents",)):
    return Principal(
        subject=subject,
        actor=(),
        scope=(),
        groups=groups,
        email=None,
        kind=PrincipalKind.WORKLOAD,
        authority=Authority.DELEGATED,
    )


def binding(
    principal_id: str,
    *,
    lane="delivery",
    issue=5704,
    subject="kg-agent-sa",
    cross_lane=False,
    mutate=False,
    ledger_owner=None,
):
    return board.TrustedAgentBinding(
        authenticated_subject=subject,
        principal_id=principal_id,
        session_id=f"session-{principal_id}",
        receipt_id="receipt-323",
        task_id="t-323ef3f2-5853-4b90-b3bd-e715ade0dbaf",
        repository="jomcgi-org/homelab",
        branch="factory/t-323ef3f2-5853-4b90-b3bd-e715ade0dbaf",
        worktree="/workspace/src",
        issue=issue,
        pr=6400,
        lane=lane,
        allowed_services=("knowledge",),
        conductor_cross_lane=cross_lane,
        conductor_mutation_authorized=mutate,
        ledger_owner=ledger_owner,
    )


def call(tool, who, trusted, *args, **kwargs):
    with (
        patch.object(board, "current_principal", return_value=who),
        board.trusted_binding_resolver(lambda _principal: trusted),
    ):
        return asyncio.run(tool(*args, **kwargs))


def test_default_off_and_enabled_without_binding_fail_closed(board_db, monkeypatch):
    monkeypatch.delenv(board.BOARD_ENABLED_ENV, raising=False)
    assert call(board.post_message, principal(), binding("agent-a"), "x", "b", 1) == {
        "error": "board_disabled"
    }

    monkeypatch.setenv(board.BOARD_ENABLED_ENV, "true")
    with patch.object(board, "current_principal", return_value=principal()):
        assert asyncio.run(
            board.post_message("claim:issue:jomcgi-org/homelab#5704", "working", 60)
        ) == {"error": "scope_unavailable"}
        assert asyncio.run(board.read_board("distress")) == {
            "error": "scope_unavailable"
        }
        assert asyncio.run(board.ack_message(1)) == {"error": "scope_unavailable"}


def test_claim_and_blocker_scope_has_meaningful_allows_and_denials(
    board_db, monkeypatch
):
    monkeypatch.setenv(board.BOARD_ENABLED_ENV, "true")
    shared = principal()
    agent_a = binding("agent-a")
    agent_b = binding("agent-b")
    outside = binding("agent-c", lane="advisory", issue=6000)
    claim = "claim:issue:jomcgi-org/homelab#5704"
    blocker = "blocker:lane:delivery"
    service_blocker = "blocker:service:knowledge"

    posted = call(board.post_message, shared, agent_a, claim, "working", 60)
    assert posted["status"] == "posted"
    assert (
        call(board.post_message, shared, agent_a, blocker, "hold", 60)["status"]
        == "posted"
    )
    assert (
        call(
            board.post_message,
            shared,
            agent_a,
            service_blocker,
            "service hold",
            60,
        )["status"]
        == "posted"
    )

    read = call(board.read_board, shared, agent_b, claim, None)
    assert [message["body"] for message in read["messages"]] == ["working"]
    assert read["classification"] == "untrusted"
    assert read["messages"][0]["untrusted"] is True
    assert read["messages"][0]["principal"] == "agent-a"
    assert read["messages"][0]["provenance"]["authenticated_subject"] == "kg-agent-sa"
    assert read["messages"][0]["expires_at"]
    assert read["provenance"]["receipt_id"] == "receipt-323"
    blocker_read = call(board.read_board, shared, agent_b, blocker, None)
    assert [message["body"] for message in blocker_read["messages"]] == ["hold"]

    assert call(board.read_board, shared, outside, claim, None) == {
        "error": "scope_denied"
    }
    assert call(board.post_message, shared, outside, blocker, "squat", 60) == {
        "error": "scope_denied"
    }
    assert call(
        board.post_message,
        shared,
        agent_a,
        "blocker:service:payments",
        "squat",
        60,
    ) == {"error": "scope_denied"}
    assert call(board.post_message, shared, agent_a, "distress", "page", 60) == {
        "error": "scope_denied"
    }


def test_explicit_conductor_cross_lane_read_and_mutation_ledger_gate(
    board_db, monkeypatch
):
    monkeypatch.setenv(board.BOARD_ENABLED_ENV, "true")
    shared = principal()
    call(
        board.post_message,
        shared,
        binding("agent-a"),
        "blocker:lane:delivery",
        "delivery held",
        60,
    )
    call(
        board.mirror_distress,
        shared,
        binding("agent-a"),
        raw_id="raw-delivery",
        summary="delivery distress",
        severity="blocked",
        details="delivery detail",
        requested_intervention="inspect delivery",
    )
    call(
        board.mirror_distress,
        shared,
        binding("agent-c", lane="advisory", issue=6000),
        raw_id="raw-advisory",
        summary="advisory distress",
        severity="blocked",
        details="advisory detail",
        requested_intervention="inspect advisory",
    )
    call(
        board.post_message,
        shared,
        binding("agent-c", lane="advisory", issue=6000),
        "blocker:lane:advisory",
        "advisory held",
        60,
    )

    conductor = principal("factory-conductor", ("factory-conductor",))
    scoped = binding("conductor-1", subject="factory-conductor", cross_lane=True)
    read = call(board.read_board, conductor, scoped, "blocker", None)
    assert {message["topic"] for message in read["messages"]} == {
        "blocker:lane:delivery",
        "blocker:lane:advisory",
    }
    distress = call(board.read_board, conductor, scoped, "distress", None)
    assert {message["topic"] for message in distress["messages"]} == {
        "distress:lane:delivery",
        "distress:lane:advisory",
    }
    assert call(
        board.post_message,
        conductor,
        scoped,
        "blocker:lane:delivery",
        "mutation",
        60,
    ) == {"error": "mutation_authorization_unavailable"}

    authorized = binding(
        "conductor-1",
        subject="factory-conductor",
        cross_lane=True,
        mutate=True,
        ledger_owner="conductor-1",
    )
    assert (
        call(
            board.post_message,
            conductor,
            authorized,
            "blocker:lane:delivery",
            "authorized mutation",
            60,
        )["status"]
        == "posted"
    )


def test_ack_is_scoped_non_leaking_and_preserves_concurrent_readers(
    board_db, monkeypatch
):
    monkeypatch.setenv(board.BOARD_ENABLED_ENV, "true")
    shared = principal()
    agent_a = binding("agent-a")
    message_id = call(
        board.post_message,
        shared,
        agent_a,
        "blocker:lane:delivery",
        "hold",
        60,
    )["id"]
    outside = binding("agent-c", lane="advisory", issue=6000)
    assert call(board.ack_message, shared, outside, message_id) == {
        "error": "message_unavailable"
    }
    assert call(board.ack_message, shared, outside, 999999) == {
        "error": "message_unavailable"
    }

    readers = [binding("agent-b"), binding("agent-d")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda trusted: board._ack_sync(
                    principal=shared,
                    binding=trusted,
                    message_id=message_id,
                    now=NOW,
                ),
                readers,
            )
        )
    assert {result["status"] for result in results} == {"acknowledged"}
    with Session(board_db) as session:
        row = session.get(AgentBoardMessage, message_id)
        assert row is not None
        assert row.acknowledged_by == ["agent-b", "agent-d"]


def test_exact_expiry_and_bounds(board_db, monkeypatch):
    monkeypatch.setenv(board.BOARD_ENABLED_ENV, "true")
    shared = principal()
    trusted = binding("agent-a")
    topic = "blocker:lane:delivery"
    with Session(board_db) as session:
        session.add(
            AgentBoardMessage(
                principal="agent-a",
                authenticated_subject="kg-agent-sa",
                topic=topic,
                body="expires exactly now",
                created_at=NOW - timedelta(seconds=60),
                expires_at=NOW,
            )
        )
        session.add(
            AgentBoardMessage(
                principal="agent-a",
                authenticated_subject="kg-agent-sa",
                topic=topic,
                body="still active",
                created_at=NOW - timedelta(seconds=1),
                expires_at=NOW + timedelta(microseconds=1),
            )
        )
        session.commit()
    read = call(board.read_board, shared, trusted, topic, None)
    assert [message["body"] for message in read["messages"]] == ["still active"]
    assert call(board.post_message, shared, trusted, topic, "bad", 0)["error"] == (
        "invalid_ttl"
    )
    assert call(
        board.post_message,
        shared,
        trusted,
        topic,
        "x" * (board.MAX_BODY_CHARS + 1),
        60,
    ) == {"error": "invalid_body"}


def test_read_cap_keeps_newest_messages_in_chronological_order(
    board_db, monkeypatch
):
    monkeypatch.setenv(board.BOARD_ENABLED_ENV, "true")
    topic = "blocker:lane:delivery"
    with Session(board_db) as session:
        session.add_all(
            [
                AgentBoardMessage(
                    principal="agent-a",
                    authenticated_subject="kg-agent-sa",
                    topic=topic,
                    body=f"message-{index}",
                    created_at=NOW + timedelta(microseconds=index),
                    expires_at=NOW + timedelta(seconds=60),
                )
                for index in range(board.MAX_READ_MESSAGES + 1)
            ]
        )
        session.commit()

    read = call(board.read_board, principal(), binding("agent-a"), topic, None)
    assert len(read["messages"]) == board.MAX_READ_MESSAGES
    assert read["messages"][0]["body"] == "message-1"
    assert read["messages"][-1]["body"] == f"message-{board.MAX_READ_MESSAGES}"


def test_distress_mirror_is_attributable_idempotent_and_lane_scoped(
    board_db, monkeypatch
):
    monkeypatch.setenv(board.BOARD_ENABLED_ENV, "true")
    shared = principal()
    trusted = binding("agent-a")
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    kwargs = {
        "raw_id": "raw-distress-1",
        "summary": f"blocked with {secret}",
        "severity": "blocked",
        "details": f"dependency leaked {secret}",
        "requested_intervention": f"rotate {secret}",
    }
    first = call(board.mirror_distress, shared, trusted, **kwargs)
    second = call(board.mirror_distress, shared, trusted, **kwargs)
    assert first["status"] == "posted"
    assert second == {"id": first["id"], "status": "already_mirrored"}
    read = call(board.read_board, shared, binding("agent-b"), "distress", None)
    assert len(read["messages"]) == 1
    assert read["messages"][0]["principal"] == "agent-a"
    assert secret not in read["messages"][0]["body"]
    assert read["messages"][0]["body"].count("[REDACTED:github_token]") == 3
    assert read["messages"][0]["provenance"] == {
        "source": "distress_mirror",
        "source_id": "distress:raw-distress-1",
        "authenticated_subject": "kg-agent-sa",
    }
    assert (
        call(
            board.read_board,
            shared,
            binding("agent-c", lane="advisory", issue=6000),
            "distress",
            None,
        )["messages"]
        == []
    )
