"""Chat API routes -- backfill, explore and cluster endpoints."""

import asyncio
import logging
from collections.abc import Awaitable
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from chat.backfill import BackfillProgress, run_backfill
from chat.cluster_agent import ClusterDeps, create_cluster_agent
from chat.explorer import ExplorerDeps, create_explorer_agent
from chat.sse import SSEEmitter
from knowledge.api import get_store
from shared.embedding import EmbeddingClient
import shared.inference

logger = logging.getLogger(__name__)


def _log_backfill_exception(task: "asyncio.Task[object]") -> None:
    """Log unhandled exceptions from the backfill task."""
    if not task.cancelled() and task.exception():
        logger.error("Backfill task failed", exc_info=task.exception())


DiscordChannelId = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]


class BackfillRequest(BaseModel):
    """Optional scope for a backfill. Omitted or null means all channels."""

    model_config = ConfigDict(extra="forbid")

    channel_ids: Annotated[list[DiscordChannelId] | None, Field(min_length=1)] = None

    @field_validator("channel_ids")
    @classmethod
    def validate_channel_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) != len(set(value)):
            raise ValueError("channel_ids must not contain duplicates")
        if any(int(channel_id) > (1 << 64) - 1 for channel_id in value):
            raise ValueError("channel_ids must be unsigned 64-bit Discord snowflakes")
        return value


class BackfillStatus(BaseModel):
    """Inspectable lifecycle and progress for the most recent backfill."""

    status: Literal["idle", "running", "success", "failure", "cancelled"]
    channel_ids: list[str] | None = None
    channels_total: int = 0
    channels_completed: int = 0
    messages_stored: int = 0
    messages_skipped: int = 0
    current_channel_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


def _update_backfill_progress(
    status: BackfillStatus, progress: BackfillProgress
) -> None:
    status.channels_total = progress.channels_total
    status.channels_completed = progress.channels_completed
    status.messages_stored = progress.messages_stored
    status.messages_skipped = progress.messages_skipped
    status.current_channel_id = progress.current_channel_id


async def _track_backfill(
    operation: Awaitable[BackfillProgress], status: BackfillStatus
) -> None:
    try:
        progress = await operation
    except asyncio.CancelledError:
        status.status = "cancelled"
        status.current_channel_id = None
        status.finished_at = datetime.now(timezone.utc)
        raise
    except Exception as exc:
        status.status = "failure"
        status.current_channel_id = None
        status.finished_at = datetime.now(timezone.utc)
        status.error = str(exc)
        raise
    else:
        _update_backfill_progress(status, progress)
        status.status = "success"
        status.current_channel_id = None
        status.finished_at = datetime.now(timezone.utc)


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
async def backfill(request: Request, body: BackfillRequest | None = None):
    """Launch a background backfill for all or selected Discord channels."""
    bot = request.app.state.bot
    if not bot:
        raise HTTPException(503, "Discord bot not running")

    task = getattr(request.app.state, "backfill_task", None)
    if task and not task.done():
        raise HTTPException(409, "Backfill already running")

    channel_ids = body.channel_ids if body is not None else None
    channels = [c for g in bot.guilds for c in g.text_channels]
    if channel_ids is not None:
        visible_ids = {str(channel.id) for channel in channels}
        unknown_ids = sorted(set(channel_ids) - visible_ids)
        if unknown_ids:
            raise HTTPException(
                422, f"Unknown or inaccessible channel_ids: {', '.join(unknown_ids)}"
            )
        requested_ids = set(channel_ids)
        channels = [c for c in channels if str(c.id) in requested_ids]

    status = BackfillStatus(
        status="running",
        channel_ids=channel_ids,
        channels_total=len(channels),
        started_at=datetime.now(timezone.utc),
    )
    operation = run_backfill(
        bot,
        channel_ids=channel_ids,
        on_progress=lambda progress: _update_backfill_progress(status, progress),
    )
    task = asyncio.create_task(_track_backfill(operation, status))
    task.add_done_callback(_log_backfill_exception)
    request.app.state.backfill_task = task
    request.app.state.backfill_status = status

    return {"status": "started", "channels": len(channels)}


@router.get("/backfill/status", response_model=BackfillStatus)
async def backfill_status(request: Request) -> BackfillStatus:
    """Return progress and the terminal result of the most recent backfill."""
    status = getattr(request.app.state, "backfill_status", None)
    if status is None:
        return BackfillStatus(status="idle")
    return status


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
                try:
                    usage = stream.usage
                    shared.inference.record_usage(
                        {
                            "prompt_tokens": usage.input_tokens,
                            "completion_tokens": usage.output_tokens,
                            "total_tokens": usage.total_tokens,
                        },
                        shared.inference.META_SPARK_MODEL,
                        "explorer",
                    )
                except Exception:
                    pass

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
                try:
                    usage = stream.usage
                    shared.inference.record_usage(
                        {
                            "prompt_tokens": usage.input_tokens,
                            "completion_tokens": usage.output_tokens,
                            "total_tokens": usage.total_tokens,
                        },
                        shared.inference.META_SPARK_MODEL,
                        "cluster_agent",
                    )
                except Exception:
                    pass

            emitter.emit("done", {})
            emitter.close()
        except Exception as e:
            logger.exception("Cluster chat stream failed")
            emitter.emit("error", {"message": str(e)})
            emitter.close()

        async for event in emitter.stream():
            yield event

    return StreamingResponse(generate(), media_type="text/event-stream")
