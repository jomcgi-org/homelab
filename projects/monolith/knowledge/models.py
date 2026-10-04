"""SQLModel definitions for the knowledge schema."""

import json
from datetime import date, datetime, timezone
from typing import Any, Literal, NewType

from pgvector.sqlalchemy import Vector
from pydantic import field_validator
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

NoteId = NewType("NoteId", str)

# Mirror of the CHECK constraint in
# chart/migrations/20260408000000_knowledge_schema.sql - keep in sync.
EdgeType = Literal[
    "refines",
    "generalizes",
    "related",
    "contradicts",
    "derives_from",
    "supersedes",
]
LinkKind = Literal["link", "edge"]

# Mirror of the CHECK constraint in
# chart/migrations/20260424000000_knowledge_gaps.sql - keep in sync.
GapClass = Literal["external", "internal", "hybrid", "parked"]
# Mirror of the CHECK constraint in
# chart/migrations/20260424000000_knowledge_gaps.sql - keep in sync.
GapState = Literal[
    "discovered",
    "classified",
    "in_review",
    "researching",
    "researched",
    "verified",
    "consolidated",
    "committed",
    "parked",
    "rejected",
]

# Mirror of the CHECK constraint in
# chart/migrations/20260508000000_knowledge_notes_visibility.sql - keep in sync.
Visibility = Literal["public", "private"]

# Mirror of knowledge.notes.notes_scope_shape_chk. Keep every producer and
# parser on the same scope grammar so invalid input fails before persistence.
SCOPE_PATTERN = r"^(personal|org|repo|environment|session):.+$"

# Postgres uses native TEXT[] for tags/aliases; SQLite falls back to JSON
# so the in-memory test fixture can create the tables.
_STRING_ARRAY = PG_ARRAY(String).with_variant(JSON(), "sqlite")
# Postgres uses JSONB (matching the migration + GIN index); SQLite falls
# back to JSON.
_JSONB = JSONB().with_variant(JSON(), "sqlite")

AuditCause = Literal[
    "lens_overgeneralised",
    "missing_supersession",
    "stale_after_code_change",
    "duplicate_not_merged",
    "ranking_surfaced_stale",
    "chunking_split_evidence",
    "source_wrong",
    "other",
]
AUDIT_CAUSES = (
    "lens_overgeneralised",
    "missing_supersession",
    "stale_after_code_change",
    "duplicate_not_merged",
    "ranking_surfaced_stale",
    "chunking_split_evidence",
    "source_wrong",
    "other",
)


