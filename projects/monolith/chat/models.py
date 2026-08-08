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

    kind tags a row that needs post-processing after it posts: a plain post
    leaves kind="", while a directive_proposal row (enqueued by the observer
    job) is a normal content post whose payload_json carries the channel /
    directive_change / evidence the drain's post-hook needs to wire the
    propose-then-confirm flow against the message id it just posted.
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
    # Post-hook discriminator: "" for a plain post, "directive_proposal" for a
    # row the leader drain follows up on (payload_json below carries the args).
    kind: str = Field(default="")
    payload_json: str | None = Field(default=None)


# nosemgrep: sqlmodel-datetime-without-factory (posted_at/sent_message_id are intentionally NULL until the gateway sends the row)
class WhatsappOutbox(SQLModel, table=True):
    """Pending WhatsApp sends for the household gateway (ADR 039, spec section 3).

    The single send path for all monolith-originated WhatsApp traffic: any
    replica enqueues a row and the single-replica Go gateway drains it oldest-
    first per group, sends via whatsmeow, and stamps posted_at + sent_message_id.
    The gateway is the sender (unlike the Discord outbox, there is no Python drain
    here).

    kind is the verb: message (text, optionally quoting quoted_message_id), edit
    (edit_of -> the original send's sent_message_id, with new content), reaction
    (on target_message_id; whatsmeow's reaction build needs target_sender_jid, the
    JID of that message's sender, which the row must carry). reaction_remove sends
    an empty reaction to clear it. media (an image: media_bytes + media_mime, with
    content as the optional caption) -- the gateway uploads the bytes to WhatsApp
    and sends an ImageMessage, so a chart/image reaches the group inline (ADR 039,
    amended).

    The combined CHECK mirrors the DB constraint so the SQLite test fixtures
    enforce the per-kind shape too (create_all does not see migration-only
    constraints, which is how a malformed outbox row once slipped past tests into
    prod on the Discord side).
    """

    __tablename__ = "whatsapp_outbox"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'message' AND content IS NOT NULL)"
            " OR (kind = 'edit' AND content IS NOT NULL AND edit_of IS NOT NULL)"
            " OR (kind = 'reaction' AND target_message_id IS NOT NULL"
            " AND target_sender_jid IS NOT NULL AND reaction IS NOT NULL)"
            " OR (kind = 'media' AND media_bytes IS NOT NULL"
            " AND media_mime IS NOT NULL)",
            name="whatsapp_outbox_kind_valid",
        ),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    group_jid: str
    kind: str
    content: str | None = Field(default=None)
    quoted_message_id: str | None = Field(default=None)
    edit_of: int | None = Field(default=None)
    target_message_id: str | None = Field(default=None)
    target_sender_jid: str | None = Field(default=None)
    reaction: str | None = Field(default=None)
    reaction_remove: bool = Field(default=False)
    # media kind: the raw image bytes + its mime type (content is the caption).
    # bytes -> LargeBinary (BYTEA on Postgres, BLOB on the SQLite test fixtures).
    media_bytes: bytes | None = Field(default=None)
    media_mime: str | None = Field(default=None)
    sent_message_id: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    posted_at: datetime | None = Field(default=None)
    attempts: int = Field(default=0)
    last_error: str | None = Field(default=None)


