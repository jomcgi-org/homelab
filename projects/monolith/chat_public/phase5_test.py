"""Phase 5 unit tests for the public-chat response cache (ADR 005 follow-up).

Covers the in-process LRU response cache: a repeated identical message replays
the stored answer WITHOUT calling vLLM a second time; a changed notes watermark
or prompt/model version misses and regenerates; and a cache hit still emits the
SSE token+done frames and persists the turn to the transcript.

Fast in-memory SQLite + TestClient, mirroring phase4_test.py. The notes
watermark (normally a public_api view query) is monkeypatched so the cache key
is controllable without a real Postgres, and inference is a counting stub so we
can assert the GPU path is skipped on a hit.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.db import get_session
from chat_public import cache, inference, retrieval, sessions
from chat_public.db import get_chat_session
from chat_public.models import ChatMessage
from chat_public.retrieval import RetrievedNote
from chat_public.router import router

_FAKE_REPLY = "The TSA method is Thread State Analysis."


def _counting_stream(counter: dict, reply: str = _FAKE_REPLY):
    """A stand-in for ``inference.stream_chat`` that counts how often it runs."""

    async def _gen(messages, *, max_tokens):
        counter["calls"] += 1
        yield inference.TokenDelta(text=reply)
        yield inference.Usage(prompt_tokens=10, completion_tokens=5)

    return _gen


def _fake_retrieve(notes: list[RetrievedNote]):
    async def _stub(session, query, *, k=None, embed_client=None):
        return notes

    return _stub


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts and ends with an empty response cache + watermark memo."""
    cache.reset()
    yield
    cache.reset()


@pytest.fixture(name="session")
def session_fixture():
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
    app.include_router(router)
    app.dependency_overrides[get_chat_session] = lambda: session
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _parse_sse(text: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


def _post(client, session_id: str, message: str):
    return client.post(
        "/internal/chat/message",
        json={"session_id": session_id, "message": message},
    )


# ---------------------------------------------------------------------------
# 1. A repeated identical message hits the cache and skips the model
# ---------------------------------------------------------------------------


def test_repeated_message_hits_cache_and_skips_model(client, session, monkeypatch):
    counter = {"calls": 0}
    monkeypatch.setattr(inference, "stream_chat", _counting_stream(counter))
    monkeypatch.setattr(
        retrieval,
        "retrieve",
        _fake_retrieve([RetrievedNote("n1", "TSA", "thread state analysis", 0.9)]),
    )
    # A fixed watermark so both turns share a cache key.
    monkeypatch.setattr(cache, "current_watermark", lambda read_db: "wm-1")
    row = sessions.create_session(session)

    first = _post(client, row.id, "What is the TSA method?")
    assert first.status_code == 200
    assert counter["calls"] == 1

    # Same question, trivially different whitespace/case: still a cache hit.
    second = _post(client, row.id, "  what is THE tsa   method? ")
    assert second.status_code == 200
    # vLLM was NOT called the second time.
    assert counter["calls"] == 1

    frames = _parse_sse(second.text)
    types = [f["type"] for f in frames]
    # The cached touched node is repainted before the text, then a single token
    # frame carries the whole reply, then done.
    touched = [f["data"] for f in frames if f["type"] == "node_touched"]
    assert {(t["id"], t["title"]) for t in touched} == {("n1", "TSA")}
    assert types.index("node_touched") < types.index("token") < types.index("done")
    token_text = "".join(f["data"]["text"] for f in frames if f["type"] == "token")
    assert token_text == _FAKE_REPLY


# ---------------------------------------------------------------------------
# 2. A changed notes watermark misses and regenerates
# ---------------------------------------------------------------------------


def test_changed_watermark_misses(client, session, monkeypatch):
    counter = {"calls": 0}
    monkeypatch.setattr(inference, "stream_chat", _counting_stream(counter))
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve([]))
    watermark = {"value": "wm-1"}
    monkeypatch.setattr(cache, "current_watermark", lambda read_db: watermark["value"])
    row = sessions.create_session(session)

    assert _post(client, row.id, "What is the TSA method?").status_code == 200
    assert counter["calls"] == 1

    # The public notes changed: a new watermark invalidates the cached answer.
    watermark["value"] = "wm-2"
    assert _post(client, row.id, "What is the TSA method?").status_code == 200
    assert counter["calls"] == 2


# ---------------------------------------------------------------------------
# 3. A changed prompt/model version misses and regenerates
# ---------------------------------------------------------------------------


def test_changed_prompt_version_misses(client, session, monkeypatch):
    counter = {"calls": 0}
    monkeypatch.setattr(inference, "stream_chat", _counting_stream(counter))
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve([]))
    monkeypatch.setattr(cache, "current_watermark", lambda read_db: "wm-1")
    row = sessions.create_session(session)

    monkeypatch.setenv("CHAT_PUBLIC_SYSTEM_PROMPT", "Prompt version A.")
    assert _post(client, row.id, "What is the TSA method?").status_code == 200
    assert counter["calls"] == 1

    # The server-fixed system prompt changed: the prompt version, and therefore
    # the cache key, changes, so the same question regenerates.
    monkeypatch.setenv("CHAT_PUBLIC_SYSTEM_PROMPT", "Prompt version B is different.")
    assert _post(client, row.id, "What is the TSA method?").status_code == 200
    assert counter["calls"] == 2


# ---------------------------------------------------------------------------
# 4. A cache hit still emits token+done and persists the turn
# ---------------------------------------------------------------------------


def test_cache_hit_persists_turn_and_emits_sse(client, session, monkeypatch):
    counter = {"calls": 0}
    monkeypatch.setattr(inference, "stream_chat", _counting_stream(counter))
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve([]))
    monkeypatch.setattr(cache, "current_watermark", lambda read_db: "wm-1")
    row = sessions.create_session(session)

    _post(client, row.id, "What is the TSA method?")
    second = _post(client, row.id, "What is the TSA method?")
    assert counter["calls"] == 1

    frames = _parse_sse(second.text)
    token_text = "".join(f["data"]["text"] for f in frames if f["type"] == "token")
    assert token_text == _FAKE_REPLY
    done = next(f for f in frames if f["type"] == "done")
    # The cached turn is the session's second turn and counters advanced.
    assert done["data"]["turn_count"] == 2

    # Both turns persisted to the transcript (a cache hit is still a real turn).
    messages = session.exec(select(ChatMessage).order_by(ChatMessage.id)).all()
    assert [m.role for m in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert messages[3].content == _FAKE_REPLY
    session.refresh(row)
    assert row.turn_count == 2
    # The per-session token budget moved on the cache hit (estimate-based).
    assert row.total_tokens > 0
