-- #5899: anchor free-form knowledge facts to a repository-seeded entity spine.

CREATE TABLE knowledge.entities (
    id         BIGSERIAL PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('project','service','environment','issue')),
    slug       TEXT NOT NULL,
    title      TEXT NOT NULL,
    aliases    TEXT[] NOT NULL DEFAULT '{}',
    scope      TEXT,
    source     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, slug)
);

CREATE INDEX entities_aliases_gin ON knowledge.entities USING gin (aliases);

CREATE TABLE knowledge.note_entities (
    id         BIGSERIAL PRIMARY KEY,
    note_id    TEXT NOT NULL,
    entity_id  BIGINT NOT NULL REFERENCES knowledge.entities(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('subject','mentions')),
    source     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (note_id, entity_id, role)
);

CREATE INDEX note_entities_note_id_idx ON knowledge.note_entities (note_id);
CREATE INDEX note_entities_entity_id_idx ON knowledge.note_entities (entity_id);

-- Repair the three known malformed production scopes before enforcing the shape.
UPDATE knowledge.notes
SET scope = regexp_replace(scope, E'[​‌‍﻿]', '', 'g')
WHERE scope IS NOT NULL
  AND scope ~ E'[​‌‍﻿]';

UPDATE knowledge.notes
SET scope = 'session:codex-session'
WHERE scope = 'environment:codex-session';

ALTER TABLE knowledge.notes
    ADD CONSTRAINT notes_scope_shape_chk CHECK (
        scope IS NULL OR scope ~ '^(personal|org|repo|environment|session):.+$'
    ) NOT VALID;

ALTER TABLE knowledge.notes VALIDATE CONSTRAINT notes_scope_shape_chk;

CREATE VIEW public_api.knowledge_entities AS
    SELECT
        id,
        kind,
        slug,
        title,
        aliases,
        scope,
        source,
        created_at,
        updated_at
    FROM knowledge.entities;

CREATE VIEW public_api.knowledge_note_entities AS
    SELECT
        ne.id,
        ne.note_id,
        ne.entity_id,
        ne.role,
        ne.source,
        ne.created_at,
        n.verification_state,
        n.indexed_at AS note_indexed_at
    FROM knowledge.note_entities ne
    JOIN public_api.knowledge_notes pn ON pn.note_id = ne.note_id
    JOIN knowledge.notes n ON n.note_id = ne.note_id;

GRANT SELECT ON public_api.knowledge_entities TO public_reader;
GRANT SELECT ON public_api.knowledge_note_entities TO public_reader;

GRANT SELECT ON knowledge.entities TO agents_writer;
GRANT SELECT ON knowledge.note_entities TO agents_writer;