class WhatsappGroup(SQLModel, table=True):
    """Allow-list registry for the WhatsApp household gateway (ADR 039, spec 6).

    Only groups with an enabled row here produce inbound traffic: the gateway
    filters on its startup config and the monolith inbound endpoint re-checks
    this table (defense in depth). ``tier`` maps the group to a capability/tool
    subset (ADR 034); ``household`` grants knowledge, calendar, and reminders but
    not repo/cluster/artifact tools. ``ambient`` toggles the attention gate's
    ambient classify. ``directive_seed`` seeds the group directive (an empty seed
    falls back to a built-in household default in the inbound handler).
    ``digest_config`` holds the morning-digest cadence and quiet hours (Phase 5)
    as a JSON dict. ``enabled`` is a kill switch that stops all traffic without
    dropping config or unpairing.
    """

    __tablename__ = "whatsapp_group"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    group_jid: str = Field(primary_key=True)
    display_name: str | None = Field(default=None)
    tier: str = Field(default="household")
    ambient: bool = Field(default=True)
    directive_seed: str | None = Field(default=None)
    digest_config: dict | None = Field(default=None, sa_column=Column(_JSONB))
    enabled: bool = Field(default=True)
    # Timestamp of the last morning digest sent to this group (ADR 039 spec 5d).
    # The digest job dedupes on it (at most one digest per local day) while
    # honouring quiet hours; NULL until the first digest is sent.
    last_digest_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WhatsappPendingAction(SQLModel, table=True):
    """Transient per-group conversational state for the household capabilities
    (ADR 039 spec section 5). One pending action per group (``group_jid`` PK).

    ``kind`` is the awaited resolution: ``record`` is a knowledge capture awaiting
    an affirmative confirmation (confirm-then-capture, the KG consent boundary),
    ``calendar``/``reminder`` are intents awaiting one clarifying answer
    (clarify-once). ``summary`` holds the record confirmation text; ``payload``
    carries the original intent text so a clarifying follow-up can be combined with
    it. The next engaged message resolves or abandons the row, so it is
    short-lived. The CHECK mirrors the DB constraint so the SQLite test fixtures
    reject an invalid kind too (create_all does not see migration-only CHECKs).
    """

    __tablename__ = "whatsapp_pending_action"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('record', 'calendar', 'reminder')",
            name="whatsapp_pending_action_kind_valid",
        ),
        {"schema": "chat", "extend_existing": True},
    )

    group_jid: str = Field(primary_key=True)
    kind: str
    summary: str | None = Field(default=None)
    payload: dict | None = Field(default=None, sa_column=Column(_JSONB))
    created_by: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# nosemgrep: sqlmodel-datetime-without-factory (delivered_at is intentionally NULL until the digest delivers the reminder)
class WhatsappReminder(SQLModel, table=True):
    """An ad-hoc reminder created in the household group (ADR 039 spec 5d).

    Created conversationally ("remind us to X on Y"); the morning digest renders
    open (undelivered, due) reminders and stamps ``delivered_at`` as it includes
    them, so a reminder surfaces once. ``due_at`` is stored in UTC; ``created_by``
    records the participant who set it.
    """

    __tablename__ = "whatsapp_reminder"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    group_jid: str
    text: str
    due_at: datetime
    created_by: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    delivered_at: datetime | None = Field(default=None)


