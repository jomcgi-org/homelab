"""Qwen-generated session names for the /agents UI.

The leader runs a small refresh loop: any session whose newest turn is
beyond the turn its title was generated from (``title_turn_seq``) gets
renamed from its transcript. Qwen is self-hosted, so the calls are free;
the loop stays out of the turn-execution path entirely and a failed pass
simply leaves the session stale for the next tick. Sessions without a
title fall back to their first prompt in the router.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from sqlmodel import Session, func, select

from agent_sessions.models import AgentSession, AgentTurn
from core.db import get_engine
from framework import log_task_exception
import shared.inference

logger = logging.getLogger(__name__)

# One pass every 20s names a fresh session within a couple of UI poll
# cycles without hammering the shared vLLM.
REFRESH_INTERVAL_SECONDS = 20
BATCH_LIMIT = 3
TITLE_MAX_CHARS = 80
# Short name only. Thinking is disabled in the request, so the budget is
# spent on the name itself.
_LLM_MAX_TOKENS = 40

_TITLE_PROMPT = """You name coding-agent sessions for a session list.
Write a short name (3 to 8 words) for the session below. Describe the
task, not the outcome. Plain text only: no quotes, no markdown, no
trailing period.

First request:
{first_prompt}

Latest request:
{latest_prompt}

Latest result summary:
{latest_summary}

Name:"""

_title_task: asyncio.Task | None = None


def pick_stale_sessions(session: Session, limit: int = BATCH_LIMIT) -> list[dict]:
    """Sessions whose newest turn postdates their title, freshest first."""
    max_seq = (
        select(AgentTurn.session_id, func.max(AgentTurn.seq).label("max_seq"))
        .group_by(AgentTurn.session_id)
        .subquery()
    )
    rows = session.exec(
        select(AgentSession, max_seq.c.max_seq)
        .join(max_seq, max_seq.c.session_id == AgentSession.id)
        .where(func.coalesce(AgentSession.title_turn_seq, 0) < max_seq.c.max_seq)
        .order_by(AgentSession.last_turn_at.desc())
        .limit(limit)
    ).all()
    candidates = []
    for row, latest_seq in rows:
        first_turn = session.exec(
            select(AgentTurn)
            .where(AgentTurn.session_id == row.id)
            .order_by(AgentTurn.seq)
            .limit(1)
        ).first()
        latest_turn = session.exec(
            select(AgentTurn)
            .where(AgentTurn.session_id == row.id)
            .order_by(AgentTurn.seq.desc())  # type: ignore[union-attr]
            .limit(1)
        ).first()
        candidates.append(
            {
                "session_id": row.id,
                "turn_seq": int(latest_seq),
                "first_prompt": first_turn.prompt if first_turn else "",
                "latest_prompt": latest_turn.prompt if latest_turn else "",
                "latest_summary": (
                    (latest_turn.voice_summary or latest_turn.result_text)
                    if latest_turn
                    else ""
                ),
            }
        )
    return candidates


def store_title(session: Session, session_id: int, title: str, turn_seq: int) -> None:
    row = session.get(AgentSession, session_id)
    if row is None:
        return
    row.title = title
    row.title_turn_seq = turn_seq
    session.add(row)
    session.commit()


def build_title_prompt(candidate: dict) -> str:
    def clip(value, limit: int = 400) -> str:
        return " ".join(str(value or "").split())[:limit]

    return _TITLE_PROMPT.format(
        first_prompt=clip(candidate.get("first_prompt")),
        latest_prompt=clip(candidate.get("latest_prompt")),
        latest_summary=clip(candidate.get("latest_summary"), 300),
    )


# Quotes, backticks, and periods in either order (LLMs emit both
# `"name".` and `"name."`), plus whitespace.
_TRIM_CHARS = " \"'`“”‘’."


def sanitize_title(raw: str) -> str:
    text = " ".join(str(raw or "").split()).strip(_TRIM_CHARS)
    return text[:TITLE_MAX_CHARS].strip()


async def _call_qwen(prompt: str) -> str:
    url = os.environ.get("LLAMA_CPP_URL", "").rstrip("/")
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.post(
            f"{url}/v1/chat/completions",
            json={
                "model": "qwen3.6-27b",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": _LLM_MAX_TOKENS,
                # A thinking response spends the budget on <think> and
                # returns content: null behind a 200, so disable it.
                **shared.inference.thinking_off(),
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        if not content:
            raise RuntimeError("LLM returned empty content")
        return content


def _pick_stale_sessions_sync(limit: int = BATCH_LIMIT) -> list[dict]:
    with Session(get_engine()) as session:
        return pick_stale_sessions(session, limit)


def _store_title_sync(session_id: int, title: str, turn_seq: int) -> None:
    with Session(get_engine()) as session:
        store_title(session, session_id, title, turn_seq)


async def refresh_titles_once(call_llm=_call_qwen) -> int:
    """Name up to BATCH_LIMIT stale sessions; returns how many were named."""
    if not os.environ.get("LLAMA_CPP_URL"):
        return 0
    candidates = await asyncio.to_thread(_pick_stale_sessions_sync, BATCH_LIMIT)
    named = 0
    for candidate in candidates:
        try:
            raw = await call_llm(build_title_prompt(candidate))
        except (httpx.HTTPError, RuntimeError, KeyError, IndexError, ValueError) as exc:
            logger.warning(
                "Session naming failed for %s: %s", candidate["session_id"], exc
            )
            continue
        title = sanitize_title(raw)
        if not title:
            continue
        await asyncio.to_thread(
            _store_title_sync, candidate["session_id"], title, candidate["turn_seq"]
        )
        named += 1
    return named


async def _title_refresh_loop() -> None:
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            await refresh_titles_once()
        except Exception:
            # Never let one bad pass kill the leader task; the stale
            # sessions are retried on the next tick.
            logger.exception("Session title refresh pass failed")


def start_title_refresh_loop() -> list[asyncio.Task]:
    """Start the leader-owned title refresh loop and return its task."""
    global _title_task
    if _title_task is None or _title_task.done():
        _title_task = asyncio.create_task(_title_refresh_loop())
        _title_task.add_done_callback(log_task_exception)
    return [_title_task]
