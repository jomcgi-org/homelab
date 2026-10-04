-- Expose review deadlines so public readers share the application clock's
-- current predicate in knowledge/freshness.py. Preserve the definer-rights
-- confinement and existing columns from 20261002150000; append the review
-- columns mirrored by knowledge/public_models.py, with no lease or backfill.
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
