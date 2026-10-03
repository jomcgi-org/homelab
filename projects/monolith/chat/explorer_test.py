import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat.explorer import ExplorerDeps, _discard_node, _expand_node, _search_kg
from chat.sse import SSEEmitter

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def make_deps(emitter: SSEEmitter) -> ExplorerDeps:
    store = MagicMock()
    store.search_notes_with_context.return_value = [
        {
            "note_id": "note-1",
            "title": "Kubernetes Networking",
            "type": "note",
            "tags": ["k8s", "networking"],
            "score": 0.92,
            "snippet": "Service mesh overview...",
            "observed_at": "2026-10-01T00:00:00Z",
            "review_after": "2026-12-01T00:00:00Z",
            "freshness": "current",
            "edges": [
                {
                    "target_id": "note-2",
                    "target_title": "Linkerd",
                    "kind": "edge",
                    "edge_type": "refines",
                }
            ],
        }
    ]
    store.get_note_links.return_value = [
        {
            "target_id": "note-3",
            "target_title": "Cilium",
            "kind": "edge",
            "edge_type": "related",
            "resolved_note_id": "note-3",
        }
    ]
    store.get_note_by_id.return_value = {
        "note_id": "note-3",
        "title": "Cilium",
        "type": "article",
        "tags": ["networking"],
        "observed_at": "2026-10-01T00:00:00Z",
        "review_after": "2026-12-01T00:00:00Z",
        "freshness": "current",
    }

    embed_client = AsyncMock()
    embed_client.embed.return_value = [0.1] * 1024

    return ExplorerDeps(
        store=store,
        embed_client=embed_client,
        emitter=emitter,
    )


@pytest.mark.anyio
async def test_search_kg_emits_node_discovered():
    emitter = SSEEmitter()
    deps = make_deps(emitter)

    result = await _search_kg(deps, "kubernetes networking", now=NOW)

    emitter.close()
    events = []
    async for chunk in emitter.stream():
        parsed = json.loads(chunk.removeprefix("data: ").strip())
        events.append(parsed)

    assert len(events) == 1
    assert events[0]["type"] == "node_discovered"
    assert events[0]["data"]["note_id"] == "note-1"
    assert events[0]["data"]["title"] == "Kubernetes Networking"
    assert "refines" in str(events[0]["data"]["edges"])
    assert "kubernetes" in result.lower()


@pytest.mark.anyio
async def test_expand_node_emits_edge_and_node():
    emitter = SSEEmitter()
    deps = make_deps(emitter)

    await _expand_node(deps, "note-1", now=NOW)

    emitter.close()
    events = []
    async for chunk in emitter.stream():
        parsed = json.loads(chunk.removeprefix("data: ").strip())
        events.append(parsed)

    types = [e["type"] for e in events]
    assert "edge_traversed" in types
    assert "node_discovered" in types


@pytest.mark.anyio
async def test_discard_node_emits_event():
    emitter = SSEEmitter()
    deps = make_deps(emitter)

    await _discard_node(deps, "note-1", "not relevant to query")

    emitter.close()
    events = []
    async for chunk in emitter.stream():
        parsed = json.loads(chunk.removeprefix("data: ").strip())
        events.append(parsed)

    assert len(events) == 1
    assert events[0]["type"] == "node_discarded"
    assert events[0]["data"]["note_id"] == "note-1"
    assert events[0]["data"]["reason"] == "not relevant to query"


@pytest.mark.anyio
async def test_explorer_search_and_edges_exclude_due_and_unknown_context():
    deps = make_deps(SSEEmitter())
    deps.store.search_notes_with_context.return_value[0]["review_after"] = (
        NOW.isoformat()
    )
    assert await _search_kg(deps, "query", now=NOW) == "No results found."
    deps.store.get_note_by_id.return_value["review_after"] = None
    assert "No current evidence" in await _expand_node(deps, "note-1", now=NOW)
    deps.store.get_note_links.assert_not_called()
