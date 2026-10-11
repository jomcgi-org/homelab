CREATE TABLE grimoire.character_fact (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id uuid NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    session_id uuid NOT NULL REFERENCES grimoire.game_session(id) ON DELETE CASCADE,
    player_character_id uuid REFERENCES grimoire.player_character(id) ON DELETE CASCADE,
    viewer_key text NOT NULL,
    statement text NOT NULL,
    entity_id uuid REFERENCES grimoire.entity(id) ON DELETE SET NULL,
    evidence_event_ids uuid[] NOT NULL,
    extraction_version text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT character_fact_viewer_chk CHECK (
        (viewer_key = 'party' AND player_character_id IS NULL) OR
        (player_character_id IS NOT NULL AND viewer_key = player_character_id::text)
    ),
    CONSTRAINT character_fact_status_chk CHECK (status IN ('active', 'disputed', 'retracted')),
    CONSTRAINT character_fact_evidence_chk CHECK (
        cardinality(evidence_event_ids) > 0 AND array_position(evidence_event_ids, NULL) IS NULL
    ),
    CONSTRAINT character_fact_replay_key UNIQUE (session_id, viewer_key, extraction_version, statement)
);

CREATE INDEX character_fact_viewer_status_idx
    ON grimoire.character_fact (campaign_id, viewer_key, status);
CREATE INDEX character_fact_evidence_idx
    ON grimoire.character_fact USING gin (evidence_event_ids);

-- Private player and party state: deliberately no public_reader grant.
ALTER TABLE grimoire.embedding
    DROP CONSTRAINT embedding_embeddable_kind_chk,
    ADD CONSTRAINT embedding_embeddable_kind_chk
        CHECK (embeddable_kind IN ('entity', 'chunk', 'transcript', 'note', 'event', 'fact')),
    DROP CONSTRAINT embedding_play_audience_chk,
    ADD CONSTRAINT embedding_play_audience_chk CHECK (
        (embeddable_kind IN ('entity', 'chunk') AND campaign_id IS NULL
            AND audience IS NULL AND audience_pc_ids IS NULL AND dm_readable IS NULL)
        OR
        (embeddable_kind IN ('note', 'event', 'transcript', 'fact')
            AND campaign_id IS NOT NULL AND audience IS NOT NULL
            AND audience_pc_ids IS NOT NULL AND (
                (embeddable_kind = 'note' AND audience IN ('character', 'party')
                    AND dm_readable IS NOT NULL)
                OR
                (embeddable_kind IN ('event', 'transcript')
                    AND audience IN ('table', 'dm', 'pcs'))
                OR
                (embeddable_kind = 'fact' AND audience IN ('table', 'pcs')
                    AND author_member_id IS NULL AND dm_readable IS NULL)
            ))
    );
