"""Fork must not resurrect a snapshot whose takedown committed on the primary.

Production reads go to two engines: public_writer on the primary and
public_reader on a replica that can lag. Here two separate in-memory SQLite
databases stand in for them, so "taken down on the primary, still on the
replica" is a deterministic state, not a timing window. A fake siteverify can
also apply the takedown while the fork is awaiting Turnstile admission.
"""

from __future__ import annotations

import pytest
from core.db import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat_public import sessions, snapshots, turnstile
from chat_public.db import get_chat_session
from chat_public.models import ChatMessage, ChatSession, ChatSnapshot


def _engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.fixture(name="dbs")
def dbs_fixture():
    """(primary, replica) sessions on two independent SQLite databases."""
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        primary_engine, replica_engine = _engine(), _engine()
        SQLModel.metadata.create_all(primary_engine)
        SQLModel.metadata.create_all(replica_engine)
        with Session(primary_engine) as primary, Session(replica_engine) as replica:
            yield primary, replica
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture(name="client")
def client_fixture(dbs):
    primary, replica = dbs
    app = FastAPI()
    from chat_public.router import router

    app.include_router(router)
    app.dependency_overrides[get_chat_session] = lambda: primary
    app.dependency_overrides[get_session] = lambda: replica
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _mint(db: Session) -> ChatSnapshot:
    row = sessions.create_session(db)
    sessions.append_message(db, row, role="user", content="private details")
    sessions.append_message(db, row, role="assistant", content="noted")
    return snapshots.create_snapshot(db, row)


def _replicate(snapshot: ChatSnapshot, replica: Session) -> None:
    replica.add(ChatSnapshot.model_validate(snapshot.model_dump()))
    replica.commit()


def _take_down(primary: Session, snapshot_id: str) -> None:
    primary.delete(primary.get(ChatSnapshot, snapshot_id))
    primary.commit()


def _fork(client: TestClient, snapshot_id: str):
    return client.post("/internal/chat/fork", json={"snapshot_id": snapshot_id})


def test_takedown_not_yet_replicated_cannot_be_forked(client, dbs):
    primary, replica = dbs
    snapshot = _mint(primary)
    _replicate(snapshot, replica)
    snapshot_id = snapshot.id
    sessions_before = len(primary.exec(select(ChatSession)).all())

    _take_down(primary, snapshot_id)  # committed on the primary, replica lags

    forked = _fork(client, snapshot_id)
    assert forked.status_code == 404
    assert len(primary.exec(select(ChatSession)).all()) == sessions_before


def test_takedown_during_admission_cannot_be_forked(client, dbs, monkeypatch):
    primary, replica = dbs
    snapshot = _mint(primary)
    _replicate(snapshot, replica)
    snapshot_id = snapshot.id
    real_siteverify = turnstile.siteverify

    async def siteverify_then_takedown(token, remoteip=None):
        result = await real_siteverify(token, remoteip)
        _take_down(primary, snapshot_id)
        return result

    monkeypatch.setattr(turnstile, "siteverify", siteverify_then_takedown)

    assert _fork(client, snapshot_id).status_code == 404


def test_live_snapshot_still_forks_with_its_transcript(client, dbs):
    primary, replica = dbs
    snapshot = _mint(primary)
    _replicate(snapshot, replica)

    forked = _fork(client, snapshot.id)
    assert forked.status_code == 200
    seeded = primary.exec(
        select(ChatMessage)
        .where(ChatMessage.session_id == forked.json()["session_id"])
        .order_by(ChatMessage.id)
    ).all()
    assert [(m.role, m.content) for m in seeded] == [
        ("user", "private details"),
        ("assistant", "noted"),
    ]


def test_freshly_minted_snapshot_forks_before_replication(client, dbs):
    primary, _replica = dbs
    snapshot = _mint(primary)  # not yet on the replica

    assert _fork(client, snapshot.id).status_code == 200
