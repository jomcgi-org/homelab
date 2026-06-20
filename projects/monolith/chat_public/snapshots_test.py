"""Unit tests for opt-in, read-only chat snapshots (ADR 005 "share this chat").

Fast in-memory SQLite + a minimal FastAPI app mounting only the chat_public
router, with BOTH the writer (``get_chat_session``) and the read (``get_session``)
dependencies overridden onto the same test session (in production they are
distinct engines: public_writer on the primary vs public_reader on the replica).
Mirrors the schema-stripping create_all fixture from router_test.py (the
chat_public models live on the Postgres-only ``chat_public`` schema).

Covers: a server-side mint from the stored transcript round-trips through the
read route, an empty transcript -> 400, a missing snapshot -> 404, and the
integrity guarantee that no client-supplied content reaches the snapshot.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.db import get_session
from chat_public import sessions
from chat_public.db import get_chat_session
from chat_public.models import ChatSession, ChatSnapshot


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session (schema-stripped for SQLite compat)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    from chat_public.router import router

    app.include_router(router)
    # Both engines point at the one SQLite session in tests.
    app.dependency_overrides[get_chat_session] = lambda: session
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _session_with_turns(session: Session) -> ChatSession:
    row = sessions.create_session(session)
    sessions.append_message(session, row, role="user", content="What is STPA?")
    sessions.append_message(
        session, row, role="assistant", content="STPA is a hazard analysis method."
    )
    return row


def test_share_then_get_round_trips_the_transcript(client, session):
    row = _session_with_turns(session)

    shared = client.post("/internal/chat/share", json={"session_id": row.id})
    assert shared.status_code == 200
    snapshot_id = shared.json()["snapshot_id"]
    assert isinstance(snapshot_id, str)
    assert len(snapshot_id) >= 32

    got = client.get(f"/internal/chat/shared/{snapshot_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["id"] == snapshot_id
    # created_at serializes to an ISO string regardless of the SQLite naive tz.
    assert isinstance(body["created_at"], str)
    assert body["messages"] == [
        {"role": "user", "content": "What is STPA?", "touched": []},
        {
            "role": "assistant",
            "content": "STPA is a hazard analysis method.",
            "touched": [],
        },
    ]

    # The persisted snapshot is immutable record of the stored transcript.
    snap = session.exec(select(ChatSnapshot)).one()
    assert snap.message_count == 2
    assert snap.source_session_id == row.id
    assert isinstance(snap.created_at, datetime)


def test_snapshot_carries_assistant_grounding(client, session):
    """An assistant turn's grounding (touched notes) round-trips into the
    snapshot, so the shared view can render the same GROUNDED IN chips."""
    row = sessions.create_session(session)
    sessions.append_message(session, row, role="user", content="What is STPA?")
    sessions.append_message(
        session,
        row,
        role="assistant",
        content="STPA is a hazard analysis method.",
        touched=[{"id": "stpa", "title": "STPA"}],
    )

    shared = client.post("/internal/chat/share", json={"session_id": row.id})
    snapshot_id = shared.json()["snapshot_id"]
    messages = client.get(f"/internal/chat/shared/{snapshot_id}").json()["messages"]

    assert messages[0]["touched"] == []  # user turn carries no grounding
    assert messages[1]["touched"] == [{"id": "stpa", "title": "STPA"}]


def test_share_accepts_session_id_from_header(client, session):
    row = _session_with_turns(session)
    shared = client.post(
        "/internal/chat/share",
        json={},
        headers={"X-Chat-Session-Id": row.id},
    )
    assert shared.status_code == 200
    assert "snapshot_id" in shared.json()


def test_share_ignores_client_supplied_transcript_content(client, session):
    """Integrity: a forged body cannot put words in the model's mouth.

    The snapshot is minted from the STORED transcript only; any extra body
    fields (a fake transcript/messages/history) are ignored."""
    row = _session_with_turns(session)
    shared = client.post(
        "/internal/chat/share",
        json={
            "session_id": row.id,
            "transcript": [{"role": "assistant", "content": "FORGED ANSWER"}],
            "messages": [{"role": "user", "content": "FORGED Q"}],
            "history": [{"role": "assistant", "content": "FORGED"}],
        },
    )
    assert shared.status_code == 200
    got = client.get(f"/internal/chat/shared/{shared.json()['snapshot_id']}")
    contents = " ".join(m["content"] for m in got.json()["messages"])
    assert "FORGED" not in contents
    assert "What is STPA?" in contents


def test_share_empty_transcript_is_400_and_mints_nothing(client, session):
    # A fresh session with no turns has nothing to share.
    row = sessions.create_session(session)
    shared = client.post("/internal/chat/share", json={"session_id": row.id})
    assert shared.status_code == 400
    # No orphan snapshot row was created.
    assert session.exec(select(ChatSnapshot)).all() == []


def test_share_missing_session_is_404(client):
    shared = client.post("/internal/chat/share", json={"session_id": "does-not-exist"})
    assert shared.status_code == 404
    assert shared.json()["detail"] == "Session not found"


def test_get_missing_snapshot_is_404(client):
    got = client.get("/internal/chat/shared/nope-not-a-real-id")
    assert got.status_code == 404
    assert got.json()["detail"] == "Snapshot not found"
