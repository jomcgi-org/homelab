"""Moving planner HTTP API. Private-tier only.

The dashboard reads one aggregate response from ``GET /api/moving/state``.
Task writes use dedicated create, patch, delete, done, and undone endpoints.
Every route resolves its viewer through ``get_viewer``; routes never read the
identity header directly.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from core.db import get_session
from moving.collisions import find_collisions
from moving.models import Milestone, Owner, Role, Span, Task, Track
from moving.viewer import get_viewer

router = APIRouter(prefix="/api/moving", tags=["moving"])


class TaskCreateRequest(BaseModel):
    track: Track | None = None
    title: str
    note: str | None = None
    owner: Owner | None = None
    due_on: date | None = None
    value_cad: Decimal | None = None


class TaskUpdateRequest(BaseModel):
    track: Track | None = None
    title: str | None = None
    note: str | None = None
    owner: Owner | None = None
    due_on: date | None = None
    value_cad: Decimal | None = None


class TaskView(BaseModel):
    id: str
    track: Track | None
    title: str
    note: str | None
    owner: Owner | None
    due_on: date | None
    done_at: datetime | None
    value_cad: Decimal | None
    created_at: datetime


def _task_or_404(session: Session, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.get("/state")
def get_state(
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> dict:
    """Return the complete moving dashboard in one response."""
    tasks = session.exec(select(Task).order_by(Task.created_at, Task.id)).all()
    milestones = session.exec(
        select(Milestone).order_by(Milestone.occurs_on, Milestone.id)
    ).all()
    spans = session.exec(select(Span).order_by(Span.starts_on, Span.id)).all()
    roles = session.exec(select(Role).order_by(Role.company, Role.title)).all()

    total = session.exec(select(func.count(Task.id))).one()
    done = session.exec(
        select(func.count(Task.id)).where(Task.done_at.is_not(None))
    ).one()
    progress = done / total if total else 0.0

    return {
        "tasks": tasks,
        "milestones": milestones,
        "spans": spans,
        "roles": roles,
        "collisions": find_collisions(spans, tasks),
        "progress": progress,
        "viewer": viewer,
    }


@router.post("/tasks", response_model=TaskView, status_code=201)
def create_task(
    body: TaskCreateRequest,
    _viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Task:
    """Create an action item."""
    task = Task(**body.model_dump())
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


@router.patch("/tasks/{task_id}", response_model=TaskView)
def update_task(
    task_id: str,
    body: TaskUpdateRequest,
    _viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Task:
    """Patch the supplied task fields, including explicit nullable values."""
    task = _task_or_404(session, task_id)
    updates = body.model_dump(exclude_unset=True)
    if updates.get("title", task.title) is None:
        raise HTTPException(status_code=422, detail="title may not be null")
    for field, value in updates.items():
        setattr(task, field, value)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(
    task_id: str,
    _viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> None:
    """Delete an action item."""
    task = _task_or_404(session, task_id)
    session.delete(task)
    session.commit()


@router.post("/tasks/{task_id}/done", response_model=TaskView)
def mark_task_done(
    task_id: str,
    _viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Task:
    """Mark a task done, preserving its first completion timestamp on retries."""
    task = _task_or_404(session, task_id)
    if task.done_at is None:
        task.done_at = datetime.now(timezone.utc)
        session.add(task)
        session.commit()
        session.refresh(task)
    return task


@router.post("/tasks/{task_id}/undone", response_model=TaskView)
def mark_task_undone(
    task_id: str,
    _viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Task:
    """Clear a task completion timestamp; repeated calls are a no-op."""
    task = _task_or_404(session, task_id)
    if task.done_at is not None:
        task.done_at = None
        session.add(task)
        session.commit()
        session.refresh(task)
    return task
