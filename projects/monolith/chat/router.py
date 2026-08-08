"""Chat API routes -- backfill, explore and cluster endpoints."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from chat.backfill import run_backfill
from chat.cluster_agent import ClusterDeps, create_cluster_agent
from chat.explorer import ExplorerDeps, create_explorer_agent
from chat.sse import SSEEmitter
from knowledge.api import get_store
from shared.embedding import EmbeddingClient

logger = logging.getLogger(__name__)


def _log_backfill_exception(task: "asyncio.Task[object]") -> None:
    """Log unhandled exceptions from the backfill task."""
    if not task.cancelled() and task.exception():
        logger.error("Backfill task failed", exc_info=task.exception())


def _history_to_messages(history: list[dict]) -> list[ModelMessage]:
    """Convert a [{role, content}] history into pydantic-ai ModelMessages.

    pydantic-ai (>=1.x) requires ``message_history`` items to be ModelMessage
    objects, not plain dicts (it reads ``.conversation_id`` off each). A user
    turn becomes a ModelRequest with a UserPromptPart; anything else (assistant)
    becomes a ModelResponse with a TextPart.
    """
    messages: list[ModelMessage] = []
    for turn in history:
        content = turn["content"]
        if turn["role"] == "user":
            messages.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        else:
            messages.append(ModelResponse(parts=[TextPart(content=content)]))
    return messages


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
    from core.db import get_session

    session = next(get_session())
    emitter = SSEEmitter()
    agent = get_explorer_agent()

    deps = ExplorerDeps(
        store=get_store(session),
        embed_client=EmbeddingClient(),
        emitter=emitter,
    )

    messages = _history_to_messages(body.history)

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


class ClusterChatRequest(BaseModel):
    message: str = Field(min_length=1)
    history: list[dict] = Field(default_factory=list)


_cluster_agent = None


def get_cluster_agent():
    global _cluster_agent
    if _cluster_agent is None:
        _cluster_agent = create_cluster_agent()
    return _cluster_agent


@router.post("/cluster")
async def cluster_chat(body: ClusterChatRequest, request: Request):
    emitter = SSEEmitter()
    agent = get_cluster_agent()

    deps = ClusterDeps(emitter=emitter)

    messages = _history_to_messages(body.history)

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
            logger.exception("Cluster chat stream failed")
            emitter.emit("error", {"message": str(e)})
            emitter.close()

        async for event in emitter.stream():
            yield event

    return StreamingResponse(generate(), media_type="text/event-stream")
