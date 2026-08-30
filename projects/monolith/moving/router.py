"""Moving planner HTTP API. Private-tier only.

The dashboard reads one aggregate response from ``GET /api/moving/state``.
Tasks, spans, milestones, and roles have dedicated create, patch, and delete
endpoints; tasks add done and undone toggles, and computed collisions can be
acknowledged. Every route resolves its viewer through ``get_viewer``; routes
never read the identity header directly.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from core.db import get_session
from moving.collisions import find_collisions
from moving.models import (
    CollisionAck,
    GcalState,
    Milestone,
    Owner,
    Role,
    RoleStage,
    Span,
    SpanKind,
    Task,
    Track,
    ViewerName,
)
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


def _editable_or_403(
    session: Session,
    model_cls: type,
    entity_id: uuid.UUID,
    viewer: str,
    noun: str,
) -> Task | Span | Milestone | Role:
    """Load a row and enforce its write ownership at one audit point."""
    row = session.get(model_cls, str(entity_id))
    if row is None:
        raise HTTPException(status_code=404, detail=f"{noun} not found")
    if row.owner not in (viewer, "both"):
        raise HTTPException(status_code=403, detail=f"{noun} belongs to another viewer")
    return row


def _owner_or_viewer(body: BaseModel, viewer: str) -> str:
    """An explicit owner wins; an unset owner defaults to the caller."""
    if "owner" in body.model_fields_set:
        if body.owner is None:
            raise HTTPException(status_code=422, detail="owner may not be null")
        return body.owner
    return viewer


def _reject_null_updates(updates: dict, row: object, fields: tuple[str, ...]) -> None:
    """422 when a patch would null a field the schema requires."""
    for field in fields:
        if updates.get(field, getattr(row, field)) is None:
            raise HTTPException(status_code=422, detail=f"{field} may not be null")


def _date_order_or_422(starts_on: date, ends_on: date) -> None:
    """Surface the spans date-order CHECK as a 422 instead of a DB error."""
    if ends_on < starts_on:
        raise HTTPException(status_code=422, detail="ends_on may not precede starts_on")


def _span_exists_or_422(session: Session, span_id: str | None) -> None:
    if span_id is not None and session.get(Span, span_id) is None:
        raise HTTPException(status_code=422, detail="span not found")


def _ack_pair(item1_id: uuid.UUID, item2_id: uuid.UUID) -> tuple[str, str]:
    """Acks are stored sorted so either argument order reaches the same row."""
    first, second = sorted((str(item1_id), str(item2_id)))
    return first, second


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

    acks = {
        (ack.item1_id, ack.item2_id): ack
        for ack in session.exec(select(CollisionAck)).all()
    }
    collisions = []
    for collision in find_collisions(spans, tasks):
        ack = acks.get(tuple(sorted((collision.item1_id, collision.item2_id))))
        collisions.append(
            asdict(collision)
            | {
                "acked_by": ack.acked_by if ack else None,
                "ack_note": ack.note if ack else None,
            }
        )

    return {
        "tasks": tasks,
        "milestones": milestones,
        "spans": spans,
        "roles": roles,
        "collisions": collisions,
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
    task = Task(
        **body.model_dump(exclude={"owner"}), owner=_owner_or_viewer(body, viewer)
    )
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
    task = _editable_or_403(session, Task, task_id, viewer, "task")
    updates = body.model_dump(exclude_unset=True)
    _reject_null_updates(updates, task, ("title", "owner"))
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
    task = _editable_or_403(session, Task, task_id, viewer, "task")
    session.delete(task)
    session.commit()


@router.post("/tasks/{task_id}/done", response_model=TaskView)
def mark_task_done(
    task_id: uuid.UUID,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Task:
    """Mark a task done, preserving its first completion timestamp on retries."""
    task = _editable_or_403(session, Task, task_id, viewer, "task")
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
    task = _editable_or_403(session, Task, task_id, viewer, "task")
    if task.done_at is not None:
        task.done_at = None
        session.add(task)
        session.commit()
        session.refresh(task)
    return task


class SpanCreateRequest(BaseModel):
    kind: SpanKind
    label: str
    starts_on: date
    ends_on: date
    owner: Owner | None = None


class SpanUpdateRequest(BaseModel):
    kind: SpanKind | None = None
    label: str | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    owner: Owner | None = None


class SpanView(BaseModel):
    id: str
    kind: SpanKind
    label: str
    starts_on: date
    ends_on: date
    owner: Owner


@router.post("/spans", response_model=SpanView, status_code=201)
def create_span(
    body: SpanCreateRequest,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Span:
    """Create a schedule span."""
    _date_order_or_422(body.starts_on, body.ends_on)
    span = Span(
        **body.model_dump(exclude={"owner"}), owner=_owner_or_viewer(body, viewer)
    )
    session.add(span)
    session.commit()
    session.refresh(span)
    return span


@router.patch("/spans/{span_id}", response_model=SpanView)
def update_span(
    span_id: uuid.UUID,
    body: SpanUpdateRequest,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Span:
    """Patch the supplied span fields; none of them are nullable."""
    span = _editable_or_403(session, Span, span_id, viewer, "span")
    updates = body.model_dump(exclude_unset=True)
    _reject_null_updates(
        updates, span, ("kind", "label", "starts_on", "ends_on", "owner")
    )
    _date_order_or_422(
        updates.get("starts_on", span.starts_on),
        updates.get("ends_on", span.ends_on),
    )
    for field, value in updates.items():
        setattr(span, field, value)
    session.add(span)
    session.commit()
    session.refresh(span)
    return span


@router.delete("/spans/{span_id}", status_code=204)
def delete_span(
    span_id: uuid.UUID,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> None:
    """Delete a span; linked roles keep existing with span_id cleared."""
    span = _editable_or_403(session, Span, span_id, viewer, "span")
    session.delete(span)
    session.commit()


class MilestoneCreateRequest(BaseModel):
    title: str
    occurs_on: date
    owner: Owner | None = None
    gcal_state: GcalState = "queued"


class MilestoneUpdateRequest(BaseModel):
    title: str | None = None
    occurs_on: date | None = None
    owner: Owner | None = None
    gcal_state: GcalState | None = None


class MilestoneView(BaseModel):
    id: str
    title: str
    occurs_on: date
    owner: Owner
    gcal_event_id: str | None
    gcal_synced_at: datetime | None
    gcal_state: GcalState


@router.post("/milestones", response_model=MilestoneView, status_code=201)
def create_milestone(
    body: MilestoneCreateRequest,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Milestone:
    """Create a milestone. gcal_event_id and gcal_synced_at belong to the sync job."""
    milestone = Milestone(
        **body.model_dump(exclude={"owner"}), owner=_owner_or_viewer(body, viewer)
    )
    session.add(milestone)
    session.commit()
    session.refresh(milestone)
    return milestone


@router.patch("/milestones/{milestone_id}", response_model=MilestoneView)
def update_milestone(
    milestone_id: uuid.UUID,
    body: MilestoneUpdateRequest,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Milestone:
    """Patch the supplied milestone fields; none of them are nullable."""
    milestone = _editable_or_403(session, Milestone, milestone_id, viewer, "milestone")
    updates = body.model_dump(exclude_unset=True)
    _reject_null_updates(
        updates, milestone, ("title", "occurs_on", "owner", "gcal_state")
    )
    for field, value in updates.items():
        setattr(milestone, field, value)
    session.add(milestone)
    session.commit()
    session.refresh(milestone)
    return milestone


@router.delete("/milestones/{milestone_id}", status_code=204)
def delete_milestone(
    milestone_id: uuid.UUID,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> None:
    """Delete a milestone."""
    milestone = _editable_or_403(session, Milestone, milestone_id, viewer, "milestone")
    session.delete(milestone)
    session.commit()


class RoleCreateRequest(BaseModel):
    company: str
    title: str
    owner: Owner | None = None
    stage: RoleStage | None = None
    next_on: date | None = None
    span_id: uuid.UUID | None = None


class RoleUpdateRequest(BaseModel):
    company: str | None = None
    title: str | None = None
    owner: Owner | None = None
    stage: RoleStage | None = None
    next_on: date | None = None
    span_id: uuid.UUID | None = None


class RoleView(BaseModel):
    id: str
    company: str
    title: str
    owner: Owner
    stage: RoleStage | None
    next_on: date | None
    span_id: str | None


@router.post("/roles", response_model=RoleView, status_code=201)
def create_role(
    body: RoleCreateRequest,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Role:
    """Create a job application record, optionally linked to a span."""
    span_id = str(body.span_id) if body.span_id is not None else None
    _span_exists_or_422(session, span_id)
    role = Role(
        **body.model_dump(exclude={"owner", "span_id"}),
        owner=_owner_or_viewer(body, viewer),
        span_id=span_id,
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


@router.patch("/roles/{role_id}", response_model=RoleView)
def update_role(
    role_id: uuid.UUID,
    body: RoleUpdateRequest,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> Role:
    """Patch role fields; stage, next_on, and span_id accept explicit null."""
    role = _editable_or_403(session, Role, role_id, viewer, "role")
    updates = body.model_dump(exclude_unset=True)
    _reject_null_updates(updates, role, ("company", "title", "owner"))
    if updates.get("span_id") is not None:
        updates["span_id"] = str(updates["span_id"])
        _span_exists_or_422(session, updates["span_id"])
    for field, value in updates.items():
        setattr(role, field, value)
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


@router.delete("/roles/{role_id}", status_code=204)
def delete_role(
    role_id: uuid.UUID,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> None:
    """Delete a job application record."""
    role = _editable_or_403(session, Role, role_id, viewer, "role")
    session.delete(role)
    session.commit()


class CollisionAckRequest(BaseModel):
    note: str | None = None


class CollisionAckView(BaseModel):
    item1_id: str
    item2_id: str
    note: str | None
    acked_by: ViewerName
    acked_at: datetime


@router.post("/collisions/{item1_id}/{item2_id}/ack", response_model=CollisionAckView)
def ack_collision(
    item1_id: uuid.UUID,
    item2_id: uuid.UUID,
    body: CollisionAckRequest,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> CollisionAck:
    """Record that a collision is understood and accepted.

    Any viewer may acknowledge: a collision is shared judgment, not owned data.
    Re-acking without a note keeps the existing note; an explicit null clears it.
    """
    first, second = _ack_pair(item1_id, item2_id)
    ack = session.get(CollisionAck, (first, second))
    if ack is None:
        ack = CollisionAck(
            item1_id=first, item2_id=second, note=body.note, acked_by=viewer
        )
    else:
        ack.acked_by = viewer
        if "note" in body.model_fields_set:
            ack.note = body.note
    session.add(ack)
    session.commit()
    session.refresh(ack)
    return ack


@router.delete("/collisions/{item1_id}/{item2_id}/ack", status_code=204)
def unack_collision(
    item1_id: uuid.UUID,
    item2_id: uuid.UUID,
    viewer: str = Depends(get_viewer),
    session: Session = Depends(get_session),
) -> None:
    """Remove an acknowledgement; removing an absent one is not an error."""
    ack = session.get(CollisionAck, _ack_pair(item1_id, item2_id))
    if ack is not None:
        session.delete(ack)
        session.commit()
