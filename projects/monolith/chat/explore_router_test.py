"""Integration test for POST /api/chat/explore.

Mocks only externals (LLM model, embedding service, knowledge store).
Exercises the full HTTP path: request parsing -> agent tool dispatch ->
SSE event emission -> streaming response.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, DeltaToolCalls, FunctionModel

from app.main import app
from chat.explorer import create_explorer_agent


# ---------------------------------------------------------------------------
# Mock data — represents what KnowledgeStore / EmbeddingClient would return
# ---------------------------------------------------------------------------

MOCK_SEARCH_RESULTS = [
    {
        "note_id": "note-1",
        "title": "Kubernetes Networking",
        "type": "note",
        "tags": ["k8s", "networking"],
        "score": 0.92,
        "snippet": "Service mesh overview and comparison of CNI plugins.",
        "edges": [{"target_id": "note-2", "edge_type": "refines"}],
    },
]

MOCK_LINKS = [
    {
        "target_id": "note-2",
        "resolved_note_id": "note-2",
        "edge_type": "related",
    },
]

MOCK_NOTE = {
    "note_id": "note-2",
    "title": "Linkerd",
    "type": "article",
    "tags": ["service-mesh"],
}


# ---------------------------------------------------------------------------
# Scripted tool-call sequence for both function and stream_function
# ---------------------------------------------------------------------------

_TOOL_STEPS: list[tuple[str, dict, str]] = [
    ("search_kg", {"query": "kubernetes networking"}, "c1"),
    ("expand_node", {"note_id": "note-1"}, "c2"),
    ("discard_node", {"note_id": "note-2", "reason": "not relevant to query"}, "c3"),
]

_FINAL_TEXT = "Kubernetes networking uses CNI plugins and service meshes."


def _count_tool_returns(messages) -> int:
    return sum(
        1
        for msg in messages
        if hasattr(msg, "parts")
        for part in msg.parts
        if isinstance(part, ToolReturnPart)
    )


def _scripted_model(messages, info):
    """Non-streaming: search_kg -> expand_node -> discard_node -> text."""
    n = _count_tool_returns(messages)
    if n < len(_TOOL_STEPS):
        name, args, cid = _TOOL_STEPS[n]
        return ModelResponse(
            parts=[ToolCallPart(tool_name=name, args=args, tool_call_id=cid)]
        )
    return ModelResponse(parts=[TextPart(_FINAL_TEXT)])


async def _scripted_stream(messages, info):
    """Streaming: yields DeltaToolCalls for tool steps, str for text step."""
    n = _count_tool_returns(messages)
    if n < len(_TOOL_STEPS):
        name, args, cid = _TOOL_STEPS[n]
        yield {
            0: DeltaToolCall(
                name=name,
                json_args=json.dumps(args),
                tool_call_id=cid,
            )
        }
    else:
        yield _FINAL_TEXT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse(raw: str) -> list[dict]:
    """Parse SSE text into a list of event dicts."""
    events = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            events.append(json.loads(line.removeprefix("data: ")))
    return events


def _mock_get_session():
    yield MagicMock()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_store():
    store = MagicMock()
    store.search_notes_with_context.return_value = MOCK_SEARCH_RESULTS
    store.get_note_links.return_value = MOCK_LINKS
    store.get_note_by_id.return_value = MOCK_NOTE
    return store


@pytest.fixture()
def mock_embed_client():
    client = AsyncMock()
    client.embed.return_value = [0.1] * 1024
    return client


@pytest.fixture()
def client(mock_store, mock_embed_client):
    agent = create_explorer_agent()

    with (
        patch("chat.router._explorer_agent", agent),
        patch("chat.router.get_store", return_value=mock_store),
        patch("chat.router.EmbeddingClient", return_value=mock_embed_client),
        patch("core.db.get_session", _mock_get_session),
        agent.override(
            model=FunctionModel(_scripted_model, stream_function=_scripted_stream)
        ),
    ):
        yield TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExploreEndpoint:
    def test_returns_sse_stream(self, client):
        resp = client.post(
            "/api/chat/explore", json={"message": "kubernetes networking"}
        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    def test_event_sequence(self, client):
        resp = client.post(
            "/api/chat/explore", json={"message": "kubernetes networking"}
        )
        events = _parse_sse(resp.text)
        types = [e["type"] for e in events]

        # Tools fire in scripted order, then text, then done
        assert "node_discovered" in types
        assert "edge_traversed" in types
        assert "node_discarded" in types
        assert "text_chunk" in types
        assert types[-1] == "done"

    def test_node_discovered_payload(self, client):
        resp = client.post(
            "/api/chat/explore", json={"message": "kubernetes networking"}
        )
        events = _parse_sse(resp.text)
        discovered = [e for e in events if e["type"] == "node_discovered"]

        # search_kg discovers note-1, expand_node discovers note-2
        ids = {e["data"]["note_id"] for e in discovered}
        assert "note-1" in ids
        assert "note-2" in ids

        note1 = next(e for e in discovered if e["data"]["note_id"] == "note-1")
        assert note1["data"]["title"] == "Kubernetes Networking"
        assert note1["data"]["type"] == "note"
        assert "k8s" in note1["data"]["tags"]

    def test_edge_traversed_payload(self, client):
        resp = client.post(
            "/api/chat/explore", json={"message": "kubernetes networking"}
        )
        events = _parse_sse(resp.text)
        edges = [e for e in events if e["type"] == "edge_traversed"]

        assert len(edges) >= 1
        assert edges[0]["data"]["from_id"] == "note-1"
        assert edges[0]["data"]["to_id"] == "note-2"
        assert edges[0]["data"]["edge_type"] == "related"

    def test_node_discarded_payload(self, client):
        resp = client.post(
            "/api/chat/explore", json={"message": "kubernetes networking"}
        )
        events = _parse_sse(resp.text)
        discarded = [e for e in events if e["type"] == "node_discarded"]

        assert len(discarded) == 1
        assert discarded[0]["data"]["note_id"] == "note-2"
        assert discarded[0]["data"]["reason"] == "not relevant to query"

    def test_text_chunk_contains_response(self, client):
        resp = client.post(
            "/api/chat/explore", json={"message": "kubernetes networking"}
        )
        events = _parse_sse(resp.text)
        chunks = [e for e in events if e["type"] == "text_chunk"]

        full_text = "".join(c["data"]["text"] for c in chunks)
        assert "CNI plugins" in full_text

    def test_empty_message_rejected(self, client):
        resp = client.post("/api/chat/explore", json={"message": ""})
        assert resp.status_code == 422

    def test_history_forwarded(self, client, mock_store, mock_embed_client):
        resp = client.post(
            "/api/chat/explore",
            json={
                "message": "tell me more",
                "history": [
                    {"role": "user", "content": "kubernetes networking"},
                    {"role": "assistant", "content": "Here are some notes..."},
                ],
            },
        )

        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        assert any(e["type"] == "done" for e in events)
