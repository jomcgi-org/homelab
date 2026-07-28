"""Phase 5 unit tests: the durable public-chat response cache + shared GPU
limiter wiring (ADR 005 follow-up).

Covers the Postgres-backed response cache and its interaction with the
cluster-wide GPU slot limiter:

- A repeated identical message replays the stored answer WITHOUT calling vLLM a
  second time (and trivial whitespace/case differences still hit).
- A changed notes watermark or prompt/model version misses and regenerates.
- A cache hit still emits the SSE token+done frames and persists the turn.
- A cache hit does NOT consume a GPU slot: it succeeds even when every slot is
  occupied (so cached starters scale freely across replicas).
- The shared limiter sheds "busy" on a miss when the ceiling is reached, and the
  slot is released on both success and exception so a later turn is not blocked.

Fast in-memory SQLite + TestClient, mirroring phase4_test.py. The cache table is
materialized by ``SQLModel.metadata.create_all`` (the ChatResponseCache model);
the notes watermark (normally a public_api view query) is monkeypatched so the
cache key is controllable without a real Postgres; inference is a counting stub
so we can assert the GPU path is skipped on a hit. The limiter's SQLite path uses
the in-process breaker, so the shed/release semantics are exercised without a
real advisory-lock connection (the Postgres advisory-lock path is the same
wiring against pg_try_advisory_lock).
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from core.db import get_session
from chat_public import cache, inference, limits, retrieval, sessions
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


def _raising_stream(counter: dict):
    """A stand-in for ``inference.stream_chat`` that fails mid-turn."""

    async def _gen(messages, *, max_tokens):
        counter["calls"] += 1
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this an async generator

    return _gen


def _fake_retrieve(notes: list[RetrievedNote]):
    async def _stub(session, query, *, k=None, embed_client=None):
        return notes

    return _stub


@pytest.fixture(autouse=True)
def _clear_cache_state():
    """Each test starts with a fresh watermark memo (the cache table itself is
    fresh per test via a new SQLite engine)."""
    cache.reset_watermark_memo()
    yield
    cache.reset_watermark_memo()


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


def _fix_watermark(monkeypatch, value: str = "wm-1"):
    monkeypatch.setattr(cache, "current_watermark", lambda read_db: value)


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
    _fix_watermark(monkeypatch)
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
    touched = [f["data"] for f in frames if f["type"] == "node_touched"]
    assert {(t["id"], t["title"]) for t in touched} == {("n1", "TSA")}
    assert types.index("node_touched") < types.index("token") < types.index("done")
    token_text = "".join(f["data"]["text"] for f in frames if f["type"] == "token")
    assert token_text == _FAKE_REPLY


# ---------------------------------------------------------------------------
# 2. A changed notes watermark misses and regenerates
# ---------------------------------------------------------------------------


def test_empty_reply_is_not_cached(client, session, monkeypatch):
    # A turn whose generation yields no text must NOT be cached: caching an empty
    # reply would poison the entry so every future identical turn replays nothing.
    counter = {"calls": 0}
    monkeypatch.setattr(inference, "stream_chat", _counting_stream(counter, reply=""))
    monkeypatch.setattr(
        retrieval,
        "retrieve",
        _fake_retrieve([RetrievedNote("n1", "TSA", "thread state analysis", 0.9)]),
    )
    _fix_watermark(monkeypatch)
    row = sessions.create_session(session)

    first = _post(client, row.id, "What is the TSA method?")
    assert first.status_code == 200
    assert counter["calls"] == 1

    # The same question regenerates (treated as a miss) rather than replaying an
    # empty cached answer.
    second = _post(client, row.id, "What is the TSA method?")
    assert second.status_code == 200
    assert counter["calls"] == 2


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
    _fix_watermark(monkeypatch)
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
    _fix_watermark(monkeypatch)
    row = sessions.create_session(session)

    _post(client, row.id, "What is the TSA method?")
    second = _post(client, row.id, "What is the TSA method?")
    assert counter["calls"] == 1

    frames = _parse_sse(second.text)
    token_text = "".join(f["data"]["text"] for f in frames if f["type"] == "token")
    assert token_text == _FAKE_REPLY
    done = next(f for f in frames if f["type"] == "done")
    assert done["data"]["turn_count"] == 2

    messages = session.exec(select(ChatMessage).order_by(ChatMessage.id)).all()
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[3].content == _FAKE_REPLY
    session.refresh(row)
    assert row.turn_count == 2
    assert row.total_tokens > 0


# ---------------------------------------------------------------------------
# 5. A cache hit does NOT consume a GPU slot (succeeds even when slots are full)
# ---------------------------------------------------------------------------


def test_cache_hit_bypasses_full_slots(client, session, monkeypatch):
    counter = {"calls": 0}
    monkeypatch.setattr(inference, "stream_chat", _counting_stream(counter))
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve([]))
    _fix_watermark(monkeypatch)
    row = sessions.create_session(session)

    # Populate the cache with one real (slot-consuming) generation.
    first = _post(client, row.id, "What is the TSA method?")
    assert next(f for f in _parse_sse(first.text) if f["type"] == "done")
    assert counter["calls"] == 1

    # Now occupy every GPU slot: a miss would shed busy.
    monkeypatch.setattr(limits, "_breaker", limits._CircuitBreaker(0))

    second = _post(client, row.id, "What is the TSA method?")
    assert second.status_code == 200
    frames = _parse_sse(second.text)
    types = [f["type"] for f in frames]
    # The hit serves a full answer with no busy shed and no extra vLLM call.
    assert "busy" not in types
    assert "done" in types
    token_text = "".join(f["data"]["text"] for f in frames if f["type"] == "token")
    assert token_text == _FAKE_REPLY
    assert counter["calls"] == 1


# ---------------------------------------------------------------------------
# 6. A miss sheds "busy" when the shared ceiling is reached
# ---------------------------------------------------------------------------


def test_miss_sheds_busy_when_slots_full(client, session, monkeypatch):
    counter = {"calls": 0}
    monkeypatch.setattr(inference, "stream_chat", _counting_stream(counter))
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve([]))
    _fix_watermark(monkeypatch)
    # Every slot occupied: a sized-0 breaker sheds every (uncached) turn.
    monkeypatch.setattr(limits, "_breaker", limits._CircuitBreaker(0))
    row = sessions.create_session(session)

    resp = _post(client, row.id, "An uncached question")
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    busy = next(f for f in frames if f["type"] == "busy")
    assert busy["data"]["code"] == "busy"
    # Shed before any GPU work, and nothing persisted.
    assert counter["calls"] == 0
    assert session.exec(select(ChatMessage)).all() == []


# ---------------------------------------------------------------------------
# 7. The slot is released on an exception so the next turn is not blocked
# ---------------------------------------------------------------------------


def test_slot_released_on_exception(client, session, monkeypatch):
    counter = {"calls": 0}
    monkeypatch.setattr(inference, "stream_chat", _raising_stream(counter))
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve([]))
    _fix_watermark(monkeypatch)
    # A real breaker sized 1: if the slot leaked on the failed turn, the next
    # turn would shed busy instead of acquiring.
    monkeypatch.setattr(limits, "_breaker", limits._CircuitBreaker(1))
    row = sessions.create_session(session)

    first = _post(client, row.id, "first question")
    first_types = [f["type"] for f in _parse_sse(first.text)]
    assert "error" in first_types
    assert limits.current_inflight() == 0

    second = _post(client, row.id, "second question")
    second_types = [f["type"] for f in _parse_sse(second.text)]
    # Not shed: the slot was freed in the finally, so the turn ran (and failed
    # again), it did not get a busy frame.
    assert "busy" not in second_types
    assert "error" in second_types
    assert limits.current_inflight() == 0
