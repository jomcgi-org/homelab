CREATE TABLE grimoire.session_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    session_id UUID NOT NULL REFERENCES grimoire.game_session(id) ON DELETE CASCADE,
    seq BIGINT NOT NULL,
    kind TEXT NOT NULL,
    author_member_id UUID REFERENCES grimoire.campaign_member(id) ON DELETE SET NULL,
    audience TEXT NOT NULL,
    audience_pc_ids JSONB NOT NULL DEFAULT '[]',
    body JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retracted_at TIMESTAMPTZ,
    CONSTRAINT session_event_kind_chk CHECK (kind IN ('narration', 'action', 'roll', 'reveal', 'handout', 'turn', 'system', 'utterance')),
    CONSTRAINT session_event_audience_chk CHECK (audience IN ('table', 'dm', 'pcs')),
    CONSTRAINT session_event_seq_chk CHECK (seq > 0),
    CONSTRAINT session_event_audience_pc_ids_chk CHECK (
        CASE WHEN jsonb_typeof(audience_pc_ids) = 'array' THEN
            CASE WHEN audience = 'pcs' THEN jsonb_array_length(audience_pc_ids) > 0
                 ELSE jsonb_array_length(audience_pc_ids) = 0 END
        ELSE false END
    ),
    CONSTRAINT session_event_session_id_seq_key UNIQUE (session_id, seq)
);

-- Private play state: deliberately no public_reader grant.
