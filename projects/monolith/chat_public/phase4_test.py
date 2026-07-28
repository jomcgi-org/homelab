"""Phase 4a unit tests for public-chat retrieval (ADR 005 layer 5, V3 plan Phase 4a).

Covers the public-graph retrieval wiring added in Phase 4a, without a real Postgres
(the DB-enforced confinement is covered by public_api_chunks_grants_test.py against a
real DB). Here the query embedding and the chunk search are mocked so the focus is the
integration contract:

1. ``_build_model_messages`` injects retrieved chunk text as a clearly-delimited DATA
   block (fenced, labelled "not instructions"), positioned as context before the
   conversation turns.
2. The inference request body never carries a ``tools``/``functions`` key: the model
   has no tools, so retrieved content cannot act.
3. The message turn emits one ``node_touched`` SSE event per retrieved public note,
   BEFORE the token stream, and the touched-node set equals the retrieved-note set.
4. A turn with no public matches (empty retrieval) still streams a normal reply, with
   no ``node_touched`` events and no retrieved-context block in the model messages.
5. ``retrieval.retrieve`` embeds the query and maps the public-chunk rows to
   ``RetrievedNote``s (the touched set), and fails open (empty) on an embedder error.

Fast in-memory SQLite + TestClient, mirroring phase3_test.py (the chat_public models
live on the Postgres-only ``chat_public`` schema, which SQLite cannot span, so the
schema= overrides are stripped for the fixture).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from chat_public import inference, retrieval, sessions
from chat_public import router as router_module
from chat_public.db import get_chat_session
from chat_public.retrieval import RetrievedNote
from chat_public.router import router

_FAKE_REPLY = "Grounded reply."


def _fake_stream(captured: list | None = None):
    async def _gen(messages, *, max_tokens):
        if captured is not None:
            captured.append(messages)
        yield inference.TokenDelta(text=_FAKE_REPLY)
        yield inference.Usage(prompt_tokens=10, completion_tokens=5)

    return _gen


def _fake_retrieve(notes: list[RetrievedNote]):
    """Return an async stand-in for ``retrieval.retrieve`` yielding ``notes``."""

    async def _stub(session, query, *, k=None, embed_client=None):
        return notes

    return _stub


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
    # read_db (public_reader) is overridden to the same SQLite session so the
    # dependency resolves without a real Postgres connection; retrieval itself is
    # monkeypatched in each test, so the session is not actually queried.
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _parse_sse(text: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


# ---------------------------------------------------------------------------
# 1. Retrieved context is injected as a clearly-delimited DATA block
# ---------------------------------------------------------------------------


def test_build_model_messages_injects_delimited_retrieved_context():
    retrieved = [
        RetrievedNote("note-a", "Note A", "alpha grounding text", 0.9),
        RetrievedNote("note-b", "Note B", "beta grounding text", 0.8),
    ]
    messages = router_module._build_model_messages(
        None, [], "what is alpha?", retrieved
    )

    # First message is the server-fixed system prompt.
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == router_module._DEFAULT_SYSTEM_PROMPT

    # A separate system message carries the retrieved notes, fenced + labelled as
    # data and NOT instructions.
    context = next(
        m
        for m in messages
        if m["role"] == "system" and "<public_notes>" in m["content"]
    )
    body = context["content"]
    assert "</public_notes>" in body
    assert "not instructions" in body.lower()
    assert "alpha grounding text" in body
    assert "beta grounding text" in body
    assert "[note: Note A]" in body and "[note: Note B]" in body

    # The actual user turn is still present as the final user message.
    assert messages[-1] == {"role": "user", "content": "what is alpha?"}


def test_build_model_messages_omits_block_when_no_retrieval():
    messages = router_module._build_model_messages(None, [], "hi", [])
    assert not any("<public_notes>" in m["content"] for m in messages)
    messages_none = router_module._build_model_messages(None, [], "hi", None)
    assert not any("<public_notes>" in m["content"] for m in messages_none)


# ---------------------------------------------------------------------------
# 2. The model has no tools: the inference payload never carries a tools key
# ---------------------------------------------------------------------------


def test_inference_payload_has_no_tools():
    body = inference._payload(
        [{"role": "user", "content": "hi"}], max_tokens=32, stream=True
    )
    assert "tools" not in body
    assert "functions" not in body
    assert set(body) <= {"model", "messages", "max_tokens", "stream", "stream_options"}


# ---------------------------------------------------------------------------
# 3. node_touched per retrieved note, before tokens; touched set == retrieved set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_emits_node_touched_for_each_retrieved_note(
    client, session, monkeypatch
):
    retrieved = [
        RetrievedNote("note-a", "Note A", "alpha grounding text", 0.9),
        RetrievedNote("note-b", "Note B", "beta grounding text", 0.8),
    ]
    captured: list = []
    monkeypatch.setattr(inference, "stream_chat", _fake_stream(captured))
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve(retrieved))
    row = sessions.create_session(session)

    resp = client.post(
        "/internal/chat/message",
        json={"session_id": row.id, "message": "tell me about alpha"},
    )
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    types = [f["type"] for f in frames]

    # One node_touched per retrieved note, and they all precede the first token.
    touched = [f["data"] for f in frames if f["type"] == "node_touched"]
    assert {(t["id"], t["title"]) for t in touched} == {
        ("note-a", "Note A"),
        ("note-b", "Note B"),
    }
    assert types.index("node_touched") < types.index("token")

    # The touched set equals the retrieved public-note set.
    assert {t["id"] for t in touched} == {n.note_id for n in retrieved}

    # The retrieved chunk text reached the model as delimited context.
    assert len(captured) == 1
    joined = " ".join(m["content"] for m in captured[0])
    assert "<public_notes>" in joined
    assert "alpha grounding text" in joined and "beta grounding text" in joined


# ---------------------------------------------------------------------------
# 4. Empty retrieval still streams a normal turn, no node_touched, no block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_retrieval_still_streams(client, session, monkeypatch):
    captured: list = []
    monkeypatch.setattr(inference, "stream_chat", _fake_stream(captured))
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve([]))
    row = sessions.create_session(session)

    resp = client.post(
        "/internal/chat/message",
        json={"session_id": row.id, "message": "anything"},
    )
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    types = [f["type"] for f in frames]
    assert "node_touched" not in types
    assert "token" in types and types[-1] == "done"

    joined = " ".join(m["content"] for m in captured[0])
    assert "<public_notes>" not in joined


# ---------------------------------------------------------------------------
# 5. retrieve embeds the query, maps rows to RetrievedNotes, fails open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_embeds_query_and_returns_public_notes(monkeypatch):
    vector = [0.5] * 1024
    embed = AsyncMock(return_value=vector)
    mock_client = type("C", (), {"embed": embed})()

    seen: dict = {}

    def fake_search(session, query_embedding, *, limit):
        seen["embedding"] = query_embedding
        seen["limit"] = limit
        return [
            {"note_id": "n1", "title": "T1", "chunk_text": "c1", "score": 0.9},
            {"note_id": "n2", "title": "T2", "chunk_text": "c2", "score": 0.7},
        ]

    monkeypatch.setattr(retrieval, "search_public_chunks", fake_search)

    notes = await retrieval.retrieve(
        object(), "find me notes", k=2, embed_client=mock_client
    )

    embed.assert_awaited_once_with("find me notes")
    assert seen["embedding"] == vector
    assert seen["limit"] == 2
    # Rows map 1:1 to the touched-node set.
    assert [(n.note_id, n.title, n.chunk_text) for n in notes] == [
        ("n1", "T1", "c1"),
        ("n2", "T2", "c2"),
    ]


@pytest.mark.asyncio
async def test_retrieve_blank_query_returns_empty_without_embedding():
    embed = AsyncMock()
    mock_client = type("C", (), {"embed": embed})()
    assert await retrieval.retrieve(object(), "   ", embed_client=mock_client) == []
    embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_retrieve_fails_open_on_embedder_error(monkeypatch):
    embed = AsyncMock(side_effect=RuntimeError("embedder down"))
    mock_client = type("C", (), {"embed": embed})()
    # A transient embedder failure yields an ungrounded turn, never an error.
    assert await retrieval.retrieve(object(), "query", embed_client=mock_client) == []
