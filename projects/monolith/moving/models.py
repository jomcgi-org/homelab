"""SQLModel definitions for the moving planner schema.

Mirrors chart/migrations/20260816000000_moving_schema.sql.

The constrained vocabularies use TEXT + CHECK instead of PostgreSQL enums.
PostgreSQL enum ALTER TYPE does not roll back cleanly inside a transaction,
and the vocabularies will change as the move progresses.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, SQLModel

Track = Literal["sell", "admin", "ship", "people"]
Owner = Literal["joe", "anna", "both"]
GcalState = Literal["queued", "synced", "held"]
SpanKind = Literal["visitor", "work", "move", "trip"]
RoleStage = Literal["applied", "screen", "onsite", "offer", "closed"]
ViewerName = Literal["joe", "anna"]

_UUID = PG_UUID(as_uuid=False).with_variant(String(36), "sqlite")


def _uuid_column(
    *, primary_key: bool = False, nullable: bool = True, fk: str | None = None
) -> Column:
    """Use native UUID in PostgreSQL and a string in SQLite fixtures."""
    args = [_UUID]
    if fk is not None:
        args.append(ForeignKey(fk, ondelete="SET NULL"))
    return Column(*args, primary_key=primary_key, nullable=nullable)


def _uuid() -> str:
    return str(uuid.uuid4())


class Task(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "tasks"
    __table_args__ = {"schema": "moving", "extend_existing": True}

    id: str = Field(
        default_factory=_uuid,
        sa_column=_uuid_column(primary_key=True, nullable=False),
    )
    track: Track | None = Field(
        default=None,
        sa_column=Column(
            Text,
            CheckConstraint(
                "track IN ('sell', 'admin', 'ship', 'people')",
                name="tasks_track_chk",
            ),
            nullable=True,
        ),
    )
    title: str = Field(sa_column=Column(Text, nullable=False))
    note: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    owner: Owner = Field(
        default="both",
        sa_column=Column(
            Text,
            CheckConstraint("owner IN ('joe', 'anna', 'both')", name="tasks_owner_chk"),
            nullable=False,
            server_default=text("'both'"),
        ),
    )
    due_on: date | None = Field(default=None)
    done_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    value_cad: Decimal | None = Field(
        default=None, sa_column=Column(Numeric, nullable=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Milestone(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "milestones"
    __table_args__ = {"schema": "moving", "extend_existing": True}

    id: str = Field(
        default_factory=_uuid,
        sa_column=_uuid_column(primary_key=True, nullable=False),
    )
    title: str = Field(sa_column=Column(Text, nullable=False))
    occurs_on: date
    owner: Owner = Field(
        default="both",
        sa_column=Column(
            Text,
            CheckConstraint(
                "owner IN ('joe', 'anna', 'both')", name="milestones_owner_chk"
            ),
            nullable=False,
            server_default=text("'both'"),
        ),
    )
    gcal_event_id: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True, unique=True)
    )
    gcal_synced_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    gcal_state: GcalState = Field(
        default="queued",
        sa_column=Column(
            Text,
            CheckConstraint(
                "gcal_state IN ('queued', 'synced', 'held')",
                name="milestones_gcal_state_chk",
            ),
            nullable=False,
            server_default=text("'queued'"),
        ),
    )


class Span(SQLModel, table=True):
    __tablename__ = "spans"
    __table_args__ = {"schema": "moving", "extend_existing": True}

    id: str = Field(
        default_factory=_uuid,
        sa_column=_uuid_column(primary_key=True, nullable=False),
    )
    kind: SpanKind = Field(
        sa_column=Column(
            Text,
            CheckConstraint(
                "kind IN ('visitor', 'work', 'move', 'trip')",
                name="spans_kind_chk",
            ),
            nullable=False,
        ),
    )
    label: str = Field(sa_column=Column(Text, nullable=False))
    starts_on: date
    ends_on: date = Field(
        sa_column=Column(
            Date,
            CheckConstraint("ends_on >= starts_on", name="spans_date_order_chk"),
            nullable=False,
        )
    )
    owner: Owner = Field(
        default="both",
        sa_column=Column(
            Text,
            CheckConstraint("owner IN ('joe', 'anna', 'both')", name="spans_owner_chk"),
            nullable=False,
            server_default=text("'both'"),
        ),
    )


class Role(SQLModel, table=True):
    __tablename__ = "roles"
    __table_args__ = {"schema": "moving", "extend_existing": True}

    id: str = Field(
        default_factory=_uuid,
        sa_column=_uuid_column(primary_key=True, nullable=False),
    )
    company: str = Field(sa_column=Column(Text, nullable=False))
    title: str = Field(sa_column=Column(Text, nullable=False))
    owner: Owner = Field(
        default="both",
        sa_column=Column(
            Text,
            CheckConstraint("owner IN ('joe', 'anna', 'both')", name="roles_owner_chk"),
            nullable=False,
            server_default=text("'both'"),
        ),
    )
    stage: RoleStage | None = Field(
        default=None,
        sa_column=Column(
            Text,
            CheckConstraint(
                "stage IN ('applied', 'screen', 'onsite', 'offer', 'closed')",
                name="roles_stage_chk",
            ),
            nullable=True,
        ),
    )
    next_on: date | None = Field(default=None)
    span_id: str | None = Field(
        default=None,
        sa_column=_uuid_column(nullable=True, fk="moving.spans.id"),
    )


class Viewer(SQLModel, table=True):
    __tablename__ = "viewers"
    __table_args__ = {"schema": "moving", "extend_existing": True}

    email: str = Field(sa_column=Column(Text, primary_key=True, nullable=False))
    name: ViewerName = Field(
        sa_column=Column(
            Text,
            CheckConstraint("name IN ('joe', 'anna')", name="viewers_name_chk"),
            nullable=False,
        )
    )
