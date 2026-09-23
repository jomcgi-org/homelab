-- #5899: restrict the public entity catalog to publicly linked entities.

-- Issue entities are derived from every live, non-legacy fact, including
-- private ones, so the unfiltered catalog leaks private issue numbers (and
-- any other entity referenced only by private facts) to public_reader.
-- Filter the catalog to entities with at least one link from a public,
-- non-deleted note, matching the predicates of public_api.knowledge_notes.
CREATE OR REPLACE VIEW public_api.knowledge_entities AS
    SELECT
        e.id,
        e.kind,
        e.slug,
        e.title,
        e.aliases,
        e.scope,
        e.source,
        e.created_at,
        e.updated_at
    FROM knowledge.entities e
    WHERE EXISTS (
        SELECT 1
        FROM knowledge.note_entities ne
        JOIN knowledge.notes n ON n.note_id = ne.note_id
        WHERE ne.entity_id = e.id
          AND n.visibility = 'public'
          AND n.deleted_at IS NULL
    );

GRANT SELECT ON public_api.knowledge_entities TO public_reader;
