"""Factory intake persistence models.

Singleton control, intake receipts, start ledger, and audit trail.
Receipt body holds bounded issue text only, never the raw payload.
This module defines tables only and never mutates SwarmTask.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

_BIGINT = BigInteger().with_variant(Integer(), "sqlite")
_JSONB = JSONB().with_variant(JSON(), "sqlite")
# The class a receipt written before ADR agents/038 carries. It lives here
# rather than in factory_controls because the column default needs it and
# this module imports nothing from the package above it.
DEFAULT_TASK_CLASS = "bug-fix"
# How many capacity denials one node may shrug off. A session create the
# control plane refused for capacity never reached a model and did none of the
# attempt's work, so spending an attempt on it retires a node over EmberVM's
# state rather than its own: four refine attempts died that way inside twenty
# minutes while the only brick rebuilt after a spot preemption (#6045). The
# bound is what stops a permanently saturated control plane from retrying for
# ever. Past it the denials count like any other failure and the node retires.
#
# It lives here for the same reason DEFAULT_TASK_CLASS does. The graph bounds
# attempts with it and the start ledger excuses turns with it, and those two
# are on opposite sides of the package boundary, so the number they must agree
# on belongs in the module underneath both.
MAX_CAPACITY_DENIED_ATTEMPTS = 3


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
            "('queued', 'admitted', 'uncertain', 'escalated', 'succeeded', "
            "'failed', 'cancelled')",
            name="factory_receipt_state_check",
        ),
        CheckConstraint(
            "routing_tier IS NULL OR routing_tier IN ('delivery', 'advisory')",
            name="factory_receipt_routing_tier_check",
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
    # Admission pins the quality route separately from the original class.
    # A null value identifies receipts admitted before feedback routing.
    routing_tier: str | None = Field(default=None)
    state: str = Field(default="queued")
    task_id: str | None = Field(default=None, foreign_key="swarm.swarm_task.id")
    policy_json: str | None = Field(default=None)
    allowance_json: str | None = Field(default=None)
    # The escalation document a needs-human refine leaves behind: the question,
    # the recommendation, the options a person may pick from, and once someone
    # picks, the resolution. Null on every receipt that never escalated.
    escalation_json: str | None = Field(default=None)
    # The operator's answer to the escalation the previous task raised, read
    # by the planner prompt on the first round of the task a decision
    # re-admits. Null on every receipt no operator has directed.
    direction_json: str | None = Field(default=None)
    work_item_id: int | None = Field(default=None, foreign_key="swarm.work_item.id")
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
        CheckConstraint(
            "accounting_basis IS NULL "
            "OR accounting_basis IN "
            "('no_model_post', 'capacity_denied', 'no_session_created')",
            name="factory_start_accounting_basis_check",
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
    # Which zero-cost proof settled this start, when one did. The graph books
    # the same attempt at zero on the same evidence, and the two ledgers have
    # to agree or one of them retires a node the other is still holding a
    # retry for (#6045).
    accounting_basis: str | None = Field(default=None)
    session_id: int | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FactoryAudit(SQLModel, table=True):
    """Append only audit trail for factory control and intake actions."""

    __tablename__ = "factory_audit"
    __table_args__ = (
        Index("factory_audit_created_at_idx", "created_at"),
        # Landing reads this trail by task and action on every tick, for the
        # per-task landing state and for the once-only fences.
        Index("factory_audit_task_action_idx", "task_id", "action"),
        # Global reconcilers select the newest rows for a small action set.
        Index("factory_audit_action_id_idx", "action", "id"),
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


class FactoryClassTier(SQLModel, table=True):
    """Durable routing state and the start of its current evaluation epoch."""

    __tablename__ = "factory_class_tier"
    __table_args__ = (
        CheckConstraint(
            "routing_tier IN ('delivery', 'advisory')",
            name="factory_class_tier_value_check",
        ),
        {"schema": "swarm", "extend_existing": True},
    )

    task_class: str = Field(primary_key=True)
    routing_tier: str = Field(default="delivery")
    transitioned_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FactoryReviewVerdict(SQLModel, table=True):
    """One immutable first-pass quality sample per factory task."""

    __tablename__ = "factory_review_verdict"
    __table_args__ = (
        UniqueConstraint("task_id", name="factory_review_verdict_task_id_key"),
        UniqueConstraint(
            "review_run_id", name="factory_review_verdict_review_run_id_key"
        ),
        CheckConstraint(
            "sample_kind IN ('delivery', 'advisory')",
            name="factory_review_verdict_kind_check",
        ),
        CheckConstraint(
            "verdict IN ('approve', 'changes_requested', 'blocked', 'unparseable')",
            name="factory_review_verdict_value_check",
        ),
        Index(
            "factory_review_verdict_class_kind_reviewed_idx",
            "task_class",
            "sample_kind",
            "reviewed_at",
            "id",
        ),
        {"schema": "swarm", "extend_existing": True},
    )

    id: int | None = Field(
        default=None, primary_key=True, sa_type=_BIGINT, nullable=False
    )
    task_id: str = Field(foreign_key="swarm.swarm_task.id")
    review_run_id: int = Field(foreign_key="swarm.swarm_node_run.id")
    recipe_run_id: int | None = Field(
        default=None, foreign_key="swarm.swarm_node_run.id"
    )
    task_class: str
    sample_kind: str
    verdict: str
    summary: str = Field(default="")
    head_sha: str | None = Field(default=None)
    reviewed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkItem(SQLModel, table=True):
    """Durable work item record synced from GitHub."""

    __tablename__ = "work_item"
    __table_args__ = (
        CheckConstraint(
            "state IN ('open','ready','deferred','needs_human','active','done','closed')",
            name="work_item_state_check",
        ),
        CheckConstraint(
            "close_reason IS NULL OR close_reason IN "
            "('completed','not_planned','superseded','stale','github_closed')",
            name="work_item_close_reason_check",
        ),
        CheckConstraint(
            "(state = 'closed') = (close_reason IS NOT NULL)",
            name="work_item_close_reason_consistency_check",
        ),
        CheckConstraint(
            "source_kind IN ('github','mcp','ui','discord','factory')",
            name="work_item_source_kind_check",
        ),
        CheckConstraint(
            "trust IN ('trusted','semi_trusted','untrusted')",
            name="work_item_trust_check",
        ),
        CheckConstraint(
            "authority IN ('github','local')", name="work_item_authority_check"
        ),
        CheckConstraint(
            "(github_repo IS NULL) = (github_issue_number IS NULL)",
            name="work_item_github_both_check",
        ),
        CheckConstraint(
            "github_issue_number IS NULL OR github_issue_number > 0",
            name="work_item_github_number_check",
        ),
        UniqueConstraint(
            "github_repo", "github_issue_number", name="work_item_github_key"
        ),
        Index("work_item_state_created_at_idx", "state", "created_at"),
        Index("work_item_authority_state_idx", "authority", "state"),
        {"schema": "swarm", "extend_existing": True},
    )

    id: int | None = Field(
        default=None, primary_key=True, sa_type=_BIGINT, nullable=False
    )
    title: str
    body: str = Field(default="")
    state: str
    close_reason: str | None = Field(default=None)
    task_class: str | None = Field(default=None)
    labels: list[str] = Field(
        default_factory=list, sa_column=Column(_JSONB, nullable=False)
    )
    source_kind: str
    source_ref: str | None = Field(default=None)
    trust: str
    authority: str = Field(default="local")
    github_repo: str | None = Field(default=None)
    github_issue_number: int | None = Field(default=None)
    github_created_at: datetime | None = Field(default=None)
    github_pointer_comment_id: int | None = Field(default=None, sa_type=_BIGINT)
    pointer_synced_version: int = Field(default=0)
    pointer_synced_at: datetime | None = Field(default=None)
    pointer_failures: int = Field(default=0)
    pointer_next_attempt_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: datetime | None = Field(default=None)


class WorkItemEdge(SQLModel, table=True):
    """Relationship between work items."""

    __tablename__ = "work_item_edge"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('blocks','parent','supersedes')",
            name="work_item_edge_kind_check",
        ),
        CheckConstraint(
            "source IN ('manual','github_body','decision')",
            name="work_item_edge_source_check",
        ),
        CheckConstraint("from_id <> to_id", name="work_item_edge_self_check"),
        UniqueConstraint("from_id", "to_id", "kind", name="work_item_edge_unique"),
        Index("work_item_edge_to_id_idx", "to_id"),
        {"schema": "swarm", "extend_existing": True},
    )

    id: int | None = Field(
        default=None, primary_key=True, sa_type=_BIGINT, nullable=False
    )
    from_id: int = Field(foreign_key="swarm.work_item.id")
    to_id: int = Field(foreign_key="swarm.work_item.id")
    kind: str
    source: str = Field(default="manual", nullable=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkItemEvent(SQLModel, table=True):
    """Version event for a work item."""

    __tablename__ = "work_item_event"
    __table_args__ = (
        UniqueConstraint("work_item_id", "version", name="work_item_event_version_key"),
        {"schema": "swarm", "extend_existing": True},
    )

    id: int | None = Field(
        default=None, primary_key=True, sa_type=_BIGINT, nullable=False
    )
    work_item_id: int = Field(foreign_key="swarm.work_item.id")
    version: int
    op: str
    author_kind: str
    author: str
    change_json: str
    cause_kind: str
    cause_ref: str | None = Field(default=None)
    stated_reason: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FactoryWebhookDelivery(SQLModel, table=True):
    """One authenticated GitHub delivery, committed with its applied effect."""

    __tablename__ = "factory_webhook_delivery"
    __table_args__ = (
        CheckConstraint(
            "outcome IN "
            "('processing','ignored_event','ignored_action','trusted_minted',"
            "'trusted_synced','trusted_unchanged','trusted_closed',"
            "'trusted_stale_ignored','trusted_missing_timestamp_ignored',"
            "'local_untouched','semi_trusted_held','untrusted_ignored')",
            name="factory_webhook_delivery_outcome_check",
        ),
        Index(
            "factory_webhook_delivery_issue_source_idx",
            "repo",
            "issue_number",
            "source_updated_at",
        ),
        {"schema": "swarm", "extend_existing": True},
    )

    delivery_id: str = Field(primary_key=True)
    event: str
    action: str | None = Field(default=None)
    repo: str
    issue_number: int | None = Field(default=None)
    source_updated_at: datetime | None = Field(default=None)
    outcome: str = Field(default="processing")
    work_item_id: int | None = Field(default=None, foreign_key="swarm.work_item.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FactoryGithubIssueState(SQLModel, table=True):
    """Latest applied GitHub source version for one repository issue."""

    __tablename__ = "factory_github_issue_state"
    __table_args__ = (
        CheckConstraint(
            "source_state IN ('open','closed')",
            name="factory_github_issue_state_source_state_check",
        ),
        CheckConstraint(
            "issue_number > 0",
            name="factory_github_issue_state_issue_number_check",
        ),
        {"schema": "swarm", "extend_existing": True},
    )

    repo: str = Field(primary_key=True)
    issue_number: int = Field(primary_key=True)
    source_updated_at: datetime
    source_state: str
    source_ref: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def same_work(row_or_key, other) -> bool:
    """Return whether two receipts or issue keys identify the same work.

    Lives here rather than in factory_intake because swarm_factory_controls
    ships in the pruned session executor binary without the full swarm
    package, and the re-admission guard in factory_controls needs it.
    """

    def identity(value):
        if isinstance(value, tuple) and len(value) == 2:
            return None, value[0], value[1]
        return (
            getattr(value, "work_item_id", None),
            getattr(value, "repo", None),
            getattr(value, "issue_number", None),
        )

    work_item_id, repo, issue_number = identity(row_or_key)
    other_work_item_id, other_repo, other_issue_number = identity(other)
    return (
        work_item_id is not None
        and other_work_item_id is not None
        and work_item_id == other_work_item_id
    ) or (repo == other_repo and issue_number == other_issue_number)
