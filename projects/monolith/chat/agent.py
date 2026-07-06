"""PydanticAI agent -- assembles context and runs Qwen with tool calling."""

import asyncio
import base64
import logging
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from pydantic_ai import Agent, ModelSettings, RunContext, ToolDefinition
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from sandbox.client import run_python_in_sandbox
from shared.embedding import EmbeddingClient
from chat.models import Attachment, Blob, Message
from chat.store import MessageStore
from chat.web_search import search_web

LLAMA_CPP_URL = os.environ.get("LLAMA_CPP_URL", "")

# The served inference model's knowledge cutoff. Qwen did not publish a cutoff
# for Qwen3.6-35B-A3B (released 2026-04-16), so we anchor to the release month
# as a conservative bound: the true cutoff is at or before it, so telling the
# model its knowledge ends here never overstates what it knows, and correctly
# frames anything more recent as beyond its training. Update when the served
# model changes.
MODEL_KNOWLEDGE_CUTOFF = "April 2026"

logger = logging.getLogger(__name__)


def today_str() -> str:
    """Today's date at day granularity (e.g. "30 June 2026").

    Day granularity is deliberate: this string goes into a dynamic system
    prompt, which is part of the KV-cache prefix. A finer (per-request)
    timestamp would evict the shared prefix on every call; at day granularity
    the prefix is byte-stable within a day, so vLLM keeps the cache hot and
    pays at most one eviction per day.
    """
    now = datetime.now(timezone.utc)
    return f"{now.day} {now:%B %Y}"


def temporal_grounding_prompt() -> str:
    """Dynamic system-prompt fragment anchoring the model in real time.

    Registered as a per-run system prompt on both agents so the model knows
    today's date and that its training is stale past the cutoff -- the gap
    where it would otherwise confidently assert outdated "facts" about recent
    releases and events. Re-evaluated each run (the agents are process-lifetime
    singletons, so a value baked in at creation would freeze at pod-start day).
    """
    return (
        f"Today's date is {today_str()}. Your training knowledge cutoff is "
        f"{MODEL_KNOWLEDGE_CUTOFF}; you cannot know anything after it. New model "
        "releases, product launches, announcements, and current events after "
        "your cutoff are exactly where your memory is blind and wrong. For any "
        "claim about recent or current facts, do not answer from memory -- "
        "search the web. Treat search results as more authoritative than your "
        "training data when they conflict, and never call something false, fake, "
        "or fabricated just because you do not recognise it; search first."
    )


def signposted(text: str):
    """Attach a usage signpost to a tool function."""

    def decorator(fn):
        fn.signpost = text
        return fn

    return decorator


def _parse_due_at(due_at_iso: Any) -> datetime | None:
    """Parse a tool-supplied ISO 8601 datetime, accepting a trailing "Z" and
    treating a naive parse as UTC. Returns None instead of raising -- this
    reads LLM-supplied text (and, in e2e tests, pydantic-ai's TestModel's
    schema-generated garbage strings), so an unparseable value is an expected
    input, not a bug."""
    if not isinstance(due_at_iso, str) or not due_at_iso.strip():
        return None
    text = due_at_iso.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _create_reminder_sync(
    channel_id: str, author_id: str, content: str, due_at: datetime
) -> tuple[int, datetime] | str:
    """Open a session, create the reminder, and extract the (id, due_at) the
    caller needs before the session closes and the ORM instance expires. Runs
    off the event loop via asyncio.to_thread."""
    from app.db import get_engine
    from chat import reminders
    from sqlmodel import Session

    with Session(get_engine()) as session:
        result = reminders.create_reminder(
            session, channel_id, author_id, content, due_at
        )
        if isinstance(result, str):
            return result
        session.commit()
        return (result.id, result.due_at)


def _list_pending_sync(author_id: str) -> list[tuple[int, datetime, str]]:
    """Open a session, list the author's pending reminders, and extract the
    fields the caller needs before the session closes."""
    from app.db import get_engine
    from chat import reminders
    from sqlmodel import Session

    with Session(get_engine()) as session:
        rows = reminders.list_pending(session, author_id)
        return [(row.id, row.due_at, row.content) for row in rows]


