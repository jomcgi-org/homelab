"""Durable operator evidence, independent of deletable routine-job rows."""

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel


class RoutineReconciliation(SQLModel, table=True):
    __tablename__ = "routine_reconciliations"
    __table_args__ = (
        UniqueConstraint("session_id", "unknown_turn_seq"),
        CheckConstraint("unknown_turn_seq > 0"),
        CheckConstraint("disposition IN ('rearm', 'retain_applied')"),
        {"schema": "claude_agent"},
    )

    reconciliation_key: str = Field(primary_key=True)
    request_sha256: str
    actor: str
    job_name: str
    session_id: int
    unknown_turn_seq: int
    disposition: str
    evidence_json: str
    result_json: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