# nosemgrep: sqlmodel-datetime-without-factory (confirmed_at is intentionally NULL until the draft is confirmed by hand)
class WhatsappCalendarDraft(SQLModel, table=True):
    """A proposed calendar event awaiting manual confirmation (ADR 039 spec 5b
    fallback).

    When the cluster-side calendar credential is absent at runtime, a scheduling
    intent is drafted here instead of created live, and the morning digest
    surfaces open drafts (``confirmed_at`` IS NULL) so the group can add them by
    hand. ``start_at``/``end_at`` are stored in UTC; ``attendees`` is a
    human-readable comma-joined list (v1 does not resolve WhatsApp contacts to
    calendar invitees).
    """

    __tablename__ = "whatsapp_calendar_draft"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    group_jid: str
    title: str
    start_at: datetime
    end_at: datetime | None = Field(default=None)
    attendees: str | None = Field(default=None)
    created_by: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    confirmed_at: datetime | None = Field(default=None)


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
    reply_message_id: str | None = Field(default=None)
    # Why an engage produced no in-channel reply (null when one was sent). One
    # of 'agent_thread', 'no_reply', 'send_gate', 'empty_reply'; see the
    # 20260711200000 migration. Lets /improve-ambient tell the silent paths
    # apart instead of guessing from a null reply_message_id.
    withheld_reason: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReactionEvent(SQLModel, table=True):
    """Human reactions on Bosun's own messages.

    Ground-truth signal for the /improve-ambient loop: a reaction on a reply is
    a cheaper, clearer quality signal than inferring from follow-up text. Only
    reactions on bot-authored messages are persisted (target_is_bot always True
    today); the bot's own seed reactions are never logged. Reactions on any
    bot-authored message are stored (the skill filters to ambient episodes later
    by joining). action add/remove lets a removal cancel an earlier signal.
    """

    __tablename__ = "reaction_event"
    __table_args__ = (
        CheckConstraint(
            "action IN ('add', 'remove')",
            name="reaction_event_action_valid",
        ),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    channel_id: str = Field(default="")
    message_id: str = Field(default="")
    target_is_bot: bool = Field(default=True)
    emoji: str = Field(default="")
    reactor_id: str = Field(default="")
    action: str = Field(default="add")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentReplyRepair(SQLModel, table=True):
    """Log of chat-agent replies that leaked tool-call scaffolding.

    One row per reply where the small model dumped ``<tool_call>``/``<arg_*>``
    scaffolding into its answer and the shield (chat.reply_sanitize) had to
    scrub and, when needed, run the bounded model-repair loop. Kept so the copy
    can be evaluated later and the reply/plan prompts iterated against real
    failures. outcome records how it resolved; raw/scrubbed/final are the text
    at each stage (raw is what the model emitted, final is what shipped).
    """

    __tablename__ = "agent_reply_repair"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('clean_after_repair', 'still_dirty')",
            name="agent_reply_repair_outcome_valid",
        ),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    channel_id: str = Field(default="")
    author_id: str = Field(default="")
    route: str = Field(default="chat")
    markers: str = Field(default="")
    raw_text: str = Field(default="")
    scrubbed_text: str = Field(default="")
    final_text: str = Field(default="")
    repair_attempts: int = Field(default=0)
    outcome: str = Field(default="clean_after_repair")
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
    # Provenance for the manual-precedence rule (PR 3): seed | observer |
    # autopilot | manual. A source='manual' active row blocks the directive
    # autopilot for a cooldown, so an out-of-band human tune always wins.
    source: str = Field(default="seed")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OrchestratorBrief(SQLModel, table=True):
    """ADR 036 orchestrator brief-compiler telemetry (spec section 5).

    One row per orchestrator call: chat and goose verdicts and every fail-open
    degradation. ``thread_id`` links a goose (or fell-back) verdict to its
    agent session. The orchestrator runs before the session
    thread exists, so the row is written with a null ``thread_id`` and
    ``orchestrator.link_thread`` backfills it once ``start_agent_flow`` creates
    the thread; it stays null for the chat route and for fail-opens that never
    open a thread (ungranted/disabled). ``brief_json`` holds the compiled Brief
    (goose) or the chat reply guidance, null on failopen. The route CHECK mirrors the migration so the
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


# nosemgrep: sqlmodel-datetime-without-factory (delivered_at is intentionally NULL until the drain delivers the row)
class Reminder(SQLModel, table=True):
    """A user-scheduled reminder (ambient-assistant parity, Phase 2).

    A chat tool inserts a row with due_at in the future; the scheduler drain
    job queries pending rows whose due_at has passed (status, due_at index),
    posts them into chat.discord_outbox, then stamps delivered_at and flips
    status to 'delivered'. A user can cancel a still-pending reminder before
    it fires (status='cancelled'). The status CHECK mirrors the migration so
    the SQLite test fixtures enforce it too (create_all does not see
    migration-only constraints).
    """

    __tablename__ = "reminder"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivered', 'cancelled')",
            name="reminder_status_valid",
        ),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    channel_id: str = Field(index=True)
    author_id: str
    content: str
    due_at: datetime
    status: str = Field(default="pending")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    delivered_at: datetime | None = Field(default=None)


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
    # Provenance for the manual-precedence rule (PR 3): seed | autopilot |
    # manual. A user setting their own style pref is 'manual' (blocks the
    # autopilot); the autopilot writes 'autopilot'.
    source: str = Field(default="seed")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# nosemgrep: sqlmodel-datetime-without-factory (last_signal_at is intentionally NULL until the user's first signal)
class UserTrust(SQLModel, table=True):
    """Per-(guild, user) decaying trust score (ADR chat/003).

    The safeguards ledger: heuristic signals, the LLM intent scorer, and (once
    live) the random forest decrement ``score``; time restores it at
    SAFEGUARDS_RECOVERY_PER_DAY. ``score_updated_at`` is the decay anchor, so
    the effective score is computed lazily on read rather than by a sweeper.
    Below SAFEGUARDS_LOCKOUT_THRESHOLD the bot stops engaging with the user
    (soft lockout, see chat.safeguards). The owner is never ledgered.
    """

    __tablename__ = "user_trust"
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="user_trust_guild_user_unique"),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    guild_id: str = Field(default="")
    user_id: str = Field(default="")
    score: float = Field(default=100.0)
    score_updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    signal_count: int = Field(default=0)
    lockout_count: int = Field(default=0)
    last_signal_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ModerationEvent(SQLModel, table=True):
    """One moderation-relevant observation (ADR chat/003).

    Doubles as the audit log AND the training set for the safeguards forest:
    ``features_json`` snapshots the deterministic feature vector (in
    chat.safeguards.FEATURE_NAMES order) at observation time, ``label`` is the
    training target (1 for signal/llm_intent rows, 0 for sampled clean rows,
    NULL for rows that are not samples: enforcement/lockout/pardon markers).
    A pardon flips the user's recent labels to 0, so a wrong call becomes a
    corrective example instead of a poisoned one. ``rf_score`` is the shadow
    forest's probability stamped at observation time, for live-vs-shadow
    review before any model is promoted. The kind CHECK mirrors the migration
    so the SQLite test fixtures enforce it too (create_all does not see
    migration-only constraints).
    """

    __tablename__ = "moderation_event"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('signal', 'llm_intent', 'clean_sample', 'enforcement', "
            "'lockout', 'pardon')",
            name="moderation_event_kind_valid",
        ),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    guild_id: str = Field(default="")
    channel_id: str = Field(default="")
    message_id: str = Field(default="")
    user_id: str = Field(default="")
    kind: str = Field(default="signal")
    signal: str = Field(default="")
    detail: str = Field(default="")
    delta: float = Field(default=0.0)
    score_after: float = Field(default=0.0)
    features_json: str = Field(default="[]")
    label: int | None = Field(default=None)
    rf_score: float | None = Field(default=None)
    rf_model_version: int = Field(default=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TrustModel(SQLModel, table=True):
    """A trained safeguards random forest (ADR chat/003).

    ``model_json`` is the JSON tree ensemble exported by the trainer
    (chat.safeguards_forest.train_forest, run inside the Firecracker sandbox);
    inference walks it in pure Python so no ML dependency enters the monolith.
    Every fresh train lands as status='shadow' (scored, never enforced);
    promoting a row to 'live' is a deliberate manual step via SQL or the MCP
    surface, and supersession retires old shadow rows. ``feature_names_json``
    pins the feature order the model was trained against; the loader refuses a
    model whose features do not match the running code (schema drift guard).
    The status CHECK mirrors the migration so the SQLite test fixtures enforce
    it too.
    """

    __tablename__ = "trust_model"
    __table_args__ = (
        CheckConstraint(
            "status IN ('shadow', 'live', 'retired')",
            name="trust_model_status_valid",
        ),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    version: int = Field(unique=True)
    status: str = Field(default="shadow")
    model_json: str = Field(default="{}")
    feature_names_json: str = Field(default="[]")
    n_samples: int = Field(default=0)
    n_positive: int = Field(default=0)
    metrics_json: str = Field(default="{}")
    trained_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# nosemgrep: sqlmodel-datetime-without-factory (applied_at/validate_after get a DB now() default; the model factory mirrors it)
class DirectiveAutopilot(SQLModel, table=True):
    """Autopilot decision + self-validation log (ADR chat/007, PR 3).

    One row per autonomous action the directive autopilot takes. The APPLY phase
    writes a 'pending_validation' row capturing the pre-apply baseline score, the
    prior version AND its text (so a revert can reinstate without re-deriving),
    and the supporting episode ids. The VALIDATE phase reads pending rows whose
    validate_after has passed, recomputes the post-apply score, and flips status
    to kept / reverted / superseded_manual. Confident-but-ungated findings land
    as 'proposed' (channel, routed to the human confirm flow), 'suggested' (user,
    no proposal flow exists), or 'shadow' (kill-switch mode: what the autopilot
    WOULD have done, mutating nothing).

    scope_kind and status CHECKs mirror the migration so the SQLite create_all
    test fixtures enforce them too (create_all does not see migration-only
    constraints). baseline_json and evidence_json are TEXT (JSON-serialised) so
    the SQLite fixtures build them cleanly, matching the migration.
    """

    __tablename__ = "directive_autopilot"
    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('channel', 'user')",
            name="directive_autopilot_scope_valid",
        ),
        CheckConstraint(
            "status IN ('pending_validation', 'kept', 'reverted', "
            "'superseded_manual', 'proposed', 'suggested', 'shadow')",
            name="directive_autopilot_status_valid",
        ),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    scope_kind: str = Field(default="channel")
    scope_id: str = Field(default="")
    target_version: int = Field(default=0)
    prior_version: int | None = Field(default=None)
    prior_text: str | None = Field(default=None)
    baseline_json: str = Field(default="{}")
    rationale: str = Field(default="")
    evidence_json: str = Field(default="[]")
    status: str = Field(default="pending_validation")
    applied_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    validate_after: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