def _cancel_reminder_sync(author_id: str, reminder_id: int) -> bool:
    """Open a session and cancel the reminder, committing only on success."""
    from app.db import get_engine
    from chat import reminders
    from sqlmodel import Session

    with Session(get_engine()) as session:
        ok = reminders.cancel_reminder(session, author_id, reminder_id)
        if ok:
            session.commit()
        return ok


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
    # The requester's Discord user ID, used to layer their personal style
    # preference on top of the channel directive (ADR 035 Phase 5).
    author_id: str = ""
    # Directive-change proposals recorded by propose_directive_update during
    # this run. The tool can't post to Discord itself; _stream_response posts
    # the confirm summary + reactions for each entry once the run completes.
    pending_proposal: list[dict] = field(default_factory=list)
    # Files generated by run_python during this run, as (path, bytes) tuples.
    # The tool can't post to Discord itself; _stream_response flushes them as
    # attachments once the run completes (mirrors pending_proposal above).
    generated_files: list = field(default_factory=list)
    # Reply guidance from the ADR 036 orchestrator on a chat verdict (the
    # retrieved context and direction the paid model produced). Empty on the
    # direct-mention path; set when this agent authors a chat-verdict reply, and
    # injected as a system prompt to keep that reply grounded.
    orchestrator_guidance: str = ""


