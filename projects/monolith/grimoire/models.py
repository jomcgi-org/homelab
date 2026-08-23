"""SQLModel definitions for the grimoire schema.

Mirrors chart/migrations/20260703070000_grimoire_schema.sql - keep in sync.
CTI entity spine + typed detail tables per the Grimoire hot-tier schema
(projects/monolith/ARCHITECTURE.md, section 6); jsonb reserved for
irregular nested display-only payloads (speed/ability_scores/actions/traits,
classes, sheet, properties, revealed_details).
"""

import uuid
from datetime import datetime, timezone
from typing import Literal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import JSON, Field, SQLModel

# Mirror of the CHECK constraint in
# chart/migrations/20260703070000_grimoire_schema.sql, expanded by
# 20260705150000_grimoire_extraction_v4.sql to the generic typed-extraction set
# (lore + gameplay + mechanics) and by 20260706010000_grimoire_table_type.sql with
# the mechanics `table` type - keep in sync. Grouped by the DERIVED category: lore
# (unchanged from v1), gameplay (event/quest), mechanics (D&D game rules).
EntityType = Literal[
    # lore
    "creature",
    "spell",
    "location",
    "npc",
    "faction",
    "deity",
    "item",
    # gameplay
    "event",
    "quest",
    # mechanics
    "condition",
    "feat",
    "race",
    "background",
    "class",
    "subclass",
    "class_feature",
    "action",
    "rule",
    # a table/list of options captured as ONE entity (v6): a treasure or magic-item
    # table, a spell list, a class progression, a random-encounter table.
    "table",
]
# Category is DERIVED from entity_type by a stored generated column (see
# _ENTITY_CATEGORY_EXPR / the migration), never written by the app.
Category = Literal["lore", "gameplay", "mechanics"]
# Temporality is set only for event/quest (nullable everywhere else).
Temporality = Literal["historical", "present", "future"]
SourceType = Literal["extracted", "homebrew"]
EmbeddableKind = Literal["entity", "chunk", "transcript"]
SessionStatus = Literal["active", "paused", "ended"]
GrantScope = Literal["full", "partial", "name_only"]
# Mirror of the CHECK constraint in
# chart/migrations/20260703120000_grimoire_chunk_extraction.sql - keep in sync.
ExtractionStatus = Literal["ok", "empty"]

# Postgres stores true UUIDs; SQLite (test fixtures) falls back to a plain
# string column, matching the pattern in knowledge/models.py's _STRING_ARRAY
# / _JSONB variant columns.
_UUID = PG_UUID(as_uuid=False).with_variant(String(36), "sqlite")
# Postgres uses JSONB (matching the migration); SQLite falls back to JSON.
_JSONB = JSONB().with_variant(JSON(), "sqlite")


def _uuid_column(
    *, primary_key: bool = False, nullable: bool = True, fk: str | None = None
) -> Column:
    # SQLModel's Field(foreign_key=...) is not supported alongside sa_column,
    # so the FK constraint is attached directly to the Column here.
    args = [_UUID] if fk is None else [_UUID, ForeignKey(fk)]
    return Column(*args, primary_key=primary_key, nullable=nullable)


# Type -> category derivation (spec v4): a stored generated column so category is
# ALWAYS a clean function of entity_type on both Postgres (the migration) and
# SQLite (create_all fixtures). spell stays in lore here; the mechanics surface
# unions spell in at QUERY time (category='mechanics' OR entity_type='spell'), not
# at the column level. Mirror of the CASE in
# 20260705150000_grimoire_extraction_v4.sql - keep in sync.
_ENTITY_CATEGORY_EXPR = (
    "CASE "
    "WHEN entity_type IN ("
    "'creature', 'spell', 'location', 'npc', 'faction', 'deity', 'item'"
    ") THEN 'lore' "
    "WHEN entity_type IN ('event', 'quest') THEN 'gameplay' "
    "ELSE 'mechanics' END"
)


