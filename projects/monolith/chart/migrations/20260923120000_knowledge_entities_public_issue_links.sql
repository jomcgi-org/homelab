-- #5900: confine regex-derived issue entities to the public tier.
-- public_api.knowledge_entities selected the whole entities table, so an
-- issue slug created from a private fact (link_issue_entities scans live
-- non-legacy facts regardless of visibility) was readable as public_reader.
-- knowledge_note_entities already joins public_api.knowledge_notes; apply the
-- same boundary here for issue entities only. Curated project, service, and
-- environment entities stay fully visible with or without links.
CREATE OR REPLACE VIEW public_api.knowledge_entities AS
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
    FROM knowledge.entities e
    WHERE e.kind <> 'issue'
        OR EXISTS (
            SELECT 1
            FROM knowledge.note_entities ne
            JOIN public_api.knowledge_notes pn ON pn.note_id = ne.note_id
            WHERE ne.entity_id = e.id
        );
