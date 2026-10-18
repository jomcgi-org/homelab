"""HTTP API for knowledge-graph-backed task tracking."""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Any, Literal

from core.db import get_session
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, field_validator
from shared.embedding import EmbeddingClient
from sqlmodel import Session

from knowledge.router import get_embedding_client
from knowledge.store import KnowledgeStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    status: Literal["todo", "in-progress", "done", "cancelled", "someday"] = "todo"
    due: str | None = None

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("due")
    @classmethod
    def calendar_date(cls, value: str | None) -> str | None:
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("due must be a calendar date in YYYY-MM-DD format")
        return value


@router.post("", status_code=201)
def create_task(
    body: CreateTaskRequest,
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    store = KnowledgeStore(session)
    note_id = store.create_task(title=body.title, status=body.status, due=body.due)
    return {"note_id": note_id}


@router.get("")
async def list_tasks(
    q: str | None = Query(default=None),
    status: str | None = Query(default=None),
    due_before: str | None = Query(default=None),
    due_after: str | None = Query(default=None),
    size: str | None = Query(default=None),
    include_someday: bool = Query(default=False),
    session: Session = Depends(get_session),
    embed_client: EmbeddingClient = Depends(get_embedding_client),
) -> dict:
    store = KnowledgeStore(session)
    if q and len(q) >= 2:
        try:
            vector = await embed_client.embed(q)
        except Exception:
            logger.exception("tasks: embedding call failed")
            raise HTTPException(status_code=503, detail="embedding unavailable")
        tasks = store.search_tasks(
            query_embedding=vector,
            statuses=status.split(",") if status else None,
            include_someday=include_someday,
        )
    else:
        tasks = store.list_tasks(
            statuses=status.split(",") if status else None,
            due_before=due_before,
            due_after=due_after,
            sizes=size.split(",") if size else None,
            include_someday=include_someday,
        )
    return {"tasks": tasks}


@router.get("/daily")
def list_tasks_daily(session: Session = Depends(get_session)) -> dict:
    store = KnowledgeStore(session)
    return {"tasks": store.list_tasks_daily()}


@router.get("/weekly")
def list_tasks_weekly(session: Session = Depends(get_session)) -> dict:
    store = KnowledgeStore(session)
    return {"tasks": store.list_tasks_weekly()}


@router.patch("/{note_id}")
def patch_task(
    note_id: str,
    body: dict[str, Any],
    session: Session = Depends(get_session),
) -> dict:
    store = KnowledgeStore(session)
    try:
        store.patch_task(note_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"patched": True}
