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


# nosemgrep: sqlmodel-datetime-without-factory (running_since is intentionally NULL until a turn goes running)
class GoosecrackerSession(SQLModel, table=True):
    """Per-Discord-thread curated transcript for the goosecracker agent (ADR 024).

    One row per thread; transcript accumulates the owner's instructions (never
    ambient chatter or the bot's replies). Every session is an agent session
    (conversational coding agent; an artifact is an agent run with no repo):
    ``running`` is True while a turn is in flight; replies that arrive during a
    run are appended to ``pending`` and dispatched as the next turn when the
    current one finishes.
    """

    __tablename__ = "goosecracker_sessions"
    __table_args__ = {"schema": "chat", "extend_existing": True}

    discord_thread: str = Field(primary_key=True)
    transcript: str = Field(default="")
    # recipe/tier/repo mirror the dispatch.submit params so continue_session can
    # re-dispatch without the caller re-supplying them.
    recipe: str = Field(default="agent")
    tier: str = Field(default="")
    repo: str = Field(default="")
    # Provider discriminator (ADR 039 Phase 4). "discord" is the original path;
    # "whatsapp" routes reactions, the checklist, and the final result through
    # chat.whatsapp_outbox instead of the Discord outbox. The PK (discord_thread)
    # holds a sanitized wa-<group_jid> key for a WhatsApp session, so
    # provider_group_jid carries the raw JID the outbox writers target.
    # provider_trigger_message_id / provider_trigger_sender_jid are the triggering
    # WhatsApp message + its sender JID, so the reaction lifecycle can build
    # reactions on it. checklist_outbox_id is the outbox id of the live checklist
    # message the run edits; it is repointed when the ~15-minute edit window
    # closes mid-run and a fresh checklist message is posted.
    provider: str = Field(default="discord")
    provider_group_jid: str = Field(default="")
    provider_trigger_message_id: str = Field(default="")
    provider_trigger_sender_jid: str = Field(default="")
    checklist_outbox_id: int | None = Field(default=None)
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
    # Discord id of the run's single live message (the "🤖 Planning…" reply the
    # bot posts and edits in place with the stage checklist). On completion the
    # runner overwrites this SAME message with the final result via a durable
    # outbox edit, instead of posting a separate second message. Stored on the row
    # so the off-loop runner (possibly another replica) can address it durably;
    # empty means no live message (MCP session, or a race), and the runner falls
    # back to posting the result as a new message. Rewritten per turn.
    progress_message_id: str = Field(default="")
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
    # Readable author name for attribution (ADR 039 Phase 4). WhatsApp carries a
    # sender push name alongside the JID (which lives in author_id); Discord rows
    # leave this "" and attribute by the user id in author_id.
    author_name: str = Field(default="")
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
    reply_message_id: str | None = Field(default=None)
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
    ``goosecracker_sessions`` run. The orchestrator runs before the session
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
