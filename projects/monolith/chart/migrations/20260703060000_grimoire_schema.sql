-- Grimoire v1: corpus (sourcebook entities/chunks/embeddings/edges) and
-- campaign (grants/PCs/sessions) tables, per ADR 011 and the pg-first spec
-- (docs/decisions/services/011-grimoire-hot-tier-schema.md,
-- docs/plans/2026-07-02-grimoire-pg-first-spec.md). Both live in one schema
-- for v1; the two-schema corpus/campaign split is a checkout-time concern
-- that does not exist yet.

CREATE SCHEMA IF NOT EXISTS grimoire;

-- Entity spine (CTI base). Typed detail tables below carry the queryable
-- scalars per entity_type; jsonb is reserved for irregular nested payloads.
CREATE TABLE grimoire.entity (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type        TEXT NOT NULL CHECK (entity_type IN ('creature', 'spell', 'location', 'npc', 'faction', 'deity', 'item')),
    name               TEXT NOT NULL,
    source_type        TEXT NOT NULL DEFAULT 'extracted' CHECK (source_type IN ('extracted', 'homebrew')),
    is_global          BOOLEAN NOT NULL DEFAULT TRUE,
    source_book        TEXT,
    created_in_session UUID,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_grimoire_entity_type_name ON grimoire.entity (entity_type, name);

CREATE TABLE grimoire.entity_creature (
    entity_id      UUID PRIMARY KEY REFERENCES grimoire.entity(id) ON DELETE CASCADE,
    size           TEXT,
    creature_type  TEXT,
    ac             INTEGER,
    hp_avg         INTEGER,
    cr             NUMERIC,
    speed          JSONB,
    ability_scores JSONB,
    actions        JSONB,
    traits         JSONB
);

CREATE TABLE grimoire.entity_spell (
    entity_id    UUID PRIMARY KEY REFERENCES grimoire.entity(id) ON DELETE CASCADE,
    level        INTEGER,
    school       TEXT,
    casting_time TEXT,
    range        TEXT,
    components   TEXT,
    duration     TEXT,
    classes      JSONB,
    description  TEXT
);

CREATE TABLE grimoire.entity_location (
    entity_id     UUID PRIMARY KEY REFERENCES grimoire.entity(id) ON DELETE CASCADE,
    location_type TEXT,
    region        TEXT,
    description   TEXT
);

CREATE TABLE grimoire.entity_npc (
    entity_id   UUID PRIMARY KEY REFERENCES grimoire.entity(id) ON DELETE CASCADE,
    race        TEXT,
    occupation  TEXT,
    disposition TEXT,
    description TEXT
);

-- Sourcebook chunks, loaded from s3://grimoire/chunks/<book_id>.ndjson.
CREATE TABLE grimoire.knowledge_chunk (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    book_id      TEXT NOT NULL,
    chunk_ref    TEXT NOT NULL,
    content      TEXT NOT NULL,
    section_path TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (book_id, chunk_ref)
);

CREATE TABLE grimoire.chunk_entity_mention (
    chunk_id     UUID NOT NULL REFERENCES grimoire.knowledge_chunk(id) ON DELETE CASCADE,
    entity_id    UUID NOT NULL REFERENCES grimoire.entity(id) ON DELETE CASCADE,
    mention_text TEXT,
    UNIQUE (chunk_id, entity_id)
);

CREATE TABLE grimoire.relationship (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_entity_id UUID NOT NULL REFERENCES grimoire.entity(id) ON DELETE CASCADE,
    to_entity_id   UUID NOT NULL REFERENCES grimoire.entity(id) ON DELETE CASCADE,
    rel_type       TEXT NOT NULL,
    properties     JSONB,
    UNIQUE (from_entity_id, to_entity_id, rel_type)
);

-- One generic ANN surface spanning entities, chunks, and (later) session
-- transcripts: a single kNN scan answers "search sourcebook knowledge and
-- session history with the same vector query". ivfflat/hnsw index deferred
-- until row counts justify it (seq scan is fine at v1 scale). The `vector`
-- extension is already created by the existing knowledge migrations.
CREATE TABLE grimoire.embedding (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    embeddable_kind TEXT NOT NULL CHECK (embeddable_kind IN ('entity', 'chunk', 'transcript')),
    embeddable_id  UUID NOT NULL,
    model          TEXT NOT NULL,
    dim            INTEGER NOT NULL,
    vector         vector(1024) NOT NULL,
    UNIQUE (embeddable_kind, embeddable_id, model)
);

CREATE TABLE grimoire.campaign (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    dm_name    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE grimoire.player_character (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id    UUID NOT NULL REFERENCES grimoire.campaign(id),
    player_name    TEXT,
    character_name TEXT NOT NULL,
    class_name     TEXT,
    level          INTEGER,
    sheet          JSONB
);

CREATE TABLE grimoire.game_session (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id),
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'ended')),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ
);
-- Single-active-session invariant: at most one non-ended session per campaign.
CREATE UNIQUE INDEX idx_grimoire_game_session_one_active ON grimoire.game_session (campaign_id) WHERE status != 'ended';

CREATE TABLE grimoire.knowledge_grant (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id        UUID NOT NULL REFERENCES grimoire.campaign(id),
    entity_id          UUID NOT NULL REFERENCES grimoire.entity(id) ON DELETE CASCADE,
    player_character_id UUID NOT NULL REFERENCES grimoire.player_character(id) ON DELETE CASCADE,
    grant_scope        TEXT NOT NULL CHECK (grant_scope IN ('full', 'partial', 'name_only')),
    revealed_details   JSONB,
    granted_in_session UUID,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity_id, player_character_id)
);
