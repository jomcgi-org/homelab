"""PydanticAI explorer agent -- searches and traverses the knowledge graph."""

import os
from dataclasses import dataclass

from pydantic_ai import Agent, ModelSettings, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from chat.sse import SSEEmitter
from knowledge.api import KnowledgeStore
from shared.embedding import EmbeddingClient
import shared.inference

SYSTEM_PROMPT = """\
You are a knowledge graph explorer. The user asks questions and you search \
their personal knowledge graph to find relevant notes and connections.

Your workflow:
1. Use search_kg to find notes matching the user's question.
2. Use expand_node to follow edges from promising notes and discover connections.
3. Use discard_node to explicitly mark irrelevant results (with a reason).
4. Synthesize your findings into a clear answer, referencing the notes you found.

Always search before answering. Expand at least one promising node to discover \
deeper connections. Discard results that aren't relevant — be decisive. \
Reference notes by title when answering."""


@dataclass
class ExplorerDeps:
    store: KnowledgeStore
    embed_client: EmbeddingClient
    emitter: SSEEmitter


def create_explorer_agent() -> Agent[ExplorerDeps]:
    """Create a PydanticAI agent configured for KG exploration via Qwen."""
    url = os.environ.get("LLAMA_CPP_URL", "http://localhost:8000")
    model = OpenAIChatModel(
        "qwen3.6-27b",
        provider=OpenAIProvider(base_url=f"{url}/v1", api_key="not-needed"),
    )
    agent: Agent[ExplorerDeps] = Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        model_settings=ModelSettings(
            temperature=1.0,
            top_p=0.95,
            extra_body={
                "top_k": 20,
                "presence_penalty": 1.5,
                # qwen3.6 is a hybrid thinking model: without this the whole
                # generation lands in vLLM's reasoning field, content comes
                # back null, and the SSE stream emits an empty answer.
                **shared.inference.thinking_off(),
            },
        ),
    )

    @agent.tool
    async def search_kg(ctx: RunContext[ExplorerDeps], query: str) -> str:
        """Semantic search across the knowledge graph. Returns matching notes."""
        return await _search_kg(ctx.deps, query)

    @agent.tool
    async def expand_node(ctx: RunContext[ExplorerDeps], note_id: str) -> str:
        """Follow edges from a node to discover connected notes."""
        return await _expand_node(ctx.deps, note_id)

    @agent.tool
    async def discard_node(
        ctx: RunContext[ExplorerDeps], note_id: str, reason: str
    ) -> str:
        """Mark a node as irrelevant to the current exploration."""
        return await _discard_node(ctx.deps, note_id, reason)

    return agent


async def _search_kg(deps: ExplorerDeps, query: str) -> str:
    vector = await deps.embed_client.embed(query)
    results = deps.store.search_notes_with_context(query_embedding=vector, limit=5)
    lines = []
    for r in results:
        deps.emitter.emit(
            "node_discovered",
            {
                "note_id": r["note_id"],
                "title": r["title"],
                "type": r["type"],
                "tags": r["tags"],
                "snippet": r["snippet"],
                "edges": r.get("edges", []),
            },
        )
        lines.append(
            f"- {r['title']} (score: {r['score']:.2f}, type: {r['type']}): "
            f"{r['snippet'][:200]}"
        )
    if not lines:
        return "No results found."
    return "Found notes:\n" + "\n".join(lines)


async def _expand_node(deps: ExplorerDeps, note_id: str) -> str:
    links = deps.store.get_note_links(note_id)
    if not links:
        return f"No edges found from {note_id}."

    lines = []
    for link in links:
        target_id = link.get("resolved_note_id") or link["target_id"]
        deps.emitter.emit(
            "edge_traversed",
            {
                "from_id": note_id,
                "to_id": target_id,
                "edge_type": link.get("edge_type", "link"),
            },
        )
        target = deps.store.get_note_by_id(target_id)
        if target:
            deps.emitter.emit(
                "node_discovered",
                {
                    "note_id": target["note_id"],
                    "title": target["title"],
                    "type": target["type"],
                    "tags": target.get("tags", []),
                    "snippet": "",
                    "edges": [],
                },
            )
            edge_label = link.get("edge_type", "link")
            lines.append(f"- {target['title']} ({edge_label})")
        else:
            lines.append(f"- {target_id} (unresolved)")

    return f"Edges from {note_id}:\n" + "\n".join(lines)


async def _discard_node(deps: ExplorerDeps, note_id: str, reason: str) -> str:
    deps.emitter.emit("node_discarded", {"note_id": note_id, "reason": reason})
    return f"Discarded {note_id}: {reason}"
