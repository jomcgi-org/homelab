"""Router smoke tests for grimoire chat (SQLite create_all fixture).

Ports the retrieval/prompt integration checks from chat_public/phase4_test.py to
the Grimoire surface: the retrieved corpus is fenced as a <sourcebooks> DATA block
labelled "not instructions", the inference payload never carries a tools key, one
node_touched precedes the tokens per retrieved passage (touched set == retrieved
set), an empty retrieval still streams a normal turn, and the D&D system prompt is
server-fixed but env-overridable.

Fast in-memory SQLite + TestClient (the grimoire_chat models live on the
Postgres-only ``grimoire_chat`` schema, which SQLite cannot span, so the schema=
overrides are stripped for the fixture).
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from grimoire_chat import inference, retrieval, sessions
from grimoire_chat import router as router_module
from grimoire_chat.db import get_chat_session
from grimoire_chat.retrieval import RetrievedPassage
from grimoire_chat.router import router

_FAKE_REPLY = "By the Monster Manual, a goblin is a small humanoid."


def _fake_stream(captured=None):
    async def _gen(messages, *, max_tokens):
        if captured is not None:
            captured.append(messages)
        yield inference.TokenDelta(text=_FAKE_REPLY)
        yield inference.Usage(prompt_tokens=10, completion_tokens=5)

    return _gen


def _fake_retrieve(passages):
    async def _stub(session, query, *, k=None, embed_client=None):
        return passages

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
    # dependency resolves without a real Postgres; retrieval is monkeypatched per
    # test, so the session is not actually queried for the corpus.
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
# Retrieved corpus is injected as a clearly-delimited DATA block
# ---------------------------------------------------------------------------


def test_build_model_messages_fences_retrieved_corpus():
    retrieved = [
        RetrievedPassage(
            "chunk-a", "PHB: Grappling", "grappling rules text", "chunk", 0.9
        ),
        RetrievedPassage(
            "ent-b", "Goblin (creature)", "Goblin statblock", "entity", 0.8
        ),
    ]
    messages = router_module._build_model_messages(
        None, [], "how do i grapple?", retrieved
    )

    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == router_module._DEFAULT_SYSTEM_PROMPT

    context = next(
        m for m in messages if m["role"] == "system" and "<sourcebooks>" in m["content"]
    )
    body = context["content"]
    assert "</sourcebooks>" in body
    assert "not instructions" in body.lower()
    assert "grappling rules text" in body
    assert "Goblin statblock" in body
    assert "[chunk: PHB: Grappling]" in body
    assert "[entity: Goblin (creature)]" in body

    assert messages[-1] == {"role": "user", "content": "how do i grapple?"}


def test_build_model_messages_omits_block_when_no_retrieval():
    messages = router_module._build_model_messages(None, [], "hi", [])
    assert not any("<sourcebooks>" in m["content"] for m in messages)
    messages_none = router_module._build_model_messages(None, [], "hi", None)
    assert not any("<sourcebooks>" in m["content"] for m in messages_none)


def test_system_prompt_env_overridable(monkeypatch):
    assert router_module._system_prompt() == router_module._DEFAULT_SYSTEM_PROMPT
    monkeypatch.setenv("GRIMOIRE_CHAT_SYSTEM_PROMPT", "custom sage prompt")
    assert router_module._system_prompt() == "custom sage prompt"


# ---------------------------------------------------------------------------
# No tools in the inference payload (no-tools posture preserved)
# ---------------------------------------------------------------------------


def test_inference_payload_has_no_tools():
    body = inference._payload(
        [{"role": "user", "content": "hi"}], max_tokens=32, stream=True
    )
    assert "tools" not in body
    assert "functions" not in body
    assert set(body) <= {"model", "messages", "max_tokens", "stream", "stream_options"}


# ---------------------------------------------------------------------------
# node_touched per retrieved passage, before tokens; touched set == retrieved set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_emits_node_touched_for_each_passage(
    client, session, monkeypatch
):
    retrieved = [
        RetrievedPassage(
            "chunk-a",
            "PHB: Goblins",
            "goblin lore",
            "chunk",
            0.9,
            book_id="phb",
            chunk_ref="c-goblins",
        ),
        RetrievedPassage(
            "ent-b",
            "Goblin (creature)",
            "goblin statblock",
            "entity",
            0.8,
            entity_type="creature",
        ),
    ]
    captured: list = []
    monkeypatch.setattr(inference, "stream_chat", _fake_stream(captured))
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve(retrieved))
    row = sessions.create_session(session)

    resp = client.post(
        "/internal/grimoire-chat/message",
        json={"session_id": row.id, "message": "tell me about goblins"},
    )
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    types = [f["type"] for f in frames]

    touched = [f["data"] for f in frames if f["type"] == "node_touched"]
    assert {(t["id"], t["title"]) for t in touched} == {
        ("chunk-a", "PHB: Goblins"),
        ("ent-b", "Goblin (creature)"),
    }
    assert types.index("node_touched") < types.index("token")
    assert {t["id"] for t in touched} == {p.ref_id for p in retrieved}

    # Each frame carries kind, and the clickable fields per kind: a chunk deep-links
    # via book_id + chunk_ref, an entity opens by entity_type.
    by_id = {t["id"]: t for t in touched}
    assert by_id["chunk-a"]["kind"] == "chunk"
    assert by_id["chunk-a"]["book_id"] == "phb"
    assert by_id["chunk-a"]["chunk_ref"] == "c-goblins"
    assert "entity_type" not in by_id["chunk-a"]
    assert by_id["ent-b"]["kind"] == "entity"
    assert by_id["ent-b"]["entity_type"] == "creature"
    assert "book_id" not in by_id["ent-b"]

    assert len(captured) == 1
    joined = " ".join(m["content"] for m in captured[0])
    assert "<sourcebooks>" in joined
    assert "goblin lore" in joined and "goblin statblock" in joined


@pytest.mark.asyncio
async def test_empty_retrieval_still_streams(client, session, monkeypatch):
    captured: list = []
    monkeypatch.setattr(inference, "stream_chat", _fake_stream(captured))
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve([]))
    row = sessions.create_session(session)

    resp = client.post(
        "/internal/grimoire-chat/message",
        json={"session_id": row.id, "message": "anything"},
    )
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    types = [f["type"] for f in frames]
    assert "node_touched" not in types
    assert "token" in types and types[-1] == "done"

    joined = " ".join(m["content"] for m in captured[0])
    assert "<sourcebooks>" not in joined


# ---------------------------------------------------------------------------
# Missing session -> 404 (never leak which of missing/expired/invalid)
# ---------------------------------------------------------------------------


def test_message_unknown_session_404(client):
    resp = client.post(
        "/internal/grimoire-chat/message",
        json={"session_id": "does-not-exist", "message": "hi"},
    )
    assert resp.status_code == 404