class Entity(SQLModel, table=True):
    __tablename__ = "entity"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ("
            "'creature', 'spell', 'location', 'npc', 'faction', 'deity', 'item', "
            "'event', 'quest', "
            "'condition', 'feat', 'race', 'background', 'class', 'subclass', "
            "'class_feature', 'action', 'rule', 'table')",
            name="entity_entity_type_chk",
        ),
        CheckConstraint(
            "source_type IN ('extracted', 'homebrew')",
            name="entity_source_type_chk",
        ),
        CheckConstraint(
            "temporality IS NULL OR temporality IN ('historical', 'present', 'future')",
            name="entity_temporality_chk",
        ),
        {"schema": "grimoire", "extend_existing": True},
    )

    # Generated app-side (not relying on the migration's DEFAULT
    # gen_random_uuid(), which SQLite create_all fixtures cannot run) so the
    # id is populated the same way on both backends.
    id: str | None = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=_uuid_column(primary_key=True),
    )
    entity_type: EntityType = Field(sa_column=Column(String, nullable=False))
    name: str
    # DERIVED, never written: a stored generated column over entity_type. Read
    # after a flush/refresh; the app must not pass a value (the DB computes it).
    category: Category | None = Field(
        default=None,
        sa_column=Column(
            String, Computed(_ENTITY_CATEGORY_EXPR, persisted=True), nullable=False
        ),
    )
    # Set only for event/quest (the DB CHECK allows NULL for every other type).
    temporality: Temporality | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )
    # Generic typed-detail payload for the gameplay/mechanics types that have no
    # dedicated detail table (event/quest/condition/feat/.../rule). creature,
    # location, npc, and spell keep their own typed detail tables and do NOT use
    # this column. Postgres JSONB, SQLite JSON.
    detail: dict | None = Field(default=None, sa_column=Column(_JSONB))
    source_type: SourceType = Field(
        default="extracted", sa_column=Column(String, nullable=False)
    )
    is_global: bool = True
    source_book: str | None = None
    # Parent-place key for a location that is its own keyed entry (a dungeon
    # room's containing site), lower-cased. Keeps same-named rooms in different
    # dungeons/books distinct under the otherwise-global (entity_type,
    # lower(name)) dedup; NULL for non-location entities and for prose location
    # references that name no site. Mirror of the column added in
    # 20260706000000_grimoire_extraction_hardening.sql.
    site: str | None = None
    created_in_session: str | None = Field(default=None, sa_column=_uuid_column())
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )


class EntityCreature(SQLModel, table=True):
    __tablename__ = "entity_creature"
    __table_args__ = {"schema": "grimoire", "extend_existing": True}

    entity_id: str = Field(
        sa_column=_uuid_column(
            primary_key=True, nullable=False, fk="grimoire.entity.id"
        ),
    )
    size: str | None = None
    creature_type: str | None = None
    ac: int | None = None
    hp_avg: int | None = None
    cr: float | None = None
    speed: dict = Field(default_factory=dict, sa_column=Column(_JSONB))
    ability_scores: dict = Field(default_factory=dict, sa_column=Column(_JSONB))
    actions: dict = Field(default_factory=dict, sa_column=Column(_JSONB))
    traits: dict = Field(default_factory=dict, sa_column=Column(_JSONB))


class EntitySpell(SQLModel, table=True):
    __tablename__ = "entity_spell"
    __table_args__ = {"schema": "grimoire", "extend_existing": True}

    entity_id: str = Field(
        sa_column=_uuid_column(
            primary_key=True, nullable=False, fk="grimoire.entity.id"
        ),
    )
    level: int | None = None
    school: str | None = None
    casting_time: str | None = None
    range: str | None = None
    components: str | None = None
    duration: str | None = None
    classes: dict = Field(default_factory=dict, sa_column=Column(_JSONB))
    description: str | None = None


class EntityLocation(SQLModel, table=True):
    __tablename__ = "entity_location"
    __table_args__ = {"schema": "grimoire", "extend_existing": True}

    entity_id: str = Field(
        sa_column=_uuid_column(
            primary_key=True, nullable=False, fk="grimoire.entity.id"
        ),
    )
    location_type: str | None = None
    region: str | None = None
    description: str | None = None


class EntityNpc(SQLModel, table=True):
    __tablename__ = "entity_npc"
    __table_args__ = {"schema": "grimoire", "extend_existing": True}

    entity_id: str = Field(
        sa_column=_uuid_column(
            primary_key=True, nullable=False, fk="grimoire.entity.id"
        ),
    )
    race: str | None = None
    occupation: str | None = None
    disposition: str | None = None
    description: str | None = None


