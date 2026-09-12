-- Grimoire campaign isolation (ADR services/011).
--
-- grimoire is the long-lived shared corpus schema. It retains extracted
-- entities, details, chunks, relationships, embeddings, books and adventures,
-- plus the small campaign registry needed to route trusted requests. Each
-- registry row owns one deterministic grimoire_campaign_<uuidhex> schema.
-- Character metadata, grants, live game sessions, campaign transcripts and
-- homebrew overlays live only in that schema.
--
-- grimoire_chat is intentionally unchanged: it is anonymous public chat over
-- the shared corpus and has no campaign identity. Its transcripts are governed
-- by ADR security/005, not campaign play state.
--
-- Campaign reads use schema-qualified views, never search_path. A NOLOGIN
-- campaign access role receives SELECT on the view surface and DML on campaign
-- backing tables, while transaction-bound context and row security restrict it
-- to the one validated registry route. The trusted application identity
-- provisions schemas and ingests the corpus, then each routed transaction uses
-- SET LOCAL ROLE to cross the least-privilege boundary. PUBLIC receives neither
-- CREATE nor table privileges; no schema or role name comes from request input.

CREATE OR REPLACE FUNCTION grimoire.campaign_schema_name(campaign_id UUID)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
RETURN 'grimoire_campaign_' || replace(campaign_id::text, '-', '');

DO $$
BEGIN
    -- CNPG creates this NOLOGIN role before Atlas runs in deployed clusters.
    -- The fallback keeps standalone/test PostgreSQL migration runs complete.
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'grimoire_campaign_access'
    ) THEN
        CREATE ROLE grimoire_campaign_access NOLOGIN NOSUPERUSER NOCREATEDB
            NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
END;
$$;

ALTER TABLE grimoire.campaign ADD COLUMN schema_name TEXT;
UPDATE grimoire.campaign
SET schema_name = grimoire.campaign_schema_name(id);
ALTER TABLE grimoire.campaign ALTER COLUMN schema_name SET NOT NULL;
ALTER TABLE grimoire.campaign
    ADD CONSTRAINT campaign_schema_name_key UNIQUE (schema_name),
    ADD CONSTRAINT campaign_schema_name_canonical_chk
        CHECK (schema_name = grimoire.campaign_schema_name(id));

CREATE TABLE grimoire.campaign_access_context (
    backend_pid INTEGER PRIMARY KEY,
    transaction_id XID8 NOT NULL,
    campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    schema_name TEXT NOT NULL,
    CONSTRAINT campaign_access_context_route_chk
        CHECK (schema_name = grimoire.campaign_schema_name(campaign_id))
);
REVOKE ALL ON grimoire.campaign_access_context FROM PUBLIC;
REVOKE ALL ON grimoire.campaign_access_context FROM grimoire_campaign_access;

