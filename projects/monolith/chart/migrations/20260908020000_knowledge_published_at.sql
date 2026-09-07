-- Publication lane for agent-derived knowledge facts (#5900).
ALTER TABLE knowledge.notes ADD COLUMN published_at TIMESTAMPTZ;

CREATE OR REPLACE VIEW public_api.knowledge_notes AS
    SELECT
        note_id,
        title,
        type,
        content,
        indexed_at,
        COALESCE(layout_x_public, layout_x) AS layout_x,
        COALESCE(layout_y_public, layout_y) AS layout_y,
        tags,
        aliases,
        path,
        verification_state,
        confidence,
        observed_at,
        scope,
        valid_from,
        valid_until,
        published_at,
        (
            EXISTS (
                SELECT 1
                FROM knowledge.disputes
                WHERE knowledge.disputes.note_id = notes.note_id
                  AND knowledge.disputes.state = 'open'
            )
            OR verification_state = 'disputed'
        ) AS disputed
    FROM knowledge.notes
    WHERE visibility = 'public'
      AND deleted_at IS NULL;
