from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

_JSONB = JSONB().with_variant(JSON(), "sqlite")
_BIGINT = BigInteger().with_variant(Integer(), "sqlite")


class AgentSession(SQLModel, table=True):
    __tablename__ = "agent_sessions"
    __table_args__ = {"schema": "agent_sessions", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    local_session_id: str = Field(unique=True)
    workspace: str
    branch: str
    repo: str | None = None
    # The DBOS workflow that owns this session, or None for hand-started,
    # Discord, and MCP sessions.
    workflow_id: str | None = Field(default=None, index=True)
    node_key: str | None = Field(default=None)
    node_attempt: int | None = Field(default=None)
    # The email of the human who triggered the session, projected from the
    # X-Auth-Email header. NULL for Discord, MCP, and workflow-started sessions.
    triggered_by: str | None = Field(default=None)
    # The Discord thread this session is bound to, or None for a session started
    # from the /agents UI or an MCP tool. Unique so a thread can never fan out to
    # two sessions; Postgres allows many NULLs under a unique constraint, so the
    # unbound sessions are unaffected.
    discord_thread: str | None = Field(default=None, unique=True, index=True)
    model: str | None = Field(default=None)
    reasoning: bool = Field(default=False)
    cli_session_id: str | None = Field(
        default=None
    )  # Claude CLI session_id for resumption
    ember_session_id: str | None = Field(default=None)
    ember_session_token: str | None = Field(default=None)
    # The durable workspace handle (#4306 slice 4): a restored session's
    # ember_session_id is a fresh per-generation id, not a valid restore key
    # for the NEXT generation (session_id == lineage_id only for a gen-0
    # create). This is what a later create passes as restore_lineage to
    # continue the same guest workspace/transcript across an expiry.
    ember_lineage_id: str | None = Field(default=None)
    # #4306 slice 5: the LAST live binding's lineage/CLI transcript, copied
    # here when the active binding is cleared (a confirmed-dead session or an
    # admin destroy), so the next send can still restore the durable
    # workspace even through the double-failure/destroy path that today
    # drops the lineage handle entirely. Cleared whenever a new LIVE binding
    # is established (set_ember_session), so a stale prior never shadows one.
    prior_ember_lineage_id: str | None = Field(default=None)
    prior_cli_session_id: str | None = Field(default=None)
    progress_token: str | None = Field(default=None)
    # The CALLER's system prompt for this session, appended to the guest shim's
    # own sandbox prompt. Only the MCP lane sets it (to the voice instruction);
    # a Discord thread posts the turn result verbatim, so voice markup there is
    # noise rather than signal.
    system_prompt: str | None = Field(default=None)
    # BigInteger, not the default Integer: this is epoch MILLISECONDS from the
    # control plane, which overflows int4. The migration already declares BIGINT,
    # but SQLModel maps a plain int to sqlalchemy Integer and emits an explicit
    # ::INTEGER cast in the UPDATE, so the write failed against a bigint column.
    ember_session_expires_at: int | None = Field(default=None, sa_type=BigInteger)
    status: str = Field(default="running")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_turn_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    voice_summary: str | None = Field(default=None)
    # Qwen-generated display name (titles.py). title_turn_seq is the turn
    # the name was generated from; the leader loop refreshes the name when
    # newer turns land. The router falls back to the first prompt when unset.
    title: str | None = Field(default=None)
    title_turn_seq: int | None = Field(default=None)


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
    prompt_intent: str | None = Field(default=None)
    model: str | None = Field(default=None)
    voice_summary: str | None = Field(default=None)
    result_text: str
    terminal_reason: str | None = Field(default=None)
    stop_reason: str | None = Field(default=None)
    permission_denials: str | None = Field(default=None)
    commit_sha: str | None = Field(default=None, index=True)
    base_sha: str | None = Field(default=None, index=True)
    diff_blob: bytes | None = Field(default=None, sa_type=LargeBinary)
    diff_truncated: bool = Field(default=False)
    diff_base_sha: str | None = Field(default=None)
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
    partial_text: str | None = Field(default=None)
    partial_activities: str | None = Field(default=None)
    model: str | None = Field(default=None)
    claimed_by_replica: str | None = Field(default=None)
    claimed_at: datetime | None = Field(default=None)  # For lease expiry detection
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VoiceUICompanion(SQLModel, table=True):
    __tablename__ = "voice_ui_companions"
    __table_args__ = {"schema": "agent_sessions", "extend_existing": True}

    id: str = Field(primary_key=True)
    session_id: int | None = Field(default=None)
    principal_subject: str
    principal_authority: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
    last_seen_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
    closed_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class VoiceUILedger(SQLModel, table=True):
    __tablename__ = "voice_ui_ledger"
    __table_args__ = (
        CheckConstraint(
            "call IN ('attach', 'show', 'ask', 'dismiss')",
            name="voice_ui_ledger_call_chk",
        ),
        Index("voice_ui_ledger_companion_id_id_idx", "companion_id", "id"),
        {"schema": "agent_sessions", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True, sa_type=_BIGINT)
    companion_id: str
    session_id: int | None = Field(default=None)
    call: str
    payload: dict = Field(
        default_factory=dict, sa_column=Column(_JSONB, nullable=False)
    )
    principal_subject: str
    principal_authority: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