CREATE OR REPLACE FUNCTION grimoire.set_campaign_access_context(
    target_campaign_id UUID
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    target_schema TEXT;
BEGIN
    SELECT c.schema_name INTO target_schema
    FROM grimoire.campaign AS c
    WHERE c.id = target_campaign_id
      AND c.schema_name = grimoire.campaign_schema_name(target_campaign_id)
    FOR SHARE;
    IF target_schema IS NULL THEN
        RAISE EXCEPTION 'campaign % has no valid access route', target_campaign_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    INSERT INTO grimoire.campaign_access_context (
        backend_pid, transaction_id, campaign_id, schema_name
    ) VALUES (
        pg_backend_pid(), pg_current_xact_id(), target_campaign_id, target_schema
    )
    ON CONFLICT (backend_pid) DO UPDATE
    SET transaction_id = EXCLUDED.transaction_id,
        campaign_id = EXCLUDED.campaign_id,
        schema_name = EXCLUDED.schema_name;
    RETURN target_schema;
END;
$$;

CREATE OR REPLACE FUNCTION grimoire.campaign_context_matches(
    target_schema TEXT
)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
RETURN EXISTS (
    SELECT 1
    FROM grimoire.campaign_access_context AS context
    WHERE context.backend_pid = pg_backend_pid()
      AND context.transaction_id = pg_current_xact_id_if_assigned()
      AND context.schema_name = target_schema
);

REVOKE ALL ON FUNCTION grimoire.set_campaign_access_context(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION grimoire.campaign_context_matches(TEXT) FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA grimoire FROM grimoire_campaign_access;
GRANT USAGE ON SCHEMA grimoire TO grimoire_campaign_access;
GRANT EXECUTE ON FUNCTION grimoire.campaign_context_matches(TEXT)
    TO grimoire_campaign_access;

CREATE OR REPLACE FUNCTION grimoire.lock_campaign_entity(
    target_schema TEXT,
    target_entity_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    entity_exists BOOLEAN := FALSE;
BEGIN
    IF target_schema !~ '^grimoire_campaign_[0-9a-f]{32}$'
       OR NOT EXISTS (
           SELECT 1 FROM grimoire.campaign AS c
           WHERE c.schema_name = target_schema
       ) THEN
        RAISE EXCEPTION 'invalid campaign schema %', target_schema
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT TRUE INTO entity_exists
    FROM grimoire.entity AS e
    WHERE e.id = target_entity_id
    FOR KEY SHARE;

    IF entity_exists IS NOT TRUE THEN
        EXECUTE format(
            'SELECT TRUE FROM %I.homebrew_entity WHERE id = $1 FOR KEY SHARE',
            target_schema
        ) INTO entity_exists USING target_entity_id;
    END IF;

    IF entity_exists IS NOT TRUE THEN
        RAISE EXCEPTION 'entity % is not available in campaign schema %',
            target_entity_id, target_schema USING ERRCODE = 'foreign_key_violation';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION grimoire.validate_campaign_grant_entity()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM grimoire.lock_campaign_entity(TG_TABLE_SCHEMA, NEW.entity_id);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION grimoire.validate_campaign_relationship_entities()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    endpoint_id UUID;
BEGIN
    -- Lock in UUID order so concurrent inverse edges cannot deadlock.
    FOR endpoint_id IN
        SELECT value
        FROM unnest(ARRAY[NEW.from_entity_id, NEW.to_entity_id]) AS value
        ORDER BY value
    LOOP
        PERFORM grimoire.lock_campaign_entity(TG_TABLE_SCHEMA, endpoint_id);
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION grimoire.cascade_campaign_entity_references()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_TABLE_SCHEMA !~ '^grimoire_campaign_[0-9a-f]{32}$'
       OR NOT EXISTS (
           SELECT 1 FROM grimoire.campaign AS c
           WHERE c.schema_name = TG_TABLE_SCHEMA
       ) THEN
        RAISE EXCEPTION 'invalid campaign schema %', TG_TABLE_SCHEMA
            USING ERRCODE = 'check_violation';
    END IF;
    EXECUTE format('DELETE FROM %I.knowledge_grant WHERE entity_id = $1', TG_TABLE_SCHEMA)
        USING OLD.id;
    EXECUTE format(
        'DELETE FROM %I.homebrew_relationship
         WHERE from_entity_id = $1 OR to_entity_id = $1',
        TG_TABLE_SCHEMA
    ) USING OLD.id;
    RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION grimoire.provision_campaign_schema(campaign_id UUID)
RETURNS TEXT
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    target_schema TEXT;
    expected_schema TEXT := grimoire.campaign_schema_name(campaign_id);
    campaign_table TEXT;
BEGIN
    SELECT c.schema_name INTO target_schema
    FROM grimoire.campaign AS c
    WHERE c.id = campaign_id
    FOR SHARE;

    IF target_schema IS NULL THEN
        RAISE EXCEPTION 'campaign % is not registered', campaign_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF target_schema <> expected_schema
       OR target_schema !~ '^grimoire_campaign_[0-9a-f]{32}$' THEN
        RAISE EXCEPTION 'campaign % has invalid schema routing', campaign_id
            USING ERRCODE = 'check_violation';
    END IF;
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', target_schema);
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', target_schema);

    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %1$I.player_character (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id),
            player_name TEXT,
            character_name TEXT NOT NULL,
            class_name TEXT,
            level INTEGER,
            sheet JSONB,
            CONSTRAINT player_character_campaign_chk
                CHECK (campaign_id = %2$L::uuid)
        )', target_schema, campaign_id
    );
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %1$I.game_session (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id),
            status TEXT NOT NULL DEFAULT ''active''
                CHECK (status IN (''active'', ''paused'', ''ended'')),
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            ended_at TIMESTAMPTZ,
            CONSTRAINT game_session_campaign_chk
                CHECK (campaign_id = %2$L::uuid)
        )', target_schema, campaign_id
    );
    EXECUTE format(
        'CREATE UNIQUE INDEX IF NOT EXISTS game_session_one_active
         ON %I.game_session (campaign_id) WHERE status != ''ended''',
        target_schema
    );
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %1$I.knowledge_grant (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id),
            entity_id UUID NOT NULL,
            player_character_id UUID NOT NULL
                REFERENCES %1$I.player_character(id) ON DELETE CASCADE,
            grant_scope TEXT NOT NULL
                CHECK (grant_scope IN (''full'', ''partial'', ''name_only'')),
            revealed_details JSONB,
            granted_in_session UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (entity_id, player_character_id),
            CONSTRAINT knowledge_grant_campaign_chk
                CHECK (campaign_id = %2$L::uuid)
        )', target_schema, campaign_id
    );
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I.session_transcript (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            game_session_id UUID NOT NULL
                REFERENCES %I.game_session(id) ON DELETE CASCADE,
            role TEXT NOT NULL
                CHECK (role IN (''player'', ''dm'', ''assistant'', ''system'')),
            content TEXT NOT NULL,
            tokens INTEGER NOT NULL DEFAULT 0 CHECK (tokens >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )', target_schema, target_schema
    );
    EXECUTE format(
        'CREATE INDEX IF NOT EXISTS session_transcript_session_time
         ON %I.session_transcript (game_session_id, created_at)', target_schema
    );

    -- Backing tables for campaign-authored facts. They mirror corpus row shapes
    -- so the views below can expose one stable ORM surface.
    IF to_regclass(format('%I.homebrew_entity', target_schema)) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I.homebrew_entity
                (LIKE grimoire.entity INCLUDING DEFAULTS INCLUDING GENERATED
                 INCLUDING CONSTRAINTS INCLUDING STORAGE)', target_schema
        );
        -- New campaigns are provisioned after the shared-corpus-only check is
        -- installed below. LIKE copies CHECK constraints, so remove that one
        -- before adding the inverse homebrew-only invariant.
        EXECUTE format(
            'ALTER TABLE %I.homebrew_entity
                DROP CONSTRAINT IF EXISTS entity_shared_corpus_only_chk',
            target_schema
        );
        EXECUTE format(
            'ALTER TABLE %I.homebrew_entity
                ADD PRIMARY KEY (id),
                ADD CONSTRAINT homebrew_entity_source_chk
                    CHECK (source_type = ''homebrew'')', target_schema
        );
        EXECUTE format(
            'CREATE INDEX homebrew_entity_type_name ON %I.homebrew_entity
                (entity_type, name)', target_schema
        );
    END IF;

    IF to_regclass(format('%I.homebrew_entity_creature', target_schema)) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I.homebrew_entity_creature
                (LIKE grimoire.entity_creature INCLUDING DEFAULTS
                 INCLUDING CONSTRAINTS INCLUDING STORAGE)', target_schema
        );
        EXECUTE format(
            'ALTER TABLE %1$I.homebrew_entity_creature
                ADD PRIMARY KEY (entity_id),
                ADD FOREIGN KEY (entity_id) REFERENCES %1$I.homebrew_entity(id)
                    ON DELETE CASCADE', target_schema
        );
    END IF;
    IF to_regclass(format('%I.homebrew_entity_spell', target_schema)) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I.homebrew_entity_spell
                (LIKE grimoire.entity_spell INCLUDING DEFAULTS
                 INCLUDING CONSTRAINTS INCLUDING STORAGE)', target_schema
        );
        EXECUTE format(
            'ALTER TABLE %1$I.homebrew_entity_spell
                ADD PRIMARY KEY (entity_id),
                ADD FOREIGN KEY (entity_id) REFERENCES %1$I.homebrew_entity(id)
                    ON DELETE CASCADE', target_schema
        );
    END IF;
    IF to_regclass(format('%I.homebrew_entity_location', target_schema)) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I.homebrew_entity_location
                (LIKE grimoire.entity_location INCLUDING DEFAULTS
                 INCLUDING CONSTRAINTS INCLUDING STORAGE)', target_schema
        );
        EXECUTE format(
            'ALTER TABLE %1$I.homebrew_entity_location
                ADD PRIMARY KEY (entity_id),
                ADD FOREIGN KEY (entity_id) REFERENCES %1$I.homebrew_entity(id)
                    ON DELETE CASCADE', target_schema
        );
    END IF;
    IF to_regclass(format('%I.homebrew_entity_npc', target_schema)) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I.homebrew_entity_npc
                (LIKE grimoire.entity_npc INCLUDING DEFAULTS
                 INCLUDING CONSTRAINTS INCLUDING STORAGE)', target_schema
        );
        EXECUTE format(
            'ALTER TABLE %1$I.homebrew_entity_npc
                ADD PRIMARY KEY (entity_id),
                ADD FOREIGN KEY (entity_id) REFERENCES %1$I.homebrew_entity(id)
                    ON DELETE CASCADE', target_schema
        );
    END IF;
    IF to_regclass(format('%I.homebrew_chunk_entity_mention', target_schema)) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I.homebrew_chunk_entity_mention
                (LIKE grimoire.chunk_entity_mention INCLUDING DEFAULTS
                 INCLUDING CONSTRAINTS INCLUDING STORAGE)', target_schema
        );
        EXECUTE format(
            'ALTER TABLE %1$I.homebrew_chunk_entity_mention
                ADD PRIMARY KEY (chunk_id, entity_id),
                ADD FOREIGN KEY (chunk_id) REFERENCES grimoire.knowledge_chunk(id)
                    ON DELETE CASCADE,
                ADD FOREIGN KEY (entity_id) REFERENCES %1$I.homebrew_entity(id)
                    ON DELETE CASCADE', target_schema
        );
    END IF;
    IF to_regclass(format('%I.homebrew_relationship', target_schema)) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I.homebrew_relationship
                (LIKE grimoire.relationship INCLUDING DEFAULTS
                 INCLUDING CONSTRAINTS INCLUDING STORAGE)', target_schema
        );
        EXECUTE format(
            'ALTER TABLE %I.homebrew_relationship
                ADD PRIMARY KEY (id),
                ADD UNIQUE (from_entity_id, to_entity_id, rel_type)', target_schema
        );
    END IF;
    IF to_regclass(format('%I.local_embedding', target_schema)) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I.local_embedding
                (LIKE grimoire.embedding INCLUDING DEFAULTS
                 INCLUDING CONSTRAINTS INCLUDING STORAGE)', target_schema
        );
        EXECUTE format(
            'ALTER TABLE %I.local_embedding
                ADD PRIMARY KEY (id),
                ADD UNIQUE (embeddable_kind, embeddable_id, model)', target_schema
        );
    END IF;

    -- Stable campaign read surface. Shared objects are filtered to extracted
    -- corpus rows; local homebrew never becomes visible to another schema.
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.entity AS
            SELECT * FROM grimoire.entity
            WHERE source_type = ''extracted''
              AND grimoire.campaign_context_matches(%2$L)
            UNION ALL SELECT * FROM %1$I.homebrew_entity
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.entity_creature AS
            SELECT * FROM grimoire.entity_creature
            WHERE grimoire.campaign_context_matches(%2$L)
            UNION ALL SELECT * FROM %1$I.homebrew_entity_creature
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.entity_spell AS
            SELECT * FROM grimoire.entity_spell
            WHERE grimoire.campaign_context_matches(%2$L)
            UNION ALL SELECT * FROM %1$I.homebrew_entity_spell
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.entity_location AS
            SELECT * FROM grimoire.entity_location
            WHERE grimoire.campaign_context_matches(%2$L)
            UNION ALL SELECT * FROM %1$I.homebrew_entity_location
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.entity_npc AS
            SELECT * FROM grimoire.entity_npc
            WHERE grimoire.campaign_context_matches(%2$L)
            UNION ALL SELECT * FROM %1$I.homebrew_entity_npc
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.knowledge_chunk AS
            SELECT * FROM grimoire.knowledge_chunk
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.book AS
            SELECT * FROM grimoire.book
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.adventure AS
            SELECT * FROM grimoire.adventure
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.chunk_extraction AS
            SELECT * FROM grimoire.chunk_extraction
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.chunk_entity_mention AS
            SELECT * FROM grimoire.chunk_entity_mention
            WHERE grimoire.campaign_context_matches(%2$L)
            UNION ALL SELECT * FROM %1$I.homebrew_chunk_entity_mention
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.relationship AS
            SELECT * FROM grimoire.relationship
            WHERE grimoire.campaign_context_matches(%2$L)
            UNION ALL SELECT * FROM %1$I.homebrew_relationship
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.embedding AS
            SELECT * FROM grimoire.embedding
            WHERE embeddable_kind != ''transcript''
              AND grimoire.campaign_context_matches(%2$L)
            UNION ALL SELECT * FROM %1$I.local_embedding
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );
    EXECUTE format(
        'CREATE OR REPLACE VIEW %1$I.adventure_entity AS
            SELECT DISTINCT a.id AS adventure_id, m.entity_id
            FROM grimoire.adventure AS a
            JOIN grimoire.knowledge_chunk AS kc
              ON kc.book_id = a.book_id
             AND kc.seq >= a.start_seq
             AND (a.end_seq IS NULL OR kc.seq <= a.end_seq)
            JOIN %1$I.chunk_entity_mention AS m ON m.chunk_id = kc.id
            WHERE grimoire.campaign_context_matches(%2$L)',
        target_schema, target_schema
    );

    FOREACH campaign_table IN ARRAY ARRAY[
        'player_character',
        'game_session',
        'knowledge_grant',
        'session_transcript',
        'homebrew_entity',
        'homebrew_entity_creature',
        'homebrew_entity_spell',
        'homebrew_entity_location',
        'homebrew_entity_npc',
        'homebrew_chunk_entity_mention',
        'homebrew_relationship',
        'local_embedding'
    ] LOOP
        EXECUTE format(
            'ALTER TABLE %1$I.%2$I ENABLE ROW LEVEL SECURITY',
            target_schema, campaign_table
        );
        EXECUTE format(
            'DROP POLICY IF EXISTS campaign_access_scope ON %1$I.%2$I',
            target_schema, campaign_table
        );
        EXECUTE format(
            'CREATE POLICY campaign_access_scope ON %1$I.%2$I
             TO grimoire_campaign_access
             USING (grimoire.campaign_context_matches(%3$L))
             WITH CHECK (grimoire.campaign_context_matches(%3$L))',
            target_schema, campaign_table, target_schema
        );
    END LOOP;

    EXECUTE format('DROP TRIGGER IF EXISTS validate_entity ON %I.knowledge_grant', target_schema);
    EXECUTE format(
        'CREATE TRIGGER validate_entity BEFORE INSERT OR UPDATE OF entity_id
         ON %I.knowledge_grant FOR EACH ROW
         EXECUTE FUNCTION grimoire.validate_campaign_grant_entity()',
        target_schema
    );
    EXECUTE format(
        'DROP TRIGGER IF EXISTS validate_endpoints ON %I.homebrew_relationship',
        target_schema
    );
    EXECUTE format(
        'CREATE TRIGGER validate_endpoints
         BEFORE INSERT OR UPDATE OF from_entity_id, to_entity_id
         ON %I.homebrew_relationship FOR EACH ROW
         EXECUTE FUNCTION grimoire.validate_campaign_relationship_entities()',
        target_schema
    );
    EXECUTE format(
        'DROP TRIGGER IF EXISTS cascade_references ON %I.homebrew_entity',
        target_schema
    );
    EXECUTE format(
        'CREATE TRIGGER cascade_references BEFORE DELETE
         ON %I.homebrew_entity FOR EACH ROW
         EXECUTE FUNCTION grimoire.cascade_campaign_entity_references()',
        target_schema
    );

    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM PUBLIC', target_schema);
    EXECUTE format(
        'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM grimoire_campaign_access',
        target_schema
    );
    EXECUTE format(
        'GRANT USAGE ON SCHEMA %I TO grimoire_campaign_access',
        target_schema
    );
    EXECUTE format(
        'GRANT SELECT, INSERT, UPDATE, DELETE ON
            %1$I.player_character,
            %1$I.game_session,
            %1$I.knowledge_grant,
            %1$I.session_transcript,
            %1$I.homebrew_entity,
            %1$I.homebrew_entity_creature,
            %1$I.homebrew_entity_spell,
            %1$I.homebrew_entity_location,
            %1$I.homebrew_entity_npc,
            %1$I.homebrew_chunk_entity_mention,
            %1$I.homebrew_relationship,
            %1$I.local_embedding
         TO grimoire_campaign_access',
        target_schema
    );
    EXECUTE format(
        'GRANT SELECT ON
            %1$I.entity,
            %1$I.entity_creature,
            %1$I.entity_spell,
            %1$I.entity_location,
            %1$I.entity_npc,
            %1$I.knowledge_chunk,
            %1$I.book,
            %1$I.adventure,
            %1$I.chunk_extraction,
            %1$I.chunk_entity_mention,
            %1$I.relationship,
            %1$I.embedding,
            %1$I.adventure_entity
         TO grimoire_campaign_access',
        target_schema
    );
    RETURN target_schema;
END;
$$;

REVOKE ALL ON FUNCTION grimoire.provision_campaign_schema(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION grimoire.lock_campaign_entity(TEXT, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION grimoire.validate_campaign_grant_entity() FROM PUBLIC;
REVOKE ALL ON FUNCTION grimoire.validate_campaign_relationship_entities() FROM PUBLIC;
REVOKE ALL ON FUNCTION grimoire.cascade_campaign_entity_references() FROM PUBLIC;

-- Refuse ambiguous legacy data rather than guessing a campaign or silently
-- leaving mutable rows in the shared corpus.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM grimoire.entity AS e
        LEFT JOIN grimoire.game_session AS s ON s.id = e.created_in_session
        WHERE e.source_type = 'homebrew' AND s.id IS NULL
    ) THEN
        RAISE EXCEPTION
            'cannot migrate homebrew entities without a valid creating game session';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM grimoire.knowledge_grant AS g
        JOIN grimoire.entity AS e ON e.id = g.entity_id
        JOIN grimoire.game_session AS s ON s.id = e.created_in_session
        WHERE e.source_type = 'homebrew' AND g.campaign_id <> s.campaign_id
    ) THEN
        RAISE EXCEPTION 'cannot migrate cross-campaign homebrew grants';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM grimoire.relationship AS r
        JOIN grimoire.entity AS a ON a.id = r.from_entity_id
        JOIN grimoire.entity AS b ON b.id = r.to_entity_id
        JOIN grimoire.game_session AS sa ON sa.id = a.created_in_session
        JOIN grimoire.game_session AS sb ON sb.id = b.created_in_session
        WHERE a.source_type = 'homebrew'
          AND b.source_type = 'homebrew'
          AND sa.campaign_id <> sb.campaign_id
    ) THEN
        RAISE EXCEPTION 'cannot migrate cross-campaign homebrew relationships';
    END IF;
    IF EXISTS (
        SELECT 1 FROM grimoire.embedding WHERE embeddable_kind = 'transcript'
    ) THEN
        RAISE EXCEPTION
            'cannot migrate transcript embeddings without a campaign transcript row';
    END IF;
END;
$$;

DO $$
DECLARE
    c RECORD;
BEGIN
    FOR c IN SELECT id, schema_name FROM grimoire.campaign ORDER BY id LOOP
        PERFORM grimoire.provision_campaign_schema(c.id);

        EXECUTE format(
            'INSERT INTO %1$I.player_character
             SELECT * FROM grimoire.player_character WHERE campaign_id = $1',
            c.schema_name
        ) USING c.id;
        EXECUTE format(
            'INSERT INTO %1$I.game_session
             SELECT * FROM grimoire.game_session WHERE campaign_id = $1',
            c.schema_name
        ) USING c.id;

        EXECUTE format(
            'INSERT INTO %1$I.homebrew_entity
                (id, entity_type, name, temporality, detail, source_type,
                 is_global, source_book, site, created_in_session, created_at)
             SELECT e.id, e.entity_type, e.name, e.temporality, e.detail,
                    e.source_type, e.is_global, e.source_book, e.site,
                    e.created_in_session, e.created_at
             FROM grimoire.entity AS e
             JOIN grimoire.game_session AS s ON s.id = e.created_in_session
             WHERE e.source_type = ''homebrew'' AND s.campaign_id = $1',
            c.schema_name
        ) USING c.id;
        EXECUTE format(
            'INSERT INTO %1$I.homebrew_entity_creature
             SELECT d.* FROM grimoire.entity_creature AS d
             JOIN %1$I.homebrew_entity AS e ON e.id = d.entity_id', c.schema_name
        );
        EXECUTE format(
            'INSERT INTO %1$I.homebrew_entity_spell
             SELECT d.* FROM grimoire.entity_spell AS d
             JOIN %1$I.homebrew_entity AS e ON e.id = d.entity_id', c.schema_name
        );
        EXECUTE format(
            'INSERT INTO %1$I.homebrew_entity_location
             SELECT d.* FROM grimoire.entity_location AS d
             JOIN %1$I.homebrew_entity AS e ON e.id = d.entity_id', c.schema_name
        );
        EXECUTE format(
            'INSERT INTO %1$I.homebrew_entity_npc
             SELECT d.* FROM grimoire.entity_npc AS d
             JOIN %1$I.homebrew_entity AS e ON e.id = d.entity_id', c.schema_name
        );
        EXECUTE format(
            'INSERT INTO %1$I.homebrew_chunk_entity_mention
             SELECT m.* FROM grimoire.chunk_entity_mention AS m
             JOIN %1$I.homebrew_entity AS e ON e.id = m.entity_id', c.schema_name
        );
        EXECUTE format(
            'INSERT INTO %1$I.homebrew_relationship
             SELECT r.* FROM grimoire.relationship AS r
             WHERE EXISTS (
                 SELECT 1 FROM %1$I.homebrew_entity AS e
                 WHERE e.id IN (r.from_entity_id, r.to_entity_id)
             )', c.schema_name
        );
        EXECUTE format(
            'INSERT INTO %1$I.local_embedding
             SELECT emb.* FROM grimoire.embedding AS emb
             JOIN %1$I.homebrew_entity AS e
               ON emb.embeddable_kind = ''entity''
              AND emb.embeddable_id = e.id', c.schema_name
        );

        EXECUTE format(
            'INSERT INTO %1$I.knowledge_grant
             SELECT * FROM grimoire.knowledge_grant WHERE campaign_id = $1',
            c.schema_name
        ) USING c.id;
    END LOOP;
END;
$$;

-- The transaction has copied every mutable row. Delete homebrew from the
-- shared corpus (FK cascades remove copied detail/mention/relationship rows),
-- then retire the three shared mutable tables. A failure anywhere above rolls
-- the whole migration back, including every dynamically-created schema.
DELETE FROM grimoire.embedding
WHERE embeddable_kind = 'entity'
  AND embeddable_id IN (
      SELECT id FROM grimoire.entity WHERE source_type = 'homebrew'
  );
DELETE FROM grimoire.entity WHERE source_type = 'homebrew';

DROP TABLE grimoire.knowledge_grant;
DROP TABLE grimoire.player_character;
DROP TABLE grimoire.game_session;

ALTER TABLE grimoire.entity
    ADD CONSTRAINT entity_shared_corpus_only_chk
        CHECK (source_type = 'extracted');

CREATE OR REPLACE FUNCTION grimoire.cascade_shared_entity_references()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    campaign_schema TEXT;
BEGIN
    FOR campaign_schema IN
        SELECT c.schema_name FROM grimoire.campaign AS c ORDER BY c.schema_name
    LOOP
        EXECUTE format(
            'DELETE FROM %I.knowledge_grant WHERE entity_id = $1',
            campaign_schema
        ) USING OLD.id;
        EXECUTE format(
            'DELETE FROM %I.homebrew_relationship
             WHERE from_entity_id = $1 OR to_entity_id = $1',
            campaign_schema
        ) USING OLD.id;
    END LOOP;
    RETURN OLD;
END;
$$;

CREATE TRIGGER cascade_campaign_references
BEFORE DELETE ON grimoire.entity
FOR EACH ROW
EXECUTE FUNCTION grimoire.cascade_shared_entity_references();

REVOKE ALL ON FUNCTION grimoire.cascade_shared_entity_references() FROM PUBLIC;

COMMENT ON SCHEMA grimoire IS
    'Shared Grimoire corpus and trusted campaign registry; campaign-routed sessions read corpus through per-campaign views.';
COMMENT ON COLUMN grimoire.campaign.schema_name IS
    'Canonical trusted route: grimoire_campaign_ plus the campaign UUID without dashes.';
