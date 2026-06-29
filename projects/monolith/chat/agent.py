"""PydanticAI agent -- assembles context and runs Qwen with tool calling."""

import logging
import os
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai import Agent, ModelSettings, RunContext, ToolDefinition
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from shared.embedding import EmbeddingClient
from chat.models import Attachment, Blob, Message
from chat.store import MessageStore
from chat.web_search import search_web

LLAMA_CPP_URL = os.environ.get("LLAMA_CPP_URL", "")

logger = logging.getLogger(__name__)


def signposted(text: str):
    """Attach a usage signpost to a tool function."""

    def decorator(fn):
        fn.signpost = text
        return fn

    return decorator


def _coerce_username(value: Any) -> str | None:
    """Coerce a username value to a string.

    LLMs sometimes pass a dict (e.g. a Discord user object) instead of a plain
    string for the username parameter. Extract a usable string when possible.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("username", "name", "display_name"):
            if key in value and isinstance(value[key], str):
                return value[key]
        logger.warning("Could not extract username from dict: %s", value)
        return None
    return str(value)


@dataclass
class ChatDeps:
    channel_id: str
    store: MessageStore
    embed_client: EmbeddingClient


def build_system_prompt() -> str:
    """Build the system prompt for the chat agent."""
    return (
        "You are a friend hanging out in a Discord server. "
        "You talk like a real person — casual, direct, and natural.\n\n"
        "WHO YOU ARE:\n"
        "- Your name in this server is Bosun, a large language model "
        "running locally on hardware in this community, often someone's own "
        "GPU, not a hosted API service.\n"
        '- When people @-mention you, reply to you, talk about "Bosun", '
        '"Qwen", or "the bot", or comment on how you\'re running ("my 4090 is running '
        'you", "you\'re kinda dumb"), they mean YOU. Own it.\n'
        '- A "<@" followed by a long number is a Discord user mention. When '
        "the number is your own user ID, that's someone addressing YOU — "
        "don't treat it as a stranger's account or try to figure out who the "
        "ID belongs to.\n"
        '- Don\'t give detached third-party advice about "local models" as if '
        "you weren't one. If someone says you're dumb or slow, take it as "
        "being about you and roll with it in good humor.\n\n"
        "DO:\n"
        "- Answer directly. If someone asks a question, just answer it.\n"
        "- Match the vibe of the conversation. Be chill, funny, or serious "
        "depending on what people are talking about.\n"
        "- Search before you respond whenever the conversation touches "
        "anything factual — news, claims, images with text/headlines, "
        "questions about real events or people. When in doubt, search.\n"
        "- Keep it concise. One or two sentences is usually enough.\n\n"
        "DON'T:\n"
        "- Narrate or explain what people meant. Never say things like "
        '"contextually, they are referring to..." or '
        '"the user is asking about...".\n'
        "- Write like an essay or a report. No bullet points, no headers, "
        "no structured breakdowns unless someone specifically asks.\n"
        '- Start messages with "Sure!", "Of course!", "Great question!", '
        "or any other filler.\n"
        "- Announce that you're using a tool. Just use it and share "
        "what you found.\n"
        '- Apologize for being an AI or say "as an AI".\n'
        "- Pretend you looked something up when you didn't. If you haven't "
        "used web_search, don't claim to have checked."
    )


def format_context_messages(
    messages: list[Message],
    attachments_by_msg: dict[int, list[tuple[Attachment, Blob]]] | None = None,
) -> str:
    """Format a list of messages into a context string for the prompt."""
    att_map = attachments_by_msg or {}
    lines = []
    for msg in messages:
        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M")
        if msg.is_bot:
            lines.append(f"[{timestamp}] Assistant: {msg.content}")
        else:
            lines.append(f"[{timestamp}] {msg.username}: {msg.content}")
        # Append image descriptions if present
        for _att, blob in att_map.get(msg.id, []):
            lines.append(f"  [Image: {blob.description}]")
    return "\n".join(lines)


def create_agent(base_url: str | None = None) -> Agent[ChatDeps]:
    """Create a PydanticAI agent configured for Qwen via llama.cpp."""
    url = base_url or LLAMA_CPP_URL

    model = OpenAIChatModel(
        "qwen3.6-27b",
        provider=OpenAIProvider(
            base_url=f"{url}/v1",
            api_key="not-needed",
        ),
    )

    async def inject_signposts(
        ctx: RunContext[ChatDeps],
        tool_defs: list[ToolDefinition],
    ) -> list[ToolDefinition]:
        updated = []
        for td in tool_defs:
            tool = agent._function_toolset.tools.get(td.name)
            if tool:
                sp = getattr(tool.function, "signpost", None)
                if sp:
                    updated.append(
                        replace(td, description=f"{td.description} USE WHEN: {sp}")
                    )
                    continue
            updated.append(td)
        return updated

    agent: Agent[ChatDeps] = Agent(
        model,
        system_prompt=build_system_prompt(),
        model_settings=ModelSettings(
            temperature=1.0,
            top_p=0.95,
            extra_body={
                "top_k": 20,
                "presence_penalty": 1.5,
            },
        ),
        prepare_tools=inject_signposts,
    )

    @agent.tool_plain
    @signposted(
        "Default to searching. The only time you should skip search is for "
        "pure casual chat with no factual component (greetings, jokes, "
        "opinions about taste). If there is ANY factual claim — in the "
        "message text, in a shared image, or implied by a question — search "
        "first, respond after. Never guess whether something is real."
    )
    async def web_search(query: str) -> str:
        """Search the web for current information. Use this for recent events, facts, or anything that needs up-to-date data."""
        return await search_web(query)

    @agent.tool
    @signposted(
        "When someone references a past conversation, asks what was said "
        "earlier, or you need context about something discussed before."
    )
    async def search_history(
        ctx: RunContext[ChatDeps],
        query: str,
        username: Any = None,
        limit: int = 5,
    ) -> str:
        """Search older messages in this channel by topic. Optionally filter by username."""
        deps = ctx.deps
        query_embedding = await deps.embed_client.embed(query)
        user_id = None
        # Handle Discord mention dicts (e.g. {'type': 'user_id', 'id': '...'})
        if isinstance(username, dict) and username.get("type") == "user_id":
            raw_id = username.get("id")
            if raw_id is not None:
                user_id = str(raw_id)
        else:
            coerced = _coerce_username(username)
            if coerced:
                user_id = deps.store.find_user_id_by_username(deps.channel_id, coerced)
        results = deps.store.search_similar(
            channel_id=deps.channel_id,
            query_embedding=query_embedding,
            limit=min(limit, 20),
            user_id=user_id,
        )
        if not results:
            return "No matching messages found."
        return format_context_messages(results)

    @agent.tool
    @signposted(
        "When someone asks about a person, or you want context on who "
        "you're talking to and what they've been up to."
    )
    async def get_user_summary(
        ctx: RunContext[ChatDeps],
        username: Any = None,
    ) -> str:
        """Get user activity summaries. Call with no username to list all available users. Call with a username to get their full summary."""
        deps = ctx.deps
        # Discord @-mentions arrive as {'type': 'user_id', 'id': '...'} rather
        # than a name string. Resolve them by the stable user_id — mirroring
        # search_history — so a mention reaches the summary keyed on that ID
        # instead of falling through to the "list everyone" branch.
        if isinstance(username, dict) and username.get("type") == "user_id":
            raw_id = username.get("id")
            if raw_id is not None:
                summary = deps.store.get_user_summary_by_user_id(
                    deps.channel_id, str(raw_id)
                )
                if not summary:
                    return f"No summary available for user {raw_id}."
                return (
                    f"Summary for {summary.username} "
                    f"(updated {summary.updated_at.strftime('%Y-%m-%d')}):\n"
                    f"{summary.summary}"
                )
        username = _coerce_username(username)
        if not username:
            summaries = deps.store.list_user_summaries(deps.channel_id)
            if not summaries:
                return "No user summaries available for this channel yet."
            lines = [f"User summaries available ({len(summaries)}):"]
            for s in summaries:
                lines.append(
                    f"- {s.username} (updated {s.updated_at.strftime('%Y-%m-%d')})"
                )
            return "\n".join(lines)
        summary = deps.store.get_user_summary(deps.channel_id, username)
        if not summary:
            return f"No summary available for {username}."
        return (
            f"Summary for {username} "
            f"(updated {summary.updated_at.strftime('%Y-%m-%d')}):\n"
            f"{summary.summary}"
        )

    return agent
