"""Chat API routes -- backfill and explore endpoints."""

import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException, Request, Response
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


# Internal router (ADR 024): the goosecracker guest streams goose's stdout here
# as the build runs, and the in-process Discord bot reads the buffer to edit the
# thread message live. Prefixed /internal so it is kept off the public HTTPRoute
# (in-cluster only, reached by the guest through the egress funnel), like
# artifact's /internal/artifact.
internal_router = APIRouter(prefix="/internal/goosecracker", tags=["goosecracker"])

# Artifact ids are the Discord thread id (numeric), but accept the same charset
# as the artifact router so the two validations never disagree.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ProgressIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    chunk: str = ""
    done: bool = False


@internal_router.post("/progress", status_code=204)
async def post_progress(body: ProgressIn) -> Response:
    """Append a guest stdout chunk (and/or a done marker) to a run's buffer."""
    from chat import goosecracker_progress as gp

    if not _ID_RE.match(body.id):
        raise HTTPException(400, "invalid id")
    if body.chunk:
        gp.append(body.id, body.chunk + "\n")
    if body.done:
        gp.mark_done(body.id)
    return Response(status_code=204)


class ProgressChunkIn(BaseModel):
    chunk: str = ""
    done: bool = False


@internal_router.post("/progress/{run_id}", status_code=204)
async def post_progress_by_id(run_id: str, body: ProgressChunkIn) -> Response:
    """Append a guest stdout chunk to a run's buffer, keyed by the URL path.

    The fc-invoke agent guest posts an id-less ``{"chunk": ...}`` body to the
    per-session progress URL the runner hands it (``.../progress/{session}``), so
    the run id rides in the path here rather than the body. Same in-memory buffer,
    keyed by ``run_id`` (== the Discord thread the bot polls), so the live stream
    just works.

    The guest streams goose stdout one line per POST, and its scanner strips the
    trailing newline, so each chunk is a complete line with no newline. Restore
    it before appending: the buffer's marker parser treats a marker-prefixed
    fragment with no trailing newline as an incomplete line and holds it (waiting
    for a newline that never arrives on the next, different line), so without this
    every ``::stage::`` marker would accumulate unparsed and the checklist would
    never render (ADR 035).
    """
    from chat import goosecracker_progress as gp

    if not _ID_RE.match(run_id):
        raise HTTPException(400, "invalid id")
    if body.chunk:
        gp.append(run_id, body.chunk + "\n")
    if body.done:
        gp.mark_done(run_id)
    return Response(status_code=204)


@internal_router.get("/steering/{token}")
async def get_steering(token: str, after_id: int = 0) -> dict:
    """Return undelivered steering for the session addressed by an unguessable
    per-session token (NOT the thread id), and mark it delivered. The runner
    injects this token-keyed URL into the guest; keying on the token (not the
    guessable Discord thread snowflake) stops a compromised guest from reading,
    denying, or polluting another thread's steering. In-cluster only, like the
    progress sink."""
    from chat import goosecracker

    if not _ID_RE.match(token):
        raise HTTPException(400, "invalid token")
    thread_id = await asyncio.to_thread(goosecracker.thread_for_steering_token, token)
    if thread_id is None:
        return {"messages": []}
    messages = await asyncio.to_thread(goosecracker.fetch_steering, thread_id, after_id)
    return {"messages": messages}
