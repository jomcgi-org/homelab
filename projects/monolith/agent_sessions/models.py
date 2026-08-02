from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, UniqueConstraint
from sqlmodel import Field, SQLModel


class AgentSession(SQLModel, table=True):
    __tablename__ = "agent_sessions"
    __table_args__ = {"schema": "agent_sessions", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    local_session_id: str = Field(unique=True)
    workspace: str
    branch: str
    model: str | None = Field(default=None)
    cli_session_id: str | None = Field(
        default=None
    )  # Claude CLI session_id for resumption
    ember_session_id: str | None = Field(default=None)
    ember_session_token: str | None = Field(default=None)
    # BigInteger, not the default Integer: this is epoch MILLISECONDS from the
    # control plane, which overflows int4. The migration already declares BIGINT,
    # but SQLModel maps a plain int to sqlalchemy Integer and emits an explicit
    # ::INTEGER cast in the UPDATE, so the write failed against a bigint column.
    ember_session_expires_at: int | None = Field(default=None, sa_type=BigInteger)
    status: str = Field(default="running")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_turn_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    voice_summary: str | None = Field(default=None)


class AgentTurn(SQLModel, table=True):
    __tablename__ = "agent_turns"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="agent_turns_session_seq_key"),
        {"schema": "agent_sessions", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="agent_sessions.agent_sessions.id", index=True)
    seq: int = Field(index=True)
    prompt: str
    model: str | None = Field(default=None)
    voice_summary: str | None = Field(default=None)
    result_text: str
    terminal_reason: str | None = Field(default=None)
    stop_reason: str | None = Field(default=None)
    permission_denials: str | None = Field(default=None)
    commit_sha: str | None = Field(default=None, index=True)
    usage_json: str | None = Field(default=None)
    cost_usd: float | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PendingMessage(SQLModel, table=True):
    __tablename__ = "pending_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="pending_messages_session_seq_key"),
        {"schema": "agent_sessions", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="agent_sessions.agent_sessions.id", index=True)
    seq: int
    message_text: str
    model: str | None = Field(default=None)
    claimed_by_replica: str | None = Field(default=None)
    claimed_at: datetime | None = Field(default=None)  # For lease expiry detection
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