class KnowledgeChunk(SQLModel, table=True):
    __tablename__ = "knowledge_chunk"
    __table_args__ = (
        UniqueConstraint(
            "book_id", "chunk_ref", name="knowledge_chunk_book_id_chunk_ref_key"
        ),
        {"schema": "grimoire", "extend_existing": True},
    )

    # Generated app-side (not relying on the migration's DEFAULT
    # gen_random_uuid(), which SQLite create_all fixtures cannot run) so the
    # id is populated the same way on both backends.
    id: str | None = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=_uuid_column(primary_key=True),
    )
    book_id: str
    chunk_ref: str
    content: str
    section_path: str | None = None
    # Full section-ancestry breadcrumb (e.g. "Chapter 3: Magic Items > Armor >
    # Armor of Vulnerability"), joined by " > ", shallowest ancestor first. Used
    # ONLY as extraction context (the user message's Section: line), never for the
    # reader/nav (that stays on the 2-level section_path). NULL for chunks loaded
    # before the backfill; extraction falls back to section_path. Extraction-only
    # metadata, so a change never re-embeds. Mirror of the column added in
    # 20260705130000_grimoire_chunk_section_hierarchy.sql.
    section_hierarchy: str | None = None
    # Reading-order position within a book, 0-based, assigned by the loader from
    # NDJSON line order (ingest._upsert_book_chunks). created_at is unreliable for
    # ordering (bulk upserts share a timestamp; re-uploads mutate in place), so
    # every reading-order read path (section tree, chunk reader, paged list)
    # orders by seq instead. Mirror of the column added in
    # 20260703250000_grimoire_chunk_seq_and_book.sql.
    seq: int | None = None
    # Full s3:// URI of the source illustration for image-derived chunks (Marker
    # Picture blocks); NULL for text chunks. Stored so the app can later render
    # the image (via imgproxy) alongside the retrieved chunk. Mirror of the
    # column added in 20260703130000_grimoire_chunk_image_ref.sql.
    image_ref: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )


class Book(SQLModel, table=True):
    """Human-facing metadata for a loaded book.

    book_id is a slug/UUID everywhere else in the schema; this table gives it a
    display_name (defaulting to the id until renamed from the Library UI) so the
    app never has to show a raw id. The loader upserts one row per book it sees
    (ingest._upsert_book), and PATCH /books/{book_id} renames it. Mirror of
    20260703250000_grimoire_chunk_seq_and_book.sql.
    """

    __tablename__ = "book"
    __table_args__ = {"schema": "grimoire", "extend_existing": True}

    # book_id is a caller-supplied slug/UUID (the S3 path segment), not a
    # generated surrogate, so the string id is the primary key directly.
    id: str = Field(sa_column=Column(String, primary_key=True, nullable=False))
    display_name: str
    # Gates the public full-text Reader: copyrighted books are listed on the
    # public Library and power the transformative surfaces (Entities/Chat/
    # Explore), but their verbatim text/images are never served publicly. See
    # library.is_book_copyrighted and 20260709120000_grimoire_book_copyrighted.sql.
    # Defaults TRUE so a newly ingested book fails closed (Reader locked) until
    # explicitly classified open (ingest.OPEN_LICENSE_BOOK_IDS).
    copyrighted_content: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text("true")),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )


class Adventure(SQLModel, table=True):
    """One self-contained runnable module within a book (structural layer).

    Boundaries are a contiguous ``seq`` range over the book's chunks; entity
    membership is derived by join (see grimoire.adventure_entity), never
    re-extracted. Mirror of 20260705160000_grimoire_adventure.sql.
    """

    __tablename__ = "adventure"
    __table_args__ = (
        UniqueConstraint("book_id", "name", name="adventure_book_id_name_key"),
        UniqueConstraint("book_id", "seq", name="adventure_book_id_seq_key"),
        {"schema": "grimoire", "extend_existing": True},
    )

    # Generated app-side (not relying on the migration's DEFAULT
    # gen_random_uuid(), which SQLite create_all fixtures cannot run) so the
    # id is populated the same way on both backends.
    id: str | None = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=_uuid_column(primary_key=True),
    )
    book_id: str
    name: str
    seq: int
    summary: str | None = None
    level_range: str | None = None
    start_seq: int
    end_seq: int | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )


