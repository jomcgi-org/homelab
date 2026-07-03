"""Chat models for pgvector-backed Discord conversation memory."""

import json
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from pydantic import field_validator
from sqlalchemy import JSON, CheckConstraint, Column, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# Postgres uses JSONB (matching the migration); SQLite (test fixtures) falls
# back to the generic JSON type so create_all builds the table cleanly.
_JSONB = JSONB().with_variant(JSON(), "sqlite")


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
    the SQLite test fixtures build it cleanly); content is plain text. A post row
    sets exactly one of the two; a reaction row (target_message_id + reaction)
    sets neither. The CHECK mirrors the DB constraint so the SQLite test fixtures
    enforce it too (create_all does not see migration-only constraints, which is
    how a reaction row's NULL/NULL content once slipped past tests into prod).
    """

    __tablename__ = "discord_outbox"
    __table_args__ = (
        CheckConstraint(
            "content IS NOT NULL OR embed_json IS NOT NULL OR reaction IS NOT NULL",
            name="discord_outbox_content_or_embed",
        ),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    channel_id: str
    content: str | None = Field(default=None)
    embed_json: str | None = Field(default=None)
    level: str = Field(default="info")
    # Reaction verb: when reaction is set, this row is not a post but an add/remove
    # of a unicode reaction on target_message_id in channel_id (reaction_remove
    # picks which). Lets an off-loop producer (the goose runner) drive the ⏳/👀/✅
    # lifecycle on a user's message through the same leader-safe drain.
    target_message_id: str | None = Field(default=None)
    reaction: str | None = Field(default=None)
    reaction_remove: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    posted_at: datetime | None = Field(default=None)
    attempts: int = Field(default=0)
    last_error: str | None = Field(default=None)


# nosemgrep: sqlmodel-datetime-without-factory (running_since is intentionally NULL until a turn goes running)
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
    # Discord parent channel the /agent thread was opened from. The thread itself
    # has no history, so the runner reads this to fetch channel-scoped context
    # (recent messages, rolling summaries) for a conversational reply. Empty for a
    # non-Discord (MCP) session or an artifact thread; empty means no context.
    parent_channel_id: str = Field(default="")
    # Conversational queue state (agent sessions only).
    # running: a turn is currently in flight.
    # pending: newline-joined replies queued while running=True; consumed on drain.
    # pending_message_ids: newline-joined Discord message ids, one per queued reply
    #   (parallel to pending), so the runner can react ⏳/👀/✅ on the exact messages.
    running: bool = Field(default=False)
    pending: str = Field(default="")
    pending_message_ids: str = Field(default="")
    # In-flight turn bookkeeping, for the reaction lifecycle and self-heal.
    # inflight_task: the task text the currently-running turn is executing; kept so
    #   a reclaim (startup sweep or stale-timeout) can rebuild the turn losslessly.
    # inflight_ack_ids: Discord message ids the running turn will resolve (⏳→👀→✅).
    # running_since: when the current turn went running (stale-timeout backstop).
    # runner_instance: boot token of the process that owns the running turn; a
    #   mismatch on startup means the owner died, so the turn is reclaimable.
    inflight_task: str = Field(default="")
    inflight_ack_ids: str = Field(default="")
    running_since: datetime | None = Field(default=None)
    runner_instance: str = Field(default="")
    # Unguessable capability id the artifact is published under (ADR 024 amend).
    # Random per thread, assigned on first publish and reused on re-publish so the
    # live page hot-reloads at a stable but non-discoverable URL (never the
    # enumerable Discord thread id).
    artifact_id: str = Field(default="")
    # Unguessable per-session capability token that keys the guest steering fetch
    # URL (ADR 035 Phase 2 hardening), so a compromised guest cannot address
    # another thread's steering by guessing its Discord thread id. Assigned
    # lazily on first dispatch, same pattern as artifact_id.
    steering_token: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GoosecrackerSteering(SQLModel, table=True):
    """Mid-run steering queue for goosecracker agent threads (ADR 035 Phase 2).

    While a turn is running, thread participants' replies are enqueued here
    (by the bot, ACL-gated) rather than into ``GoosecrackerSession.pending``
    (which is drained between turns by the runner). The running guest recipe
    polls the steering endpoint at stage boundaries and marks rows delivered
    as it consumes them, so a re-poll never redelivers the same message.
    """

    __tablename__ = "goosecracker_steering"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    thread_id: str = Field(default="")
    message_id: str = Field(default="")
    author_id: str = Field(default="")
    tier: str = Field(default="")
    text: str = Field(default="")
    delivered: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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


class AttentionDecision(SQLModel, table=True):
    """Log of attention-gate decisions on ambient-channel messages (ADR 035).

    Every engage is logged; ignores are sampled (ATTENTION_IGNORE_SAMPLE_RATE,
    default 0.1) to bound volume. Used later to tune the classifier and to audit
    what the bot chose to engage with. directive_version records which channel
    directive version the decision ran under (0 until Phase 5 wires directives).
    """

    __tablename__ = "attention_decision"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('engage', 'ignore')",
            name="attention_decision_decision_valid",
        ),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    channel_id: str = Field(default="")
    message_id: str = Field(default="")
    decision: str = Field(default="ignore")
    confidence: float = Field(default=0.0)
    directive_version: int = Field(default=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChannelDirective(SQLModel, table=True):
    """Living per-channel behavioural directive (ADR 035 phase 5).

    Versioned with full history: every propose/confirm/reset inserts a new row
    rather than mutating in place. Exactly one row per channel has active=True
    (enforced by a partial unique index in the migration, not here, since
    SQLite create_all does not support partial indexes and a plain
    unique=True would wrongly reject the history rows in tests). A proposed
    (not yet confirmed) row is inserted active=False and flipped by
    directives.apply_proposal.
    """

    __tablename__ = "channel_directive"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    channel_id: str
    directive: str
    version: int = Field(default=1)
    active: bool = Field(default=False)
    seed_ref: str = Field(default="")
    updated_by_user_id: str = Field(default="")
    motivating_message_id: str = Field(default="")
    proposal_message_id: str = Field(default="")
    previous_version: int = Field(default=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OrchestratorBrief(SQLModel, table=True):
    """ADR 036 orchestrator brief-compiler telemetry (spec section 5).

    One row per orchestrator call: chat and goose verdicts and every fail-open
    degradation. ``thread_id`` links a goose verdict to its
    ``goosecracker_sessions`` run (null for chat/failopen routes with no
    thread). ``brief_json`` holds the compiled Brief (goose) or the chat reply
    guidance, null on failopen. The route CHECK mirrors the migration so the
    SQLite test fixtures enforce it too (create_all does not see migration-only
    constraints). Token columns come from the provider response when present.
    """

    __tablename__ = "orchestrator_brief"
    __table_args__ = (
        CheckConstraint(
            "route IN ('chat', 'goose', 'failopen')",
            name="orchestrator_brief_route_valid",
        ),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    thread_id: str | None = Field(default=None)
    model: str = Field(default="")
    route: str = Field(default="failopen")
    brief_json: dict | None = Field(default=None, sa_column=Column(_JSONB))
    directive_version: int = Field(default=0)
    latency_ms: int = Field(default=0)
    prompt_tokens: int | None = Field(default=None)
    completion_tokens: int | None = Field(default=None)
    cached_tokens: int | None = Field(default=None)
    error: str | None = Field(default=None)


class UserStylePref(SQLModel, table=True):
    """Per-user style preference (ADR 035 phase 5).

    Layered on top of the channel directive at reply time (never merged into
    it). One active pref per user (partial unique index in the migration, same
    reasoning as ChannelDirective above); history rows are kept.
    """

    __tablename__ = "user_style_pref"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    user_id: str
    pref: str
    active: bool = Field(default=True)
    updated_by_user_id: str = Field(default="")
    motivating_message_id: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
