-- Staged for #6920. NOT part of the automatic migrations/*.sql bundle.
-- Promote in a separately authorized PR only after both readers have rolled.
-- RESTRICT is intentional: unexpected dependencies must abort the transaction.
DO $migration$
DECLARE
    notes_owner name;
    entities_owner name;
BEGIN
    SELECT pg_get_userbyid(relowner) INTO STRICT notes_owner
        FROM pg_class WHERE oid = 'public_api.knowledge_notes'::regclass;
    SELECT pg_get_userbyid(relowner) INTO STRICT entities_owner
        FROM pg_class WHERE oid = 'public_api.knowledge_note_entities'::regclass;

    DROP VIEW public_api.knowledge_note_entities RESTRICT;
    DROP VIEW public_api.knowledge_notes RESTRICT;
    ALTER TABLE knowledge.notes
        DROP COLUMN layout_x RESTRICT,
        DROP COLUMN layout_y RESTRICT,
        DROP COLUMN layout_x_public RESTRICT,
        DROP COLUMN layout_y_public RESTRICT;

    CREATE VIEW public_api.knowledge_notes AS
        SELECT
            note_id,
            title,
            type,
            content,
            indexed_at,
            tags,
            aliases,
            path,
            verification_state,
            confidence,
            observed_at,
            CASE
                WHEN scope IN (
                    'repo:jomcgi-org/homelab',
                    'org:jomcgi-org',
                    'environment:homelab'
                ) THEN scope
                ELSE NULL
            END AS scope,
            valid_from,
            valid_until,
            published_at,
            (
                EXISTS (
                    SELECT 1
                    FROM knowledge.disputes
                    WHERE knowledge.disputes.note_id = notes.note_id
                      AND knowledge.disputes.state IN ('open', 'resolution_failed')
                )
                OR verification_state = 'disputed'
            ) AS disputed,
            review_after,
            review_policy,
            last_reviewed_at
        FROM knowledge.notes
        WHERE visibility = 'public'
          AND deleted_at IS NULL;

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

    EXECUTE format('ALTER VIEW public_api.knowledge_notes OWNER TO %I', notes_owner);
    EXECUTE format('ALTER VIEW public_api.knowledge_note_entities OWNER TO %I', entities_owner);
    GRANT SELECT ON public_api.knowledge_notes TO public_reader;
    GRANT SELECT ON public_api.knowledge_note_entities TO public_reader;
END
$migration$;
