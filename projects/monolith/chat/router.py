"""Chat API routes -- backfill and explore endpoints."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from chat.backfill import run_backfill
from chat.explorer import ExplorerDeps, create_explorer_agent
from chat.sse import SSEEmitter
from knowledge.api import get_store
from shared.embedding import EmbeddingClient

logger = logging.getLogger(__name__)


def _log_backfill_exception(task: "asyncio.Task[object]") -> None:
    """Log unhandled exceptions from the backfill task."""
    if not task.cancelled() and task.exception():
        logger.error("Backfill task failed", exc_info=task.exception())


router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("/backfill", status_code=202)
async def backfill(request: Request):
    """Launch a background backfill of all Discord channel history."""
    bot = request.app.state.bot
    if not bot:
        raise HTTPException(503, "Discord bot not running")

    task = getattr(request.app.state, "backfill_task", None)
    if task and not task.done():
        raise HTTPException(409, "Backfill already running")

    task = asyncio.create_task(run_backfill(bot))
    task.add_done_callback(_log_backfill_exception)
    request.app.state.backfill_task = task

    channels = [c for g in bot.guilds for c in g.text_channels]
    return {"status": "started", "channels": len(channels)}


class ExploreRequest(BaseModel):
    message: str = Field(min_length=1)
    history: list[dict] = Field(default_factory=list)


_explorer_agent = None


def get_explorer_agent():
    global _explorer_agent
    if _explorer_agent is None:
        _explorer_agent = create_explorer_agent()
    return _explorer_agent


@router.post("/explore")
async def explore(body: ExploreRequest, request: Request):
    from app.db import get_session

    session = next(get_session())
    emitter = SSEEmitter()
    agent = get_explorer_agent()

    deps = ExplorerDeps(
        store=get_store(session),
        embed_client=EmbeddingClient(),
        emitter=emitter,
    )

    # Build message list from history
    messages = []
    for turn in body.history:
        messages.append({"role": turn["role"], "content": turn["content"]})

    async def generate():
        try:
            async with agent.run_stream(
                body.message,
                message_history=messages if messages else None,
                deps=deps,
            ) as stream:
                async for text in stream.stream_text(delta=True):
                    emitter.emit("text_chunk", {"text": text})

            emitter.emit("done", {})
            emitter.close()
        except Exception as e:
            logger.exception("Explorer stream failed")
            emitter.emit("error", {"message": str(e)})
            emitter.close()

        async for event in emitter.stream():
            yield event

    return StreamingResponse(generate(), media_type="text/event-stream")
