"""Moving planner HTTP API. Private-tier only.

The dashboard reads one aggregate response from ``GET /api/moving/state``.
Task writes use dedicated create, patch, delete, done, and undone endpoints.
Every route resolves its viewer through ``get_viewer``; routes never read the
identity header directly.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
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
    owner: Owner
    due_on: date | None
    done_at: datetime | None
    value_cad: Decimal | None
    created_at: datetime


def _task_or_404(session: Session, task_id: uuid.UUID) -> Task:
    task = session.get(Task, str(task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


def _editable_task_or_403(session: Session, task_id: uuid.UUID, viewer: str) -> Task:
    """Load a task and enforce its write ownership at one audit point."""
    task = _task_or_404(session, task_id)
    if task.owner not in (viewer, "both"):
        raise HTTPException(status_code=403, detail="task belongs to another viewer")
    return task


@router.get("/state")
def get_state(
    scope: Literal["mine", "all"] = "mine",
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> dict:
    """Return the moving dashboard scoped to the viewer by default."""
    task_query = select(Task).order_by(Task.created_at, Task.id)
    milestone_query = select(Milestone).order_by(Milestone.occurs_on, Milestone.id)
    span_query = select(Span).order_by(Span.starts_on, Span.id)
    role_query = select(Role).order_by(Role.company, Role.title)
    if scope == "mine":
        owners = (viewer, "both")
        task_query = task_query.where(Task.owner.in_(owners))
        milestone_query = milestone_query.where(Milestone.owner.in_(owners))
        span_query = span_query.where(Span.owner.in_(owners))
        role_query = role_query.where(Role.owner.in_(owners))

    tasks = session.exec(task_query).all()
    milestones = session.exec(milestone_query).all()
    spans = session.exec(span_query).all()
    roles = session.exec(role_query).all()

    done = sum(task.done_at is not None for task in tasks)
    progress = done / len(tasks) if tasks else 0.0

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
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Task:
    """Create an action item."""
    if "owner" in body.model_fields_set:
        if body.owner is None:
            raise HTTPException(status_code=422, detail="owner may not be null")
        owner = body.owner
    else:
        owner = viewer
    task = Task(**body.model_dump(exclude={"owner"}), owner=owner)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


@router.patch("/tasks/{task_id}", response_model=TaskView)
def update_task(
    task_id: uuid.UUID,
    body: TaskUpdateRequest,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Task:
    """Patch the supplied task fields, including explicit nullable values."""
    task = _editable_task_or_403(session, task_id, viewer)
    updates = body.model_dump(exclude_unset=True)
    if updates.get("title", task.title) is None:
        raise HTTPException(status_code=422, detail="title may not be null")
    if updates.get("owner", task.owner) is None:
        raise HTTPException(status_code=422, detail="owner may not be null")
    for field, value in updates.items():
        setattr(task, field, value)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(
    task_id: uuid.UUID,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> None:
    """Delete an action item."""
    task = _editable_task_or_403(session, task_id, viewer)
    session.delete(task)
    session.commit()


@router.post("/tasks/{task_id}/done", response_model=TaskView)
def mark_task_done(
    task_id: uuid.UUID,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Task:
    """Mark a task done, preserving its first completion timestamp on retries."""
    task = _editable_task_or_403(session, task_id, viewer)
    if task.done_at is None:
        task.done_at = datetime.now(timezone.utc)
        session.add(task)
        session.commit()
        session.refresh(task)
    return task


@router.post("/tasks/{task_id}/undone", response_model=TaskView)
def mark_task_undone(
    task_id: uuid.UUID,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Task:
    """Clear a task completion timestamp; repeated calls are a no-op."""
    task = _editable_task_or_403(session, task_id, viewer)
    if task.done_at is not None:
        task.done_at = None
        session.add(task)
        session.commit()
        session.refresh(task)
    return task
