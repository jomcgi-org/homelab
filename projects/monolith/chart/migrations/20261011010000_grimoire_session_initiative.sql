CREATE TABLE grimoire.session_initiative (
    session_id UUID PRIMARY KEY REFERENCES grimoire.game_session(id) ON DELETE CASCADE,
    campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    entries JSONB NOT NULL DEFAULT '[]',
    round INT NOT NULL DEFAULT 1,
    active_index INT NOT NULL DEFAULT 0,
    hidden_display TEXT NOT NULL DEFAULT 'mask',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT session_initiative_entries_chk CHECK (jsonb_typeof(entries) = 'array'),
    CONSTRAINT session_initiative_round_chk CHECK (round >= 1),
    CONSTRAINT session_initiative_active_index_chk CHECK (active_index >= 0),
    CONSTRAINT session_initiative_hidden_display_chk CHECK (hidden_display IN ('mask', 'omit'))
);

-- Private play state: deliberately no public_reader grant.
