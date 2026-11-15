"""Chat API routes: explore and cluster endpoints."""

import logging

import shared.inference
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from shared.embedding import EmbeddingClient

from chat.cluster_agent import ClusterDeps, create_cluster_agent
from chat.explorer import ExplorerDeps, create_explorer_agent, explorer_store
from chat.sse import SSEEmitter, SSEStreamingResponse

logger = logging.getLogger(__name__)


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


def _agent_sse_response(
    agent, body, deps, emitter: SSEEmitter, *, usage_label: str, failure_log: str
) -> SSEStreamingResponse:
    """Stream an agent run over SSE: text chunks, then done or error."""
    messages = _history_to_messages(body.history)

    async def produce():
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
                        usage_label,
                    )
                except Exception:
                    pass

            emitter.emit("done", {})
        except Exception as e:
            logger.exception(failure_log)
            emitter.emit("error", {"message": str(e)})
        finally:
            emitter.close()

    return SSEStreamingResponse(emitter, produce)


router = APIRouter(prefix="/api/chat", tags=["chat"])


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
    emitter = SSEEmitter()
    agent = get_explorer_agent()

    deps = ExplorerDeps(
        store=explorer_store,
        embed_client=EmbeddingClient(),
        emitter=emitter,
    )
    return _agent_sse_response(
        agent,
        body,
        deps,
        emitter,
        usage_label="explorer",
        failure_log="Explorer stream failed",
    )


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
    return _agent_sse_response(
        agent,
        body,
        deps,
        emitter,
        usage_label="cluster_agent",
        failure_log="Cluster chat stream failed",
    )