class ChunkEntityMention(SQLModel, table=True):
    __tablename__ = "chunk_entity_mention"
    # Pure association table: the (chunk_id, entity_id) pair is the natural key,
    # so it is the composite PRIMARY KEY (matching the migration) rather than a
    # surrogate id column. A separate id would be the only non-UUID surrogate PK
    # in the schema, and it was never read for its value (only as an existence
    # probe), so dropping it keeps model and migration in lockstep.
    __table_args__ = {"schema": "grimoire", "extend_existing": True}

    chunk_id: str = Field(
        sa_column=_uuid_column(
            primary_key=True, nullable=False, fk="grimoire.knowledge_chunk.id"
        ),
    )
    entity_id: str = Field(
        sa_column=_uuid_column(
            primary_key=True, nullable=False, fk="grimoire.entity.id"
        ),
    )
    mention_text: str | None = None


class ChunkExtraction(SQLModel, table=True):
    """Processed-marker: one row per (chunk, model, prompt_version) successfully
    extracted. Presence means "this chunk is done under this exact model +
    prompt version"; absence means pending. The key stores the prompt VERSION
    LABEL (``v1``, ``v2``, ...), not a sha256 of the prompt text, so promoting a
    prompt is a deliberate pointer move rather than an accidental byte-diff (see
    extract.PROMPT_VERSIONS). We record status='empty' for zero-yield chunks (so
    they aren't re-run forever) but write NOTHING on HTTP/parse failure, so
    genuine failures are naturally re-selected next run. The key deliberately
    excludes a chunk content hash (in-place content edits are out of scope;
    re-loading a changed chunk under a seen model+version will not re-extract
    until content hashing is added). Mirror of the column swap in
    chart/migrations/20260705120000_grimoire_prompt_version.sql."""

    __tablename__ = "chunk_extraction"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'empty')",
            name="chunk_extraction_status_chk",
        ),
        {"schema": "grimoire", "extend_existing": True},
    )

    chunk_id: str = Field(
        sa_column=_uuid_column(
            primary_key=True, nullable=False, fk="grimoire.knowledge_chunk.id"
        )
    )
    model: str = Field(sa_column=Column(String, primary_key=True, nullable=False))
    prompt_version: str = Field(
        sa_column=Column(String, primary_key=True, nullable=False)
    )
    status: ExtractionStatus = Field(sa_column=Column(String, nullable=False))
    extracted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Relationship(SQLModel, table=True):
    __tablename__ = "relationship"
    __table_args__ = (
        UniqueConstraint(
            "from_entity_id",
            "to_entity_id",
            "rel_type",
            name="relationship_from_entity_id_to_entity_id_rel_type_key",
        ),
        {"schema": "grimoire", "extend_existing": True},
    )

    # Generated app-side (not relying on the migration's DEFAULT
    # gen_random_uuid(), which SQLite create_all fixtures cannot run) so the
    # id is populated the same way on both backends.
    id: str | None = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=_uuid_column(primary_key=True),
    )
    from_entity_id: str = Field(
        sa_column=_uuid_column(nullable=False, fk="grimoire.entity.id"),
    )
    to_entity_id: str = Field(
        sa_column=_uuid_column(nullable=False, fk="grimoire.entity.id"),
    )
    rel_type: str
    properties: dict = Field(default_factory=dict, sa_column=Column(_JSONB))
    # The knowledge_chunk this edge was first extracted from (provenance). Dedup
    # is on the (from, to, rel_type) triple, so the first writer's chunk wins and
    # a later duplicate never overwrites it. Nullable: edges written before this
    # column, and any future non-extraction writer, have NULL. Mirror of the
    # column added in 20260706000000_grimoire_extraction_hardening.sql.
    chunk_id: str | None = Field(
        default=None,
        sa_column=_uuid_column(nullable=True, fk="grimoire.knowledge_chunk.id"),
    )


class Embedding(SQLModel, table=True):
    __tablename__ = "embedding"
    __table_args__ = (
        CheckConstraint(
            "embeddable_kind IN ('entity', 'chunk', 'transcript')",
            name="embedding_embeddable_kind_chk",
        ),
        UniqueConstraint(
            "embeddable_kind",
            "embeddable_id",
            "model",
            name="embedding_embeddable_kind_embeddable_id_model_key",
        ),
        {"schema": "grimoire", "extend_existing": True},
    )

    # Generated app-side (not relying on the migration's DEFAULT
    # gen_random_uuid(), which SQLite create_all fixtures cannot run) so the
    # id is populated the same way on both backends.
    id: str | None = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=_uuid_column(primary_key=True),
    )
    embeddable_kind: EmbeddableKind = Field(sa_column=Column(String, nullable=False))
    embeddable_id: str = Field(sa_column=_uuid_column(nullable=False))
    model: str
    dim: int
    vector: list[float] = Field(sa_column=Column(Vector(1024), nullable=False))