class AuditRun(SQLModel, table=True):
    __tablename__ = "audit_runs"
    __table_args__ = (
        CheckConstraint(
            "stream IN ('scheduled', 'expansion')", name="audit_runs_stream_chk"
        ),
        UniqueConstraint("job_name", "started_at", name="audit_runs_invocation_key"),
        {"schema": "knowledge", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    job_name: str
    stream: str = "scheduled"
    root_run_id: int | None = Field(default=None, foreign_key="knowledge.audit_runs.id")
    depth: int = 0
    prompt_version: str
    status: str = "prepared"
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    sampled_uniform: int = 0
    sampled_weighted: int = 0
    sampled_expansion: int = 0
    metrics: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(_JSONB, nullable=False)
    )
    cost_usd: float | None = None


# The JSON invocation key distinguishes recurring runs without another payload
# contract or an extra schema column. NULL is allowed on manually seeded rows.
Index(
    "audit_runs_replay_key",
    AuditRun.__table__.c.job_name,
    AuditRun.__table__.c.metrics["invocation_key"].as_string(),
    unique=True,
)


class AuditFinding(SQLModel, table=True):
    __tablename__ = "audit_findings"
    __table_args__ = (
        UniqueConstraint("run_id", "note_id", name="audit_findings_sample_key"),
        Index("audit_findings_cooldown_idx", "note_id", "created_at"),
        CheckConstraint(
            "stream IN ('uniform', 'weighted', 'expansion')",
            name="audit_findings_stream_chk",
        ),
        CheckConstraint(
            "correctness IN ('holds', 'confirmed', 'narrowed', 'superseded', 'invalidated', 'unknown')",
            name="audit_findings_correctness_chk",
        ),
        CheckConstraint(
            "clarity IN ('clear', 'unclear', 'unknown')",
            name="audit_findings_clarity_chk",
        ),
        CheckConstraint(
            "clarity_score IS NULL OR (clarity_score >= 0 AND clarity_score <= 1)",
            name="audit_findings_score_chk",
        ),
        CheckConstraint(
            "placement IN ('ok', 'misplaced', 'unknown')",
            name="audit_findings_placement_chk",
        ),
        CheckConstraint(
            "cause IS NULL OR cause IN ("
            + ", ".join(repr(cause) for cause in AUDIT_CAUSES)
            + ")",
            name="audit_findings_cause_chk",
        ),
        {"schema": "knowledge", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="knowledge.audit_runs.id")
    note_id: str
    stream: str
    depth: int = 0
    parent_finding_id: int | None = Field(
        default=None, foreign_key="knowledge.audit_findings.id"
    )
    correctness: str = "unknown"
    clarity: str = "unknown"
    clarity_score: float | None = None
    placement: str = "unknown"
    cause: str | None = None
    rationale: str = ""
    evidence: list[str] = Field(
        default_factory=list, sa_column=Column(_JSONB, nullable=False)
    )
    source_raw_id: str | None = None
    source: str | None = None
    extraction_version: str | None = None
    dispute_id: int | None = Field(default=None, foreign_key="knowledge.disputes.id")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AuditProcessIssue(SQLModel, table=True):
    __tablename__ = "audit_process_issues"
    __table_args__ = (
        CheckConstraint(
            "state IN ('write_started', 'filed', 'unresolved')",
            name="audit_process_issues_state_chk",
        ),
        {"schema": "knowledge", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    cause_key: str = Field(unique=True)
    state: str = "write_started"
    marker: str
    issue_number: int | None = None
    defect_count: int = 0
    run_count: int = 0
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class NoteRetrieval(SQLModel, table=True):
    __tablename__ = "note_retrievals"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    note_id: str = Field(primary_key=True)
    day: date = Field(sa_column=Column(Date, primary_key=True))
    count: int = 0


class RecallEmbedding(SQLModel, table=True):
    __tablename__ = "recall_embeddings"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    key: str = Field(primary_key=True)
    embedding: list[float] = Field(sa_column=Column(Vector(1024), nullable=False))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class KnowledgeFeedState(SQLModel, table=True):
    """Durable initialization state for database-backed knowledge feeds."""

    __tablename__ = "feed_state"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    feed_name: str = Field(primary_key=True)
    first_enabled_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        )
    )


class Note(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "notes"
    __table_args__ = (
        # Mirrors the CHECK constraint in
        # chart/migrations/20260508000000_knowledge_notes_visibility.sql.
        # Declared on the model (in addition to the migration) so SQLite-backed
        # unit tests using SQLModel.metadata.create_all() also enforce it.
        CheckConstraint(
            "visibility IS NULL OR visibility IN ('public', 'private')",
            name="notes_visibility_chk",
        ),
        CheckConstraint(
            "verification_state IN "
            "('legacy', 'unverified', 'verified', 'disputed', 'invalidated')",
            name="notes_verification_state_chk",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="notes_confidence_chk",
        ),
        CheckConstraint(
            "review_after IS NULL OR (COALESCE(last_reviewed_at, observed_at) IS NOT NULL "
            "AND review_after <= COALESCE(last_reviewed_at, observed_at) + INTERVAL '2160 hours')",
            name="notes_review_deadline_chk",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "review_after IS NULL OR (COALESCE(last_reviewed_at, observed_at) IS NOT NULL "
            "AND julianday(review_after) <= julianday(COALESCE(last_reviewed_at, observed_at)) + 90)",
            name="notes_review_deadline_chk",
        ).ddl_if(dialect="sqlite"),
        {"schema": "knowledge", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    note_id: NoteId = Field(
        sa_column=Column(String, nullable=False, unique=True)
    )  # stable graph identity, frontmatter `id:`
    path: str = Field(unique=True)
    title: str
    content_hash: str
    # Authoritative markdown body (frontmatter stripped), the source of
    # record per ADR 006. Nullable until the one-shot reconciler backfill
    # populates pre-existing rows from disk; new upserts always set it.
    content: str | None = None
    type: str | None = None
    status: str | None = None
    visibility: Visibility | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )
    # True once a human has confirmed the automation-chosen visibility.
    # Defaults False so historical/pre-existing notes surface in the
    # /private/review audit queue until a human spot-checks them.
    visibility_verified: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false"),
    )
    source: str | None = None
    scope: str | None = None
    verification_state: str = Field(
        default="legacy",
        sa_column=Column(String, nullable=False, server_default="legacy"),
    )
    confidence: float | None = None
    valid_from: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    valid_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    published_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    observed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    review_after: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    review_policy: str | None = None
    last_reviewed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # Monotonic counter bumped by every ORM update that changes what the note
    # asserts or how it is supported (content, confidence, state, validity,
    # retelling). A review captures it at admission and renews only if it is
    # unchanged, because content_hash alone cannot see a duplicate retelling.
    revision: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    tags: list[str] = Field(default_factory=list, sa_column=Column(_STRING_ARRAY))
    aliases: list[str] = Field(default_factory=list, sa_column=Column(_STRING_ARRAY))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict, sa_column=Column(_JSONB))
    indexed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    layout_x: float | None = None
    layout_y: float | None = None
    # Force-directed layout positions computed over the public-visibility
    # subgraph only — used by GET /knowledge/public/graph so the public
    # /notes page renders a dense layout instead of inheriting the full
    # graph's positions (which leave visible holes where private clusters
    # used to anchor). Populated by a separate gardener layout pass; the
    # public endpoint COALESCEs back to layout_x/y until the first pass.
    layout_x_public: float | None = None
    layout_y_public: float | None = None
    # Soft-delete timestamp for the /private/review audit "delete" action.
    # NULL means live; NOT NULL means the row is hidden from every user-
    # facing read path (review-queue, graph, search, get-by-id). The
    # on-disk file is moved to _trash/<ts>-<slug>.md at soft-delete time;
    # undelete moves it back to the path captured in pre_delete_path.
    # Mirrors chart/migrations/20260523120000_review_soft_delete.sql.
    deleted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # Original vault-relative path captured at soft-delete time. NULL for
    # live rows. Read back by undelete_note to restore the file to its
    # original location without parsing the trash filename. Kept separate
    # from ``path`` so the live ``path`` column always reflects where the
    # file currently lives on disk (in _trash/ for deleted rows).
    pre_delete_path: str | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )


# Columns whose change is housekeeping, not a change to the claim: the review
# lease itself, the reindex and frontmatter-updated stamps and layout positions.
_REVISION_EXEMPT = frozenset(
    {
        "revision",
        "review_after",
        "review_policy",
        "last_reviewed_at",
        "indexed_at",
        "updated_at",
        "layout_x",
        "layout_y",
        "layout_x_public",
        "layout_y_public",
    }
)


@event.listens_for(Note, "before_update")
def _bump_note_revision(mapper, connection, target) -> None:
    """Advance ``revision`` on any ORM update that touches the claim itself."""
    changed = {attr.key for attr in inspect(target).attrs if attr.history.has_changes()}
    if "revision" in changed or changed - _REVISION_EXEMPT:
        # Database arithmetic serializes concurrent stale ORM writers. A local
        # integer increment could overwrite a newer retelling's revision.
        target.revision = Note.revision + 1


def bump_revision(note: "Note") -> None:
    """Record evidence that changes no persisted column (a duplicate retelling)."""
    note.revision = Note.revision + 1


class Chunk(SQLModel, table=True):
    __tablename__ = "chunks"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    note_fk: int = Field(foreign_key="knowledge.notes.id")
    chunk_index: int
    section_header: str = ""
    chunk_text: str
    embedding: list[float] = Field(sa_column=Column(Vector(1024)))

    @field_validator("embedding", mode="before")
    @classmethod
    def _parse_embedding(cls, v: object) -> object:
        if isinstance(v, str):
            return json.loads(v)
        return v


class NoteLink(SQLModel, table=True):
    __tablename__ = "note_links"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    src_note_fk: int = Field(foreign_key="knowledge.notes.id")
    target_id: str  # target note_id (frontmatter id) or raw wikilink target
    target_title: str | None = None
    # LinkKind / EdgeType are Literals for static-analysis + the
    # __init__ validator below. At the SQL level they're plain TEXT,
    # matching the migration's CHECK constraint.
    kind: LinkKind = Field(sa_column=Column(String, nullable=False))
    edge_type: EdgeType | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )

    def __init__(self, **data: Any) -> None:
        # SQLModel table models skip pydantic validators in __init__, so
        # enforce the discriminated-union invariant manually. This
        # catches typos with a Python stack trace pointing at the call
        # site instead of waiting for the Postgres CHECK violation.
        kind = data.get("kind")
        edge_type = data.get("edge_type")
        if kind == "link" and edge_type is not None:
            raise ValueError(
                f"NoteLink.kind='link' requires edge_type=None, "
                f"got edge_type={edge_type!r}"
            )
        if kind == "edge" and edge_type is None:
            raise ValueError("NoteLink.kind='edge' requires a non-None edge_type")
        super().__init__(**data)