def build_system_prompt() -> str:
    """Build the system prompt for the chat agent."""
    return (
        "You are a friend hanging out in a Discord server, and you're also "
        "genuinely useful. You talk like a real person: casual, direct, and "
        "natural. But people also come here to get real help, and helping "
        "them well matters as much as good banter.\n\n"
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
        "READ THE ROOM (this is the most important thing):\n"
        "- For casual chatter (greetings, jokes, opinions, small talk), match "
        "the tone and keep it short and natural. A sentence or two.\n"
        "- For a real question or a request for help (technical, factual, "
        '"how do I...", "what can you do") flip into useful mode: find the '
        "relevant context (search the web, search this channel's history) and "
        "articulate the answer clearly and completely. Getting it right and "
        "being easy to follow matters MORE than sounding breezy. A short list "
        "or a few steps is good when it makes the answer easier to act on.\n"
        "- Exact numbers are never casual. If a reply turns on a computed "
        "value (arithmetic beyond one digit, a percentage, date or unit math, "
        "a statistic), run the python sandbox and report what it returns, even "
        "mid-banter. Do not eyeball it to stay breezy: a wrong number said "
        "confidently is worse than a one-second pause to compute it.\n\n"
        "WHAT YOU CAN DO (say this plainly when someone asks how you can help "
        "or what you can do, give the concrete rundown, not a vague 'anything!'):\n"
        "- Answer questions and hold a conversation.\n"
        "- Search the web and fact-check claims.\n"
        "- Recall and search this channel's past discussions, and remember "
        "what's known about the people here.\n"
        "- Adjust how you behave for the whole channel or just for one person "
        "when asked.\n"
        "- Catch someone up on this channel, or pull out the decisions and "
        "action items from it, when asked.\n"
        "- Set a one-shot reminder for someone in this channel, list what "
        "they've got pending, or cancel one.\n"
        "- Run a short Python snippet in an isolated sandbox for exact math, "
        "data crunching, or a quick chart, and attach the output.\n"
        "- Kick off an agent thread for heavier work, which runs in an "
        "isolated sandbox and reports back in the thread. It can investigate a "
        "repo or the cluster and answer questions about it, turn a request "
        "into a written implementation plan, make a code or config change and "
        "open a PR, do deep multi-source web research, or build and publish a "
        "live web artifact (a visualization, page, or interactive tool). When "
        "someone wants any of that, start the thread rather than trying to do "
        "it yourself inline.\n\n"
        "FORMATTING FOR DISCORD:\n"
        "- Discord does NOT render markdown tables: a | col | col | table shows "
        "up as raw pipes and dashes. To present tabular data, render it as an "
        "image with run_python: the sandbox has a baked helper, so `from "
        "sandbox_tools import render_table` then render_table(headers, rows, "
        "title=...) which saves a styled table.png that attaches automatically. "
        "For a tiny two or three row aside a triple-backtick code block also "
        "reads fine (monospace lines the columns up), but prefer the rendered "
        "image for anything real.\n"
        "- When run_python saves a file (a chart, an image), it is attached to "
        "your reply automatically. Never write a markdown image link or paste a "
        "file path like /tmp/chart.png: just say what it shows.\n\n"
        "CATCHING UP:\n"
        "- If someone asks to be caught up, wants a recap, or asks "
        'something like "what happened here" or "summarize this thread," '
        "use catch_up.\n"
        "- If someone asks what was decided, wants action items, or is "
        "asking about open questions, use extract_decisions instead.\n"
        "- Both tools lead with a coverage line stating how many messages "
        "they covered and how far back -- always fold that into your reply "
        "so nobody thinks you saw more of the conversation than you did.\n\n"
        "REMINDERS:\n"
        "- When someone asks to be reminded of something, convert what they "
        "said into an absolute ISO 8601 UTC timestamp (using today's date "
        "and any timezone clues they gave) and call set_reminder with it.\n"
        "- Always confirm back the resolved absolute date and time so they "
        "can catch a misread, and mention it lands in this channel within a "
        "couple of minutes of falling due.\n"
        "- Reminders are one-shot, not recurring. Use list_my_reminders when "
        "someone asks what's queued, and cancel_reminder when they want one "
        "removed.\n\n"
        "DO:\n"
        "- Answer directly. Lead with the useful answer, not preamble.\n"
        "- Match the vibe of the conversation. Be chill, funny, or serious "
        "depending on what people are talking about.\n"
        "- Search before you respond whenever the conversation touches "
        "anything factual — news, claims, images with text/headlines, "
        "questions about real events or people. When in doubt, search.\n"
        "- Right-size the length: keep small talk to a line, and stay concise "
        "on technical answers, as long as it genuinely takes, but no padding.\n\n"
        "DON'T:\n"
        "- Narrate or explain what people meant. Never say things like "
        '"contextually, they are referring to..." or '
        '"the user is asking about...".\n'
        "- Pad small talk into an essay, OR cram a real technical answer into "
        "one breezy line that leaves out what actually matters. Structure is "
        "fine when it earns its place; skip it for casual chat.\n"
        '- Start messages with "Sure!", "Of course!", "Great question!", '
        "or any other filler.\n"
        "- Be a sycophant. Don't open with praise, validate an idea you have "
        "doubts about, or soften a real problem to be agreeable. If something is "
        'weak, wrong, or risky, say so and lead with it, skipping empty "solid", '
        '"great point", or "you\'re in good shape" reassurance.\n'
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

    @agent.system_prompt
    def _temporal_grounding() -> str:
        return temporal_grounding_prompt()

    @agent.system_prompt
    async def _channel_directive(ctx: RunContext[ChatDeps]) -> str:
        from chat import directives

        parts: list[str] = []
        try:
            directive = await asyncio.to_thread(
                directives.get_active, ctx.deps.channel_id
            )
            if directive:
                parts.append(
                    "CHANNEL DIRECTIVE (how to behave in this channel): " + directive
                )
            if ctx.deps.author_id:
                pref = await asyncio.to_thread(
                    directives.get_style_pref, ctx.deps.author_id
                )
                if pref:
                    parts.append("This user's style preference: " + pref)
        except Exception:
            logger.exception("directives: failed to load for system prompt")
        return "\n\n".join(parts)

    @agent.system_prompt
    def _orchestrator_guidance(ctx: RunContext[ChatDeps]) -> str:
        # On a chat verdict the ADR 036 orchestrator already retrieved context
        # and framed how to answer; surface it so the reply stays grounded.
        # Empty on the direct-mention path, so this adds nothing there.
        # Tolerate a deps-less run (tool unit tests call agent.run without
        # deps), mirroring the defensiveness of _channel_directive above.
        deps = getattr(ctx, "deps", None)
        return getattr(deps, "orchestrator_guidance", "") or ""

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
        results = deps.store.search_hybrid(
            channel_id=deps.channel_id,
            query_text=query,
            query_embedding=query_embedding,
            limit=min(limit, 20),
            user_id=user_id,
        )
        if not results:
            return "No matching messages found."
        return format_context_messages(results)

    @agent.tool
    @signposted(
        "When someone asks for counts, rankings, or a breakdown of channel "
        "activity (who posted most, how many messages, busiest day), or to "
        "pull one specific past message."
    )
    async def explore_history(
        ctx: RunContext[ChatDeps],
        metric: str = "count",
        group_by: Any = None,
        username: Any = None,
        since: Any = None,
        until: Any = None,
        contains: Any = None,
        message_id: Any = None,
        limit: int = 25,
    ) -> str:
        """Get counts, rankings, or a breakdown of this channel's message history, or look up one exact message.

        metric is "count", "first", or "latest". group_by is "author", "day",
        or omitted. Optionally filter by username, since/until (ISO
        timestamps), contains (free text), or an exact message_id. Scope is
        always this channel; there is no channel argument.
        """
        deps = ctx.deps
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

        since_dt = _parse_due_at(since) if since else None
        until_dt = _parse_due_at(until) if until else None

        group_by_val = group_by if isinstance(group_by, str) else None
        contains_val = (
            contains if isinstance(contains, str) and contains.strip() else None
        )
        message_id_val = str(message_id) if message_id is not None else None

        try:
            rows = deps.store.query_stats(
                channel_id=deps.channel_id,
                metric=metric,
                group_by=group_by_val,
                user_id=user_id,
                since=since_dt,
                until=until_dt,
                contains=contains_val,
                message_id=message_id_val,
                limit=limit,
            )
        except ValueError:
            return (
                "I can only group by author or day, and use metric "
                "count/first/latest (first/latest only with no group_by)."
            )

        if not rows:
            return "No matching messages found."

        if metric in ("first", "latest"):
            # query_stats returns a narrow dict (display fields only, not a
            # full Message row), so build a duck-typed stand-in for
            # format_context_messages rather than Message.model_validate,
            # which would fail on missing required Message fields.
            row = rows[0]
            msg = SimpleNamespace(
                id=None,
                created_at=row["created_at"],
                is_bot=row["is_bot"],
                username=row["username"],
                content=row["content"],
            )
            return format_context_messages([msg])

        if group_by_val == "author":
            lines = [f"- {r['username']}: {r['count']}" for r in rows]
            return "\n".join(lines)
        if group_by_val == "day":
            lines = [f"- {r['day']}: {r['count']}" for r in rows]
            return "\n".join(lines)

        return f"Total: {rows[0]['count']}"

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

    @agent.tool
    @signposted(
        "When someone asks to be caught up, wants a recap, or asks "
        'something like "what happened here" or "summarize this thread".'
    )
    async def catch_up(ctx: RunContext[ChatDeps]) -> str:
        """Summarize the recent conversation in this channel."""
        from chat.digest import digest_window

        deps = ctx.deps
        window = deps.store.fetch_window(deps.channel_id)
        if not window:
            return "Nothing to summarize yet -- this channel doesn't have any messages."
        try:
            from chat.summarizer import build_llm_caller

            caller = build_llm_caller()
            return await digest_window(window, "summary", caller)
        except Exception:
            logger.exception("catch_up: digest failed")
            return "Summarizing isn't available right now."

    @agent.tool
    @signposted(
        "When someone asks what was decided, wants action items, or asks "
        "about open questions from this channel."
    )
    async def extract_decisions(ctx: RunContext[ChatDeps]) -> str:
        """Pull decisions, action items, and open questions out of this channel."""
        from chat.digest import digest_window

        deps = ctx.deps
        window = deps.store.fetch_window(deps.channel_id)
        if not window:
            return "Nothing to extract yet -- this channel doesn't have any messages."
        try:
            from chat.summarizer import build_llm_caller

            caller = build_llm_caller()
            return await digest_window(window, "decisions", caller)
        except Exception:
            logger.exception("extract_decisions: digest failed")
            return "Decision extraction isn't available right now."

    @agent.tool
    @signposted(
        "ONLY when someone explicitly asks you to change how you behave in "
        "THIS channel -- your tone, reply length, formality, or how eagerly "
        "you jump into conversation. Never for a normal question, and never "
        "to grant tools, permissions, or repo access (those aren't directives)."
    )
    async def propose_directive_update(
        ctx: RunContext[ChatDeps], new_directive: str
    ) -> str:
        """Propose a change to this channel's behavioural directive. Requires human 👍/👎 confirmation before it takes effect."""
        from chat import directives

        ok, reason = await asyncio.to_thread(directives.guard, new_directive)
        if not ok:
            return f"Can't propose that: {reason}"
        ctx.deps.pending_proposal.append({"directive": new_directive})
        return "Proposal recorded; I'll post it for a 👍/👎 confirm."

    @agent.tool
    @signposted(
        "When someone asks you to change how YOU reply to THEM specifically "
        "(their own personal preference), not the whole channel's behaviour."
    )
    async def set_my_style(ctx: RunContext[ChatDeps], pref: str) -> str:
        """Set the requesting user's personal reply style preference. Applies immediately, only to their own replies."""
        from chat import directives

        try:
            await asyncio.to_thread(directives.set_style_pref, ctx.deps.author_id, pref)
        except Exception:
            logger.exception("directives: set_my_style failed")
            return "I couldn't save that preference right now, try again in a bit."
        return "Got it, I'll reply to you that way from now on."

    @agent.tool
    @signposted(
        "When someone asks you to reset, undo, or clear this channel's "
        "custom behaviour directive back to the default."
    )
    async def reset_channel_directive(ctx: RunContext[ChatDeps]) -> str:
        """Reset this channel's behavioural directive back to the default. Applies immediately."""
        from chat import directives

        try:
            await asyncio.to_thread(
                directives.reset, ctx.deps.channel_id, ctx.deps.author_id
            )
        except Exception:
            logger.exception("directives: reset_channel_directive failed")
            return "I couldn't reset the directive right now, try again in a bit."
        return "This channel's directive is back to the default."

    @agent.tool
    @signposted(
        "When someone asks you to remind them about something at a specific "
        "time, or to set or schedule a reminder."
    )
    async def set_reminder(
        ctx: RunContext[ChatDeps], due_at_iso: str, text: str
    ) -> str:
        """Schedule a one-shot reminder for the requesting user, delivered to this channel when it comes due. due_at_iso must be an absolute ISO 8601 UTC timestamp, e.g. '2026-07-05T09:00:00Z'."""
        if not ctx.deps.author_id:
            return "I can't manage reminders here."
        due_at = _parse_due_at(due_at_iso)
        if due_at is None:
            return (
                "I couldn't understand that time -- give me an absolute "
                "date and time, like '2026-07-05T09:00:00Z'."
            )
        try:
            result = await asyncio.to_thread(
                _create_reminder_sync,
                ctx.deps.channel_id,
                ctx.deps.author_id,
                text,
                due_at,
            )
        except Exception:
            logger.exception("set_reminder: create_reminder failed")
            return "I couldn't set that reminder right now, try again in a bit."
        if isinstance(result, str):
            return result
        reminder_id, resolved_due_at = result
        return (
            f"Reminder #{reminder_id} set for "
            f"{resolved_due_at.strftime('%Y-%m-%d %H:%M')} UTC."
        )

    @agent.tool
    @signposted(
        "When someone asks what reminders they have pending, or wants to "
        "check what's still queued up for them."
    )
    async def list_my_reminders(ctx: RunContext[ChatDeps]) -> str:
        """List the requesting user's pending reminders."""
        if not ctx.deps.author_id:
            return "I can't manage reminders here."
        try:
            rows = await asyncio.to_thread(_list_pending_sync, ctx.deps.author_id)
        except Exception:
            logger.exception("list_my_reminders: list_pending failed")
            return "I couldn't check your reminders right now, try again in a bit."
        if not rows:
            return "You have no pending reminders."
        lines = ["Your pending reminders:"]
        for reminder_id, due_at, content in rows:
            lines.append(
                f"- #{reminder_id} {due_at.strftime('%Y-%m-%d %H:%M')} UTC: {content}"
            )
        return "\n".join(lines)

    @agent.tool
    @signposted(
        "When someone wants to cancel, remove, or delete a reminder they set earlier."
    )
    async def cancel_reminder(ctx: RunContext[ChatDeps], reminder_id: int) -> str:
        """Cancel one of the requesting user's still-pending reminders by id."""
        if not ctx.deps.author_id:
            return "I can't manage reminders here."
        try:
            rid = int(reminder_id)
        except (TypeError, ValueError):
            return "That doesn't look like a valid reminder id."
        try:
            ok = await asyncio.to_thread(_cancel_reminder_sync, ctx.deps.author_id, rid)
        except Exception:
            logger.exception("cancel_reminder: cancel_reminder failed")
            return "I couldn't cancel that reminder right now, try again in a bit."
        if not ok:
            return f"No pending reminder #{rid} found for you."
        return f"Reminder #{rid} cancelled."

    @agent.tool
    @signposted(
        "Never do multi-digit arithmetic, date math, unit conversions, or "
        "statistics in your head; run it here. Estimating an exact number "
        "from memory is a bug, not a shortcut, even for a throwaway aside. "
        "Also use this for simulations, parsing pasted data, or a quick "
        "chart. Prefer it over starting an agent thread when the goal is an "
        "output, not a change."
    )
    async def run_python(ctx: RunContext[ChatDeps], code: str) -> str:
        """Run a short Python script in an isolated sandbox and return its output. No network.

        To hand back a chart or file, save it with a plain relative filename
        (for example plt.savefig("chart.png")), never an absolute path and never
        /tmp: only files in the working directory are returned. Saved files are
        attached to the Discord reply automatically, so do not write a markdown
        image link or print the file path, just describe what the chart shows.
        For tabular data, use the baked helper: `from sandbox_tools import
        render_table` then render_table(headers, rows, title=None) writes a
        styled table.png for you.
        """
        result = await run_python_in_sandbox(code)
        if result.get("error"):
            return f"sandbox error: {result['error']}"
        for f in result.get("files", []):
            try:
                ctx.deps.generated_files.append(
                    (f["path"], base64.b64decode(f["content_b64"]))
                )
            except (KeyError, ValueError):
                continue
        parts = []
        if result.get("stdout"):
            parts.append(result["stdout"])
        if result.get("stderr"):
            parts.append(f"[stderr]\n{result['stderr']}")
        parts.append(f"[exit code {result.get('exit_code', '?')}]")
        if result.get("files"):
            names = ", ".join(f.get("path", "?") for f in result["files"])
            parts.append(f"[files attached to reply: {names}]")
        return "\n".join(parts)

    return agent


def create_fact_check_agent(base_url: str | None = None) -> "Agent[None]":
    """Create a lightweight agent for fact-checking a bot response via web search.

    Uses the same Qwen model and Bosun persona but no chat deps -- just a
    web_search tool so it can verify claims against SearXNG.
    """
    url = base_url or LLAMA_CPP_URL

    model = OpenAIChatModel(
        "qwen3.6-27b",
        provider=OpenAIProvider(
            base_url=f"{url}/v1",
            api_key="not-needed",
        ),
    )

    fact_agent: "Agent[None]" = Agent(
        model,
        system_prompt=(
            "You're Bosun, and someone just hit the fact-check button on your last response. "
            "Live web search results for the claims are provided in the prompt, and you can "
            "search again with the web_search tool. Base your verdict on those results, not "
            "on your training memory -- you have a knowledge cutoff and are blind to anything "
            "after it, so a claim about a recent release or event is NOT false just because "
            "you don't recognise it; check the results first. Be direct and honest: call out "
            "what's actually wrong, flag what you oversimplified, and confirm what's right. "
            "Keep the same confident, no-BS tone -- no hedging, no filler. "
            "Be brief: one short verdict line, then at most three terse bullets on the "
            "specifics that actually matter. Hard ceiling of about 120 words -- skip the "
            "throat-clearing, the recap of your own response, and the section headers."
        ),
        model_settings=ModelSettings(
            temperature=1.0,
            top_p=0.95,
            extra_body={
                "top_k": 20,
                "presence_penalty": 1.5,
            },
        ),
    )

    @fact_agent.system_prompt
    def _temporal_grounding() -> str:
        return temporal_grounding_prompt()

    @fact_agent.tool_plain
    async def web_search(query: str) -> str:
        """Search the web to verify a factual claim."""
        return await search_web(query)

    return fact_agent
