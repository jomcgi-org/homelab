"""Factory intake persistence models.

Singleton control, intake receipts, start ledger, and audit trail.
Receipt body holds bounded issue text only, never the raw payload.
This module defines tables only and never mutates SwarmTask.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, CheckConstraint, Index, Integer, UniqueConstraint
from sqlmodel import Field, SQLModel

_BIGINT = BigInteger().with_variant(Integer(), "sqlite")
# The class a receipt written before ADR agents/038 carries. It lives here
# rather than in factory_controls because the column default needs it and
# this module imports nothing from the package above it.
DEFAULT_TASK_CLASS = "bug-fix"


class FactoryControl(SQLModel, table=True):
    """Singleton row (id is always factory) holding intake switch state."""

    __tablename__ = "factory_control"
    __table_args__ = (
        CheckConstraint("id = 'factory'", name="factory_control_id_check"),
        CheckConstraint(
            "state IN ('disabled', 'enabled', 'paused', 'stopped')",
            name="factory_control_state_check",
        ),
        CheckConstraint(
            "admitted_count >= 0", name="factory_control_admitted_count_check"
        ),
        CheckConstraint("version >= 0", name="factory_control_version_check"),
        {"schema": "swarm", "extend_existing": True},
    )

    id: str = Field(primary_key=True)
    state: str = Field(default="disabled")
    policy_json: str = Field(default="{}")
    admitted_count: int = Field(default=0)
    version: int = Field(default=0)
    actor: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    stopped_at: datetime | None = Field(default=None)


class FactoryReceipt(SQLModel, table=True):
    """One intake attempt for a repo issue generation, with optional task link."""

    __tablename__ = "factory_receipt"
    __table_args__ = (
        # The class is part of receipt identity: an issue refined to
        # agent-ready in one generation is received again as a delivery in
        # that same generation, so an operator never has to bump generation
        # to let the lane act on work its own refine pass made ready.
        UniqueConstraint(
            "repo",
            "issue_number",
            "generation",
            "task_class",
            name="factory_receipt_repo_issue_generation_class_key",
        ),
        UniqueConstraint("task_id", name="factory_receipt_task_id_key"),
        CheckConstraint("issue_number > 0", name="factory_receipt_issue_number_check"),
        CheckConstraint("generation >= 0", name="factory_receipt_generation_check"),
        CheckConstraint(
            "state IN "
            "('queued', 'admitted', 'uncertain', 'succeeded', 'failed', 'cancelled')",
            name="factory_receipt_state_check",
        ),
        Index("factory_receipt_state_created_at_idx", "state", "created_at"),
        {"schema": "swarm", "extend_existing": True},
    )

    id: int | None = Field(
        default=None, primary_key=True, sa_type=_BIGINT, nullable=False
    )
    repo: str
    issue_number: int
    generation: int = Field(default=0)
    title: str
    body: str
    url: str
    actor: str
    task_class: str = Field(default=DEFAULT_TASK_CLASS)
    state: str = Field(default="queued")
    task_id: str | None = Field(default=None, foreign_key="swarm.swarm_task.id")
    policy_json: str | None = Field(default=None)
    allowance_json: str | None = Field(default=None)
    task_paused: bool = Field(default=False)
    cancellation_requested: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FactoryStart(SQLModel, table=True):
    """Start reservation ledger keyed by task and idempotency key."""

    __tablename__ = "factory_start"
    __table_args__ = (
        UniqueConstraint("task_id", "start_key", name="factory_start_task_start_key"),
        CheckConstraint(
            "status IN ('reserved', 'succeeded', 'failed', 'uncertain', 'cancelled')",
            name="factory_start_status_check",
        ),
        CheckConstraint("max_cost_usd > 0", name="factory_start_max_cost_check"),
        CheckConstraint(
            "cost_usd IS NULL OR cost_usd >= 0",
            name="factory_start_cost_check",
        ),
        Index("factory_start_task_status_idx", "task_id", "status"),
        {"schema": "swarm", "extend_existing": True},
    )

    id: int | None = Field(
        default=None, primary_key=True, sa_type=_BIGINT, nullable=False
    )
    task_id: str = Field(foreign_key="swarm.swarm_task.id")
    start_key: str
    actor: str
    model: str
    max_cost_usd: float
    status: str = Field(default="reserved")
    cost_usd: float | None = Field(default=None)
    session_id: int | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FactoryAudit(SQLModel, table=True):
    """Append only audit trail for factory control and intake actions."""

    __tablename__ = "factory_audit"
    __table_args__ = (
        Index("factory_audit_created_at_idx", "created_at"),
        {"schema": "swarm", "extend_existing": True},
    )

    id: int | None = Field(
        default=None, primary_key=True, sa_type=_BIGINT, nullable=False
    )
    actor: str
    action: str
    task_id: str | None = Field(default=None, foreign_key="swarm.swarm_task.id")
    detail_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
