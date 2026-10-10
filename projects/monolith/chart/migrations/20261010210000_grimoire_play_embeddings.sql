ALTER TABLE grimoire.embedding
    DROP CONSTRAINT embedding_embeddable_kind_chk,
    ADD CONSTRAINT embedding_embeddable_kind_chk
        CHECK (embeddable_kind IN ('entity', 'chunk', 'transcript', 'note', 'event')),
    ADD COLUMN campaign_id uuid REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    ADD COLUMN audience text,
    ADD COLUMN audience_pc_ids jsonb,
    ADD COLUMN author_member_id uuid,
    ADD COLUMN dm_readable boolean,
    ADD COLUMN content_hash text,
    ADD CONSTRAINT embedding_play_audience_chk CHECK (
        (embeddable_kind IN ('entity', 'chunk') AND campaign_id IS NULL
            AND audience IS NULL AND audience_pc_ids IS NULL AND dm_readable IS NULL)
        OR
        (embeddable_kind IN ('note', 'event', 'transcript')
            AND campaign_id IS NOT NULL AND audience IS NOT NULL
            AND audience_pc_ids IS NOT NULL AND (
                (embeddable_kind = 'note' AND audience IN ('character', 'party')
                    AND dm_readable IS NOT NULL)
                OR
                (embeddable_kind IN ('event', 'transcript')
                    AND audience IN ('table', 'dm', 'pcs'))
            ))
    );

CREATE INDEX embedding_campaign_kind_idx ON grimoire.embedding (campaign_id, embeddable_kind)
    WHERE campaign_id IS NOT NULL;
