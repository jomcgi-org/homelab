"""Chat models for pgvector-backed Discord conversation memory."""

import json
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from pydantic import field_validator
from sqlalchemy import Column, UniqueConstraint
from sqlmodel import Field, SQLModel


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    discord_message_id: str = Field(unique=True)
    channel_id: str = Field(index=True)
    user_id: str
    username: str
    content: str
    is_bot: bool = Field(default=False)
    thinking: str | None = Field(default=None)
    embedding: list[float] = Field(sa_column=Column(Vector(1024)))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("embedding", mode="before")
    @classmethod
    def _parse_embedding(cls, v: object) -> object:
        """Parse pgvector string representation from raw SQL results."""
        if isinstance(v, str):
            return json.loads(v)
        return v


class Blob(SQLModel, table=True):
    __tablename__ = "blobs"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    sha256: str = Field(primary_key=True, max_length=64)
    # Raw bytes live in SeaweedFS object storage at s3://<bucket>/blobs/<sha256>
    # (written by chat.store). The row holds only metadata.
    content_type: str
    description: str = Field(default="")


class Attachment(SQLModel, table=True):
    __tablename__ = "attachments"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="chat.messages.id")
    blob_sha256: str = Field(foreign_key="chat.blobs.sha256", max_length=64)
    filename: str


class UserChannelSummary(SQLModel, table=True):
    __tablename__ = "user_channel_summaries"
    __table_args__ = (
        UniqueConstraint("channel_id", "user_id"),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    channel_id: str
    user_id: str
    username: str
    summary: str
    last_message_id: int
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MessageLock(SQLModel, table=True):
    __tablename__ = "message_locks"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    discord_message_id: str = Field(primary_key=True)
    channel_id: str
    claimed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed: bool = Field(default=False)


class ChannelSummary(SQLModel, table=True):
    __tablename__ = "channel_summaries"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    channel_id: str = Field(unique=True)
    summary: str
    message_count: int = Field(default=0)
    last_message_id: int = Field(foreign_key="chat.messages.id")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# nosemgrep: sqlmodel-datetime-without-factory (posted_at is intentionally NULL until the drain posts the row)
class DiscordOutbox(SQLModel, table=True):
    """Pending Discord posts. Producers on any replica (or an Argo job) insert a
    row; the leader's bot drain loop posts it and stamps posted_at. Decouples
    'who computed the message' from 'who holds the bot connection' (the leader),
    so posting works regardless of which replica a request lands on.

    embed_json holds a JSON-serialised Discord embed dict (TEXT, not JSONB, so
    the SQLite test fixtures build it cleanly); content is plain text. Exactly
    one of the two is set.
    """

    __tablename__ = "discord_outbox"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    channel_id: str
    content: str | None = Field(default=None)
    embed_json: str | None = Field(default=None)
    level: str = Field(default="info")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    posted_at: datetime | None = Field(default=None)
    attempts: int = Field(default=0)
    last_error: str | None = Field(default=None)


class GoosecrackerSession(SQLModel, table=True):
    """Per-Discord-thread curated transcript for the goosecracker agent (ADR 024).

    One row per thread; transcript accumulates the owner's instructions (never
    ambient chatter or the bot's replies). ``recipe`` distinguishes artifact
    sessions (iterative HTML builder) from agent sessions (conversational coding
    agent). Agent sessions are conversational: ``running`` is True while a turn is
    in flight; replies that arrive during a run are appended to ``pending`` and
    dispatched as the next turn when the current one finishes.
    """

    __tablename__ = "goosecracker_sessions"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    discord_thread: str = Field(primary_key=True)
    transcript: str = Field(default="")
    # recipe/tier/repo mirror the dispatch.submit params so continue_session can
    # re-dispatch without the caller re-supplying them.
    recipe: str = Field(default="artifact")
    tier: str = Field(default="")
    repo: str = Field(default="")
    # Conversational queue state (agent sessions only).
    # running: a turn is currently in flight.
    # pending: newline-joined replies queued while running=True; consumed on drain.
    running: bool = Field(default=False)
    pending: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DiscordFeatureGrant(SQLModel, table=True):
    """Generic per-server Discord bot feature ACL (ADR 029).

    A grant permits a subject to use a feature in a scope within a server.
    Empty-string sentinels are wildcards: guild_id "" matches any server,
    subject_id "" matches every user in that server, scope "" grants the whole
    feature. For the "agent" feature, scope is the repo name. Allow-list only.
    """

    __tablename__ = "discord_feature_grant"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    guild_id: str = Field(default="", primary_key=True)
    subject_id: str = Field(default="", primary_key=True)
    feature: str = Field(primary_key=True)
    scope: str = Field(default="", primary_key=True)
