"""SQLModel definitions for the grimoire schema.

Mirrors chart/migrations/20260703070000_grimoire_schema.sql - keep in sync.
CTI entity spine + typed detail tables per ADR 011
(docs/decisions/services/011-grimoire-hot-tier-schema.md); jsonb reserved for
irregular nested display-only payloads (speed/ability_scores/actions/traits,
classes, sheet, properties, revealed_details).
"""

import uuid
from datetime import datetime, timezone
from typing import Literal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import JSON, Field, SQLModel

# Mirror of the CHECK constraint in
# chart/migrations/20260703070000_grimoire_schema.sql - keep in sync.
EntityType = Literal["creature", "spell", "location", "npc", "faction", "deity", "item"]
SourceType = Literal["extracted", "homebrew"]
EmbeddableKind = Literal["entity", "chunk", "transcript"]
SessionStatus = Literal["active", "paused", "ended"]
GrantScope = Literal["full", "partial", "name_only"]

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


class Entity(SQLModel, table=True):
    __tablename__ = "entity"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('creature', 'spell', 'location', 'npc', 'faction', 'deity', 'item')",
            name="entity_entity_type_chk",
        ),
        CheckConstraint(
            "source_type IN ('extracted', 'homebrew')",
            name="entity_source_type_chk",
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
    source_type: SourceType = Field(
        default="extracted", sa_column=Column(String, nullable=False)
    )
    is_global: bool = True
    source_book: str | None = None
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