class Campaign(SQLModel, table=True):
    __tablename__ = "campaign"
    __table_args__ = {"schema": "grimoire", "extend_existing": True}

    # Generated app-side (not relying on the migration's DEFAULT
    # gen_random_uuid(), which SQLite create_all fixtures cannot run) so the
    # id is populated the same way on both backends.
    id: str | None = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=_uuid_column(primary_key=True),
    )
    name: str
    dm_name: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )


class PlayerCharacter(SQLModel, table=True):
    __tablename__ = "player_character"
    __table_args__ = {"schema": "grimoire", "extend_existing": True}

    # Generated app-side (not relying on the migration's DEFAULT
    # gen_random_uuid(), which SQLite create_all fixtures cannot run) so the
    # id is populated the same way on both backends.
    id: str | None = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=_uuid_column(primary_key=True),
    )
    campaign_id: str = Field(
        sa_column=_uuid_column(nullable=False, fk="grimoire.campaign.id"),
    )
    player_name: str | None = None
    character_name: str
    class_name: str | None = None
    level: int | None = None
    sheet: dict = Field(default_factory=dict, sa_column=Column(_JSONB))


# nosemgrep: sqlmodel-datetime-without-factory (ended_at is intentionally NULL until the session ends)
class GameSession(SQLModel, table=True):
    __tablename__ = "game_session"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'ended')",
            name="game_session_status_chk",
        ),
        {"schema": "grimoire", "extend_existing": True},
    )

    # Generated app-side (not relying on the migration's DEFAULT
    # gen_random_uuid(), which SQLite create_all fixtures cannot run) so the
    # id is populated the same way on both backends.
    id: str | None = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=_uuid_column(primary_key=True),
    )
    campaign_id: str = Field(
        sa_column=_uuid_column(nullable=False, fk="grimoire.campaign.id"),
    )
    status: SessionStatus = Field(
        default="active", sa_column=Column(String, nullable=False)
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )
    ended_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    # The single-active-session invariant (at most one non-ended session per
    # campaign) is a Postgres partial unique index
    # (idx_grimoire_game_session_one_active), not representable in SQLite
    # create_all fixtures; enforce it in application code on the write path.


class KnowledgeGrant(SQLModel, table=True):
    __tablename__ = "knowledge_grant"
    __table_args__ = (
        CheckConstraint(
            "grant_scope IN ('full', 'partial', 'name_only')",
            name="knowledge_grant_grant_scope_chk",
        ),
        UniqueConstraint(
            "entity_id",
            "player_character_id",
            name="knowledge_grant_entity_id_player_character_id_key",
        ),
        {"schema": "grimoire", "extend_existing": True},
    )

    # Generated app-side (not relying on the migration's DEFAULT
    # gen_random_uuid(), which SQLite create_all fixtures cannot run) so the
    # id is populated the same way on both backends.
    id: str | None = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=_uuid_column(primary_key=True),
    )
    campaign_id: str = Field(
        sa_column=_uuid_column(nullable=False, fk="grimoire.campaign.id"),
    )
    entity_id: str = Field(
        sa_column=_uuid_column(nullable=False, fk="grimoire.entity.id"),
    )
    player_character_id: str = Field(
        sa_column=_uuid_column(nullable=False, fk="grimoire.player_character.id"),
    )
    grant_scope: GrantScope = Field(sa_column=Column(String, nullable=False))
    revealed_details: dict | None = Field(default=None, sa_column=Column(_JSONB))
    granted_in_session: str | None = Field(default=None, sa_column=_uuid_column())
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )


# Entity types with no row here (faction, deity, item) are spine-only per
# ADR 011: extraction can emit them but no typed detail table exists yet.
ENTITY_DETAIL_MODELS: dict[str, type[SQLModel]] = {
    "creature": EntityCreature,
    "spell": EntitySpell,
    "location": EntityLocation,
    "npc": EntityNpc,
}
