"""Public API for Discord and other external agent session callers."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from sqlmodel import Session

from agent_sessions import model_family, store
from agent_sessions.mcp import (
    _load_session_row,
    _persist_pending_message,
    _persist_session,
    _schedule_next_message,
    _set_session_status,
)
from core.db import get_engine
from goosecracker.api import REPO_CATALOG


async def start_session_for_thread(
    thread_id: str, prompt: str, repo: str | None, model: str = "luna"
) -> int:
    """Create a model session bound to a Discord thread and queue its first turn.

    ``thread_id`` identifies the Discord thread that should receive terminal
    notifications, ``prompt`` is the initial user request, ``repo`` selects
    the checkout or leaves it empty for an artifact run, and ``model`` selects
    the adapter family.  The returned integer is the durable session id, which
    callers use to correlate later turns and database records.
    """
    if repo is not None and repo not in REPO_CATALOG:
        raise ValueError(f"unknown repo {repo}; catalog: {', '.join(REPO_CATALOG)}")
    model_family(model)
    row = await asyncio.to_thread(
        _persist_session,
        str(uuid4()),
        "<guest>",
        "main",
        model,
        repo,
        thread_id,
    )
    await asyncio.to_thread(_persist_pending_message, row.id, prompt, model)
    _schedule_next_message(row.id)
    return row.id


async def send_to_thread_session(thread_id: str, message: str) -> dict | None:
    """Queue a follow-up message for the session bound to a Discord thread.

    ``thread_id`` scopes the message to an existing thread-owned session and
    ``message`` is the owner's next turn.  The result describes the queued turn,
    or is ``None`` when no session is bound, so callers can fall through to
    ordinary chat handling instead of claiming an unrelated thread.

    Async, and deliberately not a sync function the caller wraps in
    ``asyncio.to_thread``: ``_schedule_next_message`` calls
    ``asyncio.create_task``, which needs a running event loop on the calling
    thread. Off the loop it raises RuntimeError AFTER the turn is already
    persisted, so the owner would see a send failure for a turn that the 5s
    sweep then runs anyway. The blocking database calls get their own
    ``to_thread`` hops instead, matching ``start_session_for_thread``.
    """
    session_id = await asyncio.to_thread(session_id_for_thread, thread_id)
    if session_id is None:
        return None
    row = await asyncio.to_thread(_load_session_row, session_id)
    if row is None:
        return None
    model_family(row.model)
    turn = await asyncio.to_thread(_persist_pending_message, session_id, message, None)
    await asyncio.to_thread(_set_session_status, session_id, "running")
    _schedule_next_message(session_id)
    return {"action": "queued", "session_id": session_id, "turn": turn}


def session_id_for_thread(thread_id: str) -> int | None:
    """Find the durable session id for a Discord thread, if one is bound.

    ``thread_id`` is the Discord thread identifier.  Returning ``None`` for an
    unbound thread lets message routing distinguish agent turns from normal
    conversation without creating session state as a side effect.
    """
    with Session(get_engine()) as db_session:
        row = store.get_session_by_discord_thread(db_session, thread_id)
        return row.id if row else None