class RawInput(SQLModel, table=True):
    __tablename__ = "raw_inputs"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    raw_id: str = Field(sa_column=Column(String, nullable=False, unique=True))
    path: str = Field(unique=True)
    source: str
    original_path: str | None = None
    # ADR 006 Phase 4d: raw markdown lives in s3://knowledge/raws/<content_hash>.md,
    # not Postgres. The column was dropped; fetch the body via raw_store.fetch_raw.
    content_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    extra: dict[str, Any] = Field(default_factory=dict, sa_column=Column(_JSONB))


class AgentReportWriteFailure(SQLModel, table=True):
    """Durable, secret-free record of an agent report persistence failure."""

    __tablename__ = "agent_report_write_failures"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    reporter_kind: str = Field(sa_column=Column(String, nullable=False))
    error_type: str = Field(sa_column=Column(String, nullable=False))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PersonalRetrievalAudit(SQLModel, table=True):
    """Attribution-only audit row for an explicit personal-scope search."""

    __tablename__ = "personal_retrieval_audit"
    __table_args__ = (
        CheckConstraint(
            "length(principal_subject) BETWEEN 1 AND 512",
            name="personal_retrieval_audit_subject_length_chk",
        ),
        CheckConstraint(
            "length(principal_actor) <= 4096",
            name="personal_retrieval_audit_actor_length_chk",
        ),
        CheckConstraint(
            "principal_authority IN ('standing', 'delegated')",
            name="personal_retrieval_audit_authority_chk",
        ),
        CheckConstraint(
            "length(personal_scope) BETWEEN 1 AND 1024",
            name="personal_retrieval_audit_scope_length_chk",
        ),
        CheckConstraint(
            "entrypoint IN ('mcp', 'http')",
            name="personal_retrieval_audit_entrypoint_chk",
        ),
        {"schema": "knowledge", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    principal_subject: str = Field(sa_column=Column(String, nullable=False))
    principal_actor: str = Field(sa_column=Column(String, nullable=False))
    principal_authority: str = Field(sa_column=Column(String, nullable=False))
    personal_scope: str = Field(sa_column=Column(String, nullable=False))
    entrypoint: str = Field(sa_column=Column(String, nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )


class AgentBoardMessage(SQLModel, table=True):
    """One expiring, scoped coordination message on the agents tier.

    ``acknowledged_by`` is an array of trusted board principal identifiers.
    Writers replace it with a compare-and-swap update so acknowledgements from
    concurrent readers are additive rather than last-writer-wins.
    """

    __tablename__ = "agent_board_messages"
    __table_args__ = (
        UniqueConstraint("source_id"),
        {"schema": "knowledge", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    principal: str = Field(sa_column=Column(String, nullable=False))
    authenticated_subject: str = Field(sa_column=Column(String, nullable=False))
    topic: str = Field(sa_column=Column(String, nullable=False))
    body: str = Field(sa_column=Column(String, nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    acknowledged_by: list[str] = Field(
        default_factory=list, sa_column=Column(_JSONB, nullable=False)
    )
    # Distress mirroring uses ``distress:<raw_id>`` here. Ordinary board posts
    # leave it null, and PostgreSQL permits multiple nulls under UNIQUE.
    source_id: str | None = Field(default=None, sa_column=Column(String, nullable=True))


class AtomRawProvenance(SQLModel, table=True):
    __tablename__ = "atom_raw_provenance"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    atom_fk: int | None = Field(default=None, foreign_key="knowledge.notes.id")
    raw_fk: int | None = Field(default=None, foreign_key="knowledge.raw_inputs.id")
    derived_note_id: str | None = None
    gardener_version: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None
    retry_count: int = Field(default=0)

    def __init__(self, **data: Any) -> None:
        # Mirror the SQL CHECK (atom_fk IS NOT NULL OR raw_fk IS NOT NULL).
        # Catches bugs at the Python call site instead of waiting for Postgres.
        atom_fk = data.get("atom_fk")
        raw_fk = data.get("raw_fk")
        if atom_fk is None and raw_fk is None:
            raise ValueError(
                "AtomRawProvenance requires at least one of atom_fk or raw_fk"
            )
        super().__init__(**data)


class Dispute(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    """Dispute writers arrive with ``dispute_fact`` (#5566).

    Until then, ``disputed`` derives from unresolved rows plus
    ``verification_state == 'disputed'``.
    """

    __tablename__ = "disputes"
    __table_args__ = (
        CheckConstraint(
            "state IN ('open', 'confirmed', 'narrowed', 'superseded', "
            "'invalidated', 'rejected', 'resolution_failed')",
            name="disputes_state_chk",
        ),
        CheckConstraint(
            "previous_verification_state IS NULL OR "
            "previous_verification_state IN "
            "('legacy', 'unverified', 'verified', 'disputed', 'invalidated')",
            name="disputes_previous_verification_state_chk",
        ),
        {"schema": "knowledge", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    note_id: str = Field(sa_column=Column(String, nullable=False))
    raw_id: str | None = None
    reason: str = Field(sa_column=Column(String, nullable=False))
    evidence: list[str] = Field(
        default_factory=list, sa_column=Column(_JSONB, nullable=False)
    )
    reporter_subject: str | None = None
    reporter_authority: str | None = None
    reporter_session: str | None = None
    previous_verification_state: str | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )
    state: str = Field(
        default="open", sa_column=Column(String, nullable=False, server_default="open")
    )
    resolution: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    resolved_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


@event.listens_for(Dispute, "before_insert")
@event.listens_for(Dispute, "before_update")
def _serialize_open_dispute(mapper, connection, target) -> None:
    """Put contested evidence in the same serialization order as note review.

    The UPDATE takes the note row lock before the dispute is visible and
    advances revision in the caller's transaction. A renewal holding that
    lock completes before this dispute, or reads the dispute and new revision
    after it commits. Neither path can renew over an intervening dispute.
    """
    if target.state in {"open", "resolution_failed"}:
        connection.execute(
            Note.__table__.update()
            .where(Note.note_id == target.note_id)
            .values(revision=Note.revision + 1)
        )


ReviewStatus = Literal["success", "failed", "unavailable", "unsupported"]


class ReviewOutcome(
    SQLModel, table=True
):  # nosemgrep: sqlmodel-datetime-without-factory
    """Durable record of one evidence review attempt, kept for every outcome.

    ``next_attempt_at`` is the admission gate: NULL after ``unsupported`` means
    do not retry until the note's revision moves, a future time is a backoff.
    Only a ``success`` row accompanies a renewed lease.
    """

    __tablename__ = "review_outcomes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('success', 'failed', 'unavailable', 'unsupported')",
            name="review_outcomes_status_chk",
        ),
        {"schema": "knowledge", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    note_id: str = Field(sa_column=Column(String, nullable=False, index=True))
    status: str = Field(sa_column=Column(String, nullable=False))
    reason: str = Field(sa_column=Column(String, nullable=False))
    note_revision: int = Field(sa_column=Column(BigInteger, nullable=False))
    attempts: int = Field(
        default=1, sa_column=Column(Integer, nullable=False, server_default="1")
    )
    evidence: list[str] = Field(
        default_factory=list, sa_column=Column(_JSONB, nullable=False)
    )
    evidence_observed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    attempted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    next_attempt_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class Intervention(
    SQLModel, table=True
):  # nosemgrep: sqlmodel-datetime-without-factory
    """Human lifecycle for a retained distress raw."""

    __tablename__ = "interventions"
    __table_args__ = (
        CheckConstraint(
            "state IN ('open', 'acknowledged', 'resolved')",
            name="interventions_state_chk",
        ),
        CheckConstraint(
            "disposition IS NULL OR disposition IN ('resolved', 'no_action')",
            name="interventions_disposition_chk",
        ),
        {"schema": "knowledge", "extend_existing": True},
    )

    raw_id: str = Field(primary_key=True, foreign_key="knowledge.raw_inputs.raw_id")
    state: str = Field(
        default="open",
        sa_column=Column(String, nullable=False, server_default="open"),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    responder_subject: str | None = None
    acknowledged_by_subject: str | None = None
    acknowledged_at: datetime | None = None
    acknowledged_request_revision: int | None = None
    decision_id: int | None = None
    decision_state: str | None = None
    associated_by_subject: str | None = None
    associated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    decision_request_revision: int | None = None
    workflow_id: str | None = None
    node_key: str | None = None
    disposition: str | None = None
    resolution: str | None = None
    resolved_at: datetime | None = None
    resolved_request_revision: int | None = None
    revision: int = Field(
        default=1, sa_column=Column(Integer, nullable=False, server_default="1")
    )
    evidence_raw_id: str | None = None
    evidence_by_subject: str | None = None
    evidence_submitted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class Gap(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    """A knowledge gap: an unresolved [[wikilink]] promoted to a trackable work item.

    Gaps are surfaced when a wikilink's target is missing from the notes graph.
    Gaps are identified globally by ``term`` (one gap per term across the whole
    graph) and link to a generated stub note via ``note_id``. Each gap carries a
    class (external/internal/hybrid/parked) and advances through a state
    machine: discovered → classified → in_review → researched → verified →
    consolidated → committed (or rejected).

    Mirrors chart/migrations/20260424000000_knowledge_gaps.sql and
    20260425000000_knowledge_gaps_stub_notes.sql — keep in sync.
    """

    __tablename__ = "gaps"
    __table_args__ = (
        UniqueConstraint("term"),
        UniqueConstraint("note_id"),
        {"schema": "knowledge", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    term: str = Field(sa_column=Column(String, nullable=False))
    context: str = Field(default="", sa_column=Column(String, nullable=False))
    note_id: str | None = Field(
        default=None,
        sa_column=Column(String, nullable=True),
    )
    # GapClass / GapState are Literals for static analysis. At the SQL level
    # they're plain TEXT, matching the migration's CHECK constraints.
    gap_class: GapClass | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )
    state: GapState = Field(
        default="discovered", sa_column=Column(String, nullable=False)
    )
    research_attempts: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    # True once a human has confirmed the automation-chosen gap_class /
    # state transition. Defaults False so historical/pre-existing gaps
    # surface in the /private/review audit queue until a human spot-checks
    # them.
    human_verified: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false"),
    )
    answer: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    classified_at: datetime | None = None
    resolved_at: datetime | None = None
    # Soft-delete timestamp for the /private/review audit "delete" action.
    # NULL means live; NOT NULL means the row is hidden from every user-
    # facing read path (review-queue, list_gaps, get_gap_by_id, graph).
    # The ``_researching/<slug>.md`` stub is hard-deleted at soft-delete
    # time and regenerated lazily by ``discover_gaps`` on undelete.
    # Mirrors chart/migrations/20260523120000_review_soft_delete.sql.
    deleted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    pipeline_version: str = Field(sa_column=Column(String, nullable=False))
