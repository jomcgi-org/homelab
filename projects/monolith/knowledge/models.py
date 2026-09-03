"""SQLModel definitions for the knowledge schema."""

import json
from datetime import datetime, timezone
from typing import Any, Literal, NewType

NoteId = NewType("NoteId", str)

from pgvector.sqlalchemy import Vector
from pydantic import field_validator
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

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

# Postgres uses native TEXT[] for tags/aliases; SQLite falls back to JSON
# so the in-memory test fixture can create the tables.
_STRING_ARRAY = PG_ARRAY(String).with_variant(JSON(), "sqlite")
# Postgres uses JSONB (matching the migration + GIN index); SQLite falls
# back to JSON.
_JSONB = JSONB().with_variant(JSON(), "sqlite")


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
    observed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
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


class RepoDoc(SQLModel, table=True):
    """A repo markdown file indexed for public-chat grounding.

    Isolated from ``Note`` on purpose: the gardener and gap loop operate over
    ``knowledge.notes`` and must never touch these machine-synced, fully
    reconstructable rows. Identified by repo-relative ``path``; ``content_hash``
    is the change-detection key driving the reconcile job.

    Mirrors chart/migrations/20260618120000_repo_docs.sql - keep in sync.
    """

    __tablename__ = "repo_docs"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    path: str = Field(sa_column=Column(String, nullable=False, unique=True))
    content_hash: str
    title: str
    indexed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RepoDocChunk(SQLModel, table=True):
    """One embedded chunk of a RepoDoc. Mirrors knowledge.Chunk's embedding
    column exactly so it round-trips through the SQLite create_all fixtures.

    Mirrors chart/migrations/20260618120000_repo_docs.sql - keep in sync.
    """

    __tablename__ = "repo_doc_chunks"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    repo_doc_fk: int = Field(foreign_key="knowledge.repo_docs.id")
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
    __tablename__ = "disputes"
    __table_args__ = (
        CheckConstraint(
            "state IN ('open', 'confirmed', 'narrowed', 'superseded', "
            "'invalidated', 'rejected')",
            name="disputes_state_chk",
        ),
        {"schema": "knowledge", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    note_id: str = Field(sa_column=Column(String, nullable=False))
    raw_id: str | None = None
    reason: str = Field(sa_column=Column(String, nullable=False))
    evidence: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(_JSONB, nullable=False)
    )
    reporter_subject: str | None = None
    reporter_authority: str | None = None
    reporter_session: str | None = None
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
