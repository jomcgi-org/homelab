from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    Index,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Session, SQLModel, select

from core.db import get_engine

_JSONB = JSONB().with_variant(JSON(), "sqlite")
_BIGINT = BigInteger().with_variant(Integer(), "sqlite")


class SwarmTask(SQLModel, table=True):
    __tablename__ = "swarm_task"
    __table_args__ = {"schema": "swarm", "extend_existing": True}

    id: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    task_text: str
    repo: str | None = None
    base_branch: str | None = None
    conductor_model: str
    budget_usd: float | None = None
    workflow_id: str | None = Field(default=None, index=True)
    session_id: int | None = Field(default=None, index=True)
    settled_at: datetime | None = None


class SwarmPlanVersion(SQLModel, table=True):
    __tablename__ = "swarm_plan_version"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "version", name="swarm_plan_version_task_version_key"
        ),
        {"schema": "swarm", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(foreign_key="swarm.swarm_task.id", index=True)
    version: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    op: str
    author_kind: str
    author: str
    change_json: str
    cause_kind: str
    cause_ref: str | None = None
    stated_reason: str | None = None


class SwarmPlanNode(SQLModel, table=True):
    __tablename__ = "swarm_plan_node"
    __table_args__ = (
        UniqueConstraint("task_id", "node_key", name="swarm_plan_node_task_node_key"),
        {"schema": "swarm", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(foreign_key="swarm.swarm_task.id", index=True)
    node_key: str
    kind: str
    prompt: str
    model: str | None = None
    deps_json: str
    max_cost_usd: float
    side_effects: bool
    max_attempts: int | None = None
    turn_timeout_seconds: int | None = None
    created_in_version: int
    discarded_in_version: int | None = None
    cancelled_in_version: int | None = None
    armed_at: datetime | None = None
    base_artifact_sha: str | None = None


class SwarmConductorCall(SQLModel, table=True):
    __tablename__ = "swarm_conductor_call"
    __table_args__ = {"schema": "swarm", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(foreign_key="swarm.swarm_task.id", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    conductor_model: str
    tool: str
    args_json: str
    outcome: str
    refusal_code: str | None = None
    version_before: int | None = None
    version_after: int | None = None
    latency_ms: int | None = None


class SwarmDecision(SQLModel, table=True):
    __tablename__ = "swarm_decision"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('push_gate', 'review_escalation')",
            name="swarm_decision_kind_check",
        ),
        Index(
            "swarm_decision_open_idx",
            "workflow_id",
            "node_key",
            unique=True,
            postgresql_where=text("decided_at IS NULL"),
            sqlite_where=text("decided_at IS NULL"),
        ),
        Index("swarm_decision_workflow_requested_idx", "workflow_id", "requested_at"),
        {"schema": "swarm", "extend_existing": True},
    )

    id: int | None = Field(
        default=None, primary_key=True, sa_type=_BIGINT, nullable=False
    )
    workflow_id: str
    node_key: str
    kind: str
    options: list[str] = Field(sa_column=Column(_JSONB, nullable=False))
    note: str | None = None
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None
    decision: str | None = None
    decision_note: str | None = None
    actor_subject: str | None = None
    # CHECK-free by design. Values are cloudflare-access, anonymous, or an
    # Authority enum string from MCP.
    actor_authority: str | None = Field(
        default=None,
        description=(
            "cloudflare-access, anonymous, or an Authority enum string from MCP"
        ),
    )


@contextmanager
def _session(session: Session | None) -> Iterator[Session]:
    if session is not None:
        yield session
        return
    with Session(get_engine()) as owned_session:
        yield owned_session


def mint_task_id() -> str:
    return f"t-{uuid4()}"


def create_task(
    task_id: str,
    task_text: str,
    repo: str | None,
    base_branch: str | None,
    conductor_model: str,
    budget_usd: float | None,
    workflow_id: str | None = None,
    session_id: int | None = None,
    *,
    session: Session | None = None,
) -> SwarmTask:
    row = SwarmTask(
        id=task_id,
        task_text=task_text,
        repo=repo,
        base_branch=base_branch,
        conductor_model=conductor_model,
        budget_usd=budget_usd,
        workflow_id=workflow_id,
        session_id=session_id,
    )
    with _session(session) as db:
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def update_task_links(
    task_id: str,
    *,
    workflow_id: str | None = None,
    session_id: int | None = None,
    session: Session | None = None,
) -> SwarmTask:
    with _session(session) as db:
        row = db.get(SwarmTask, task_id)
        if row is None:
            raise ValueError(f"Unknown swarm task {task_id}")
        if workflow_id is not None:
            row.workflow_id = workflow_id
        if session_id is not None:
            row.session_id = session_id
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def append_plan_version(
    task_id: str,
    version: int,
    op: str,
    author_kind: str,
    author: str,
    change_json: str,
    cause_kind: str,
    cause_ref: str | None = None,
    stated_reason: str | None = None,
    *,
    session: Session | None = None,
) -> SwarmPlanVersion:
    row = SwarmPlanVersion(
        task_id=task_id,
        version=version,
        op=op,
        author_kind=author_kind,
        author=author,
        change_json=change_json,
        cause_kind=cause_kind,
        cause_ref=cause_ref,
        stated_reason=stated_reason,
    )
    with _session(session) as db:
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def upsert_plan_node(
    task_id: str,
    node_key: str,
    kind: str,
    prompt: str,
    model: str | None,
    deps_json: str,
    max_cost_usd: float,
    side_effects: bool,
    max_attempts: int | None,
    turn_timeout_seconds: int | None,
    created_in_version: int,
    discarded_in_version: int | None = None,
    cancelled_in_version: int | None = None,
    armed_at: datetime | None = None,
    base_artifact_sha: str | None = None,
    *,
    session: Session | None = None,
) -> SwarmPlanNode:
    with _session(session) as db:
        row = db.exec(
            select(SwarmPlanNode).where(
                SwarmPlanNode.task_id == task_id,
                SwarmPlanNode.node_key == node_key,
            )
        ).first()
        if row is None:
            row = SwarmPlanNode(task_id=task_id, node_key=node_key)
            db.add(row)
        row.kind = kind
        row.prompt = prompt
        row.model = model
        row.deps_json = deps_json
        row.max_cost_usd = max_cost_usd
        row.side_effects = side_effects
        row.max_attempts = max_attempts
        row.turn_timeout_seconds = turn_timeout_seconds
        row.created_in_version = created_in_version
        row.discarded_in_version = discarded_in_version
        row.cancelled_in_version = cancelled_in_version
        row.armed_at = armed_at
        row.base_artifact_sha = base_artifact_sha
        db.commit()
        db.refresh(row)
    return row


def record_conductor_call(
    task_id: str,
    conductor_model: str,
    tool: str,
    args_json: str,
    outcome: str,
    refusal_code: str | None = None,
    version_before: int | None = None,
    version_after: int | None = None,
    latency_ms: int | None = None,
    *,
    session: Session | None = None,
) -> SwarmConductorCall:
    row = SwarmConductorCall(
        task_id=task_id,
        conductor_model=conductor_model,
        tool=tool,
        args_json=args_json,
        outcome=outcome,
        refusal_code=refusal_code,
        version_before=version_before,
        version_after=version_after,
        latency_ms=latency_ms,
    )
    with _session(session) as db:
        db.add(row)
        db.commit()
        db.refresh(row)
    return row
