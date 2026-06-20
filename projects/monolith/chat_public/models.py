"""SQLModel definitions for the chat_public schema (ADR 005).

Mirrors chart/migrations/20260617030000_chat_public.sql. The CHECK constraints
are declared on the models (in addition to the migration) so SQLite-backed unit
tests using ``SQLModel.metadata.create_all()`` enforce them too (per CLAUDE.md
sqlite-fixture rule); the migration owns the production DDL.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, CheckConstraint, Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# Postgres uses JSONB (matching the migration); SQLite-backed unit tests fall
# back to JSON so SQLModel.metadata.create_all() can build the table.
_JSONB = JSONB().with_variant(JSON(), "sqlite")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChatSession(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    """An anonymous public-chat session. The row, not the cookie, is the budget
    authority (ADR 005 layer 1+4)."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'expired', 'purged')",
            name="sessions_status_chk",
        ),
        CheckConstraint("turn_count >= 0", name="sessions_turn_count_nonneg_chk"),
        CheckConstraint("total_tokens >= 0", name="sessions_total_tokens_nonneg_chk"),
        {"schema": "chat_public", "extend_existing": True},
    )

    # Opaque, client-unforgeable id (set server-side, never derived from input).
    id: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime = Field(default_factory=_utcnow)
    turn_count: int = Field(default=0)
    total_tokens: int = Field(default=0)
    # Pseudonymous user/session details: hashes and coarse geo only, never raw
    # IP or PII.
    ip_hash: str | None = Field(default=None)
    turnstile_outcome: str | None = Field(default=None)
    country: str | None = Field(default=None)
    user_agent_hash: str | None = Field(default=None)
    status: str = Field(default="active")
    rolling_summary: str | None = Field(default=None)


class ChatMessage(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    """One transcript message. Server-authoritative: the browser never sends
    history, so this table is the sole record of the conversation."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant', 'system')",
            name="messages_role_chk",
        ),
        CheckConstraint("tokens >= 0", name="messages_tokens_nonneg_chk"),
        {"schema": "chat_public", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="chat_public.sessions.id", index=True)
    role: str
    content: str
    tokens: int = Field(default=0)
    # Grounding for an assistant turn: the public notes it touched, as a
    # [{id, title}, ...] array (empty for user turns). Persisted so a shared
    # snapshot can render the same GROUNDED IN chips the live app shows; it is
    # the node_touched grounding emitted during the turn.
    touched: list = Field(default_factory=list, sa_column=Column(_JSONB))
    created_at: datetime = Field(default_factory=_utcnow)


class ChatResponseCache(
    SQLModel, table=True
):  # nosemgrep: sqlmodel-datetime-without-factory
    """A durable, cross-pod cache of public-chat answers (ADR 005 follow-up).

    A simple key/value row: ``cache_key`` is a hash of
    (normalized_message, prompt_version, notes_watermark). The component parts are
    stored alongside for debuggability; ``touched`` is the node_touched grounding
    list replayed on a hit. Mirrors
    chart/migrations/20260619010000_chat_public_response_cache.sql.
    """

    __tablename__ = "response_cache"
    __table_args__ = (
        CheckConstraint("hit_count >= 0", name="response_cache_hit_count_nonneg_chk"),
        {"schema": "chat_public", "extend_existing": True},
    )

    cache_key: str = Field(primary_key=True)
    normalized_message: str
    prompt_version: str
    notes_watermark: str
    response_text: str
    touched: list = Field(default_factory=list, sa_column=Column(_JSONB))
    created_at: datetime = Field(default_factory=_utcnow)
    hit_count: int = Field(default=0)


class ChatSnapshot(
    SQLModel, table=True
):  # nosemgrep: sqlmodel-datetime-without-factory
    """An opt-in, read-only, immutable share of a chat transcript (ADR 005
    follow-up, "share this chat").

    Minted SERVER-SIDE from the stored, server-authoritative transcript, never
    from client-supplied content, so a forged body cannot put words in the
    model's mouth in a publicly-shareable artifact. The id is an opaque CSPRNG
    token (same posture as a session id) so the share url is unguessable. Once
    created a snapshot is immutable: there is no application UPDATE path.

    ``source_session_id`` is forensics-only and is NOT a cascading FK: a snapshot
    must outlive its session, so purging a session sets it NULL rather than
    deleting the share (ON DELETE SET NULL in the migration). Mirrors
    chart/migrations/20260620000000_chat_public_shared_snapshots.sql.
    """

    __tablename__ = "shared_snapshots"
    __table_args__ = (
        CheckConstraint(
            "message_count >= 0", name="shared_snapshots_message_count_nonneg_chk"
        ),
        {"schema": "chat_public", "extend_existing": True},
    )

    # Opaque, client-unforgeable id (set server-side, never derived from input).
    id: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=_utcnow)
    # The frozen transcript: an array of {role, content} objects, user/assistant
    # rows only (no system rows, no grounding metadata).
    transcript: list = Field(default_factory=list, sa_column=Column(_JSONB))
    message_count: int = Field(default=0)
    # Forensics only; nullable so the snapshot survives session purge.
    source_session_id: str | None = Field(
        default=None, foreign_key="chat_public.sessions.id"
    )
