"""goosecracker: the owner-gated Discord artifact agent (ADR 024 Task 4).

``/artifact <prompt>`` opens a Discord thread and runs goose in a
Firecracker microVM (the ``artifact`` recipe + tier), which builds a
self-contained HTML artifact and publishes it; fc-agentd posts the artifact URL
back into the thread (Task 5). Each owner follow-up in the thread re-runs goose
from scratch with the FULL accumulated transcript (Model B), re-publishing the
same artifact id so the live page hot-reloads.

This module is the pure logic seam (gate + transcript + dispatch + roast) so the
Discord wiring in ``chat.bot`` stays thin and this stays unit-testable. The DB
helpers are synchronous and open their own session, so the bot calls them via
``asyncio.to_thread`` (never blocking the gateway loop).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlmodel import Session

from app.db import get_engine
from chat.models import GoosecrackerSession

logger = logging.getLogger(__name__)

# The artifact recipe + model tier (ADR 024). The tier selects Gemini via
# OpenRouter (key swapped at egress) and bounds the secret placeholders the guest
# holds; the recipe is the write-only artifact builder.
ARTIFACT_RECIPE = "artifact"
ARTIFACT_TIER = "artifact"

# Shown when the owner gate rejects someone and the qwen roast path is
# unavailable (model down), so a non-owner always gets a clear refusal.
_FALLBACK_ROAST = "Nice try. /artifact is owner-only."


def owner_id() -> str:
    """The configured owner Discord user id, or "" when unset."""
    return os.environ.get("OWNER_DISCORD_USER_ID", "")


def is_owner(user_id: int | str) -> bool:
    """True only when an owner id is configured and matches.

    Fails closed: an unset OWNER_DISCORD_USER_ID rejects everyone rather than
    opening the agent (which runs arbitrary code in a microVM and spends model
    budget) to the whole server.
    """
    owner = owner_id()
    return bool(owner) and str(user_id) == owner


def _join_transcript(existing: str, message: str) -> str:
    """Append an owner turn to the curated transcript."""
    message = message.strip()
    if not existing:
        return message
    return f"{existing}\n\n{message}"


def start_session(thread_id: str, prompt: str) -> dict:
    """Record a new thread's transcript and dispatch the first artifact run.

    Synchronous (opens its own session); call via ``asyncio.to_thread``. Returns
    the dispatch result (``thread_id`` + ``action``).
    """
    prompt = prompt.strip()
    # Imported lazily (not at module load): chat.bot imports this module, and
    # agent.api -> agent.notify -> chat.bot, so a module-level import would close
    # a circular import. agent.api is the boundary-approved surface.
    from agent.api import submit

    # Dispatch first: if submit raises, no session row is left behind (which
    # would otherwise make later replies look like a live goosecracker thread
    # with no backing run).
    result = submit(
        prompt,
        recipe=ARTIFACT_RECIPE,
        tier=ARTIFACT_TIER,
        discord_thread=thread_id,
    )
    with Session(get_engine()) as session:
        session.add(GoosecrackerSession(discord_thread=thread_id, transcript=prompt))
        session.commit()
    return result


def continue_session(thread_id: str, message: str) -> dict | None:
    """Append an owner follow-up and re-dispatch the FULL transcript (Model B).

    Returns the dispatch result, or None when ``thread_id`` is not a goosecracker
    thread (so the caller can fall through to normal handling). Synchronous; call
    via ``asyncio.to_thread``.
    """
    from agent.api import submit
    from artifact import s3

    message = message.strip()
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id)
        if row is None:
            return None
        transcript = _join_transcript(row.transcript, message)
        row.transcript = transcript
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()

    # ADR 026 Phase 2: if a persisted goose session exists for this thread, resume
    # it (Model A) - send only the new reply; the guest restores the session + the
    # prior artifact and `goose run --resume` edits the page in place, hitting the
    # inference prefix cache. Otherwise cold-rebuild from the FULL transcript
    # (Model B), which also seeds the session for next time. The S3 check is the
    # primary fallback gate (Task 2.4): no stored session -> cold, never a failure.
    has_session = False
    try:
        has_session = s3.head_session(thread_id) is not None
    except Exception:
        logger.exception("goosecracker: session existence check failed; cold rebuild")
    if has_session:
        return submit(
            message,
            recipe=ARTIFACT_RECIPE,
            tier=ARTIFACT_TIER,
            discord_thread=thread_id,
            resume=True,
        )
    return submit(
        transcript,
        recipe=ARTIFACT_RECIPE,
        tier=ARTIFACT_TIER,
        discord_thread=thread_id,
    )


def is_goosecracker_thread(thread_id: str) -> bool:
    """Whether a Discord thread id has a goosecracker session. Synchronous."""
    with Session(get_engine()) as session:
        return session.get(GoosecrackerSession, thread_id) is not None


async def build_roast(attempt_text: str) -> str:
    """Roast a non-owner who tried to run the agent, via the in-cluster qwen
    model (same path as the changelog roasts). Falls back to a fixed line if the
    model is unavailable, so the gate always replies.
    """
    from chat.summarizer import build_llm_caller

    attempt_text = (attempt_text or "").strip()[:300]
    prompt = (
        "You are a cynical senior engineer. Someone who is NOT the owner just "
        "tried to run the owner-only /artifact command"
        + (f' with: "{attempt_text}"' if attempt_text else "")
        + ". Roast them in one or two dry sentences for reaching for a tool that "
        "isn't theirs. Past tense or present, declarative. No preamble, no "
        "markdown, no emoji, no hedging."
    )
    try:
        call_llm = build_llm_caller()
        roast = (await call_llm(prompt)).strip()
        return roast or _FALLBACK_ROAST
    except Exception:
        logger.exception("goosecracker: roast generation failed")
        return _FALLBACK_ROAST
