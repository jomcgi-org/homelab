ALTER TABLE grimoire.campaign
    ADD COLUMN notes_dm_readable_default BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE grimoire.note (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    author_member_id UUID REFERENCES grimoire.campaign_member(id) ON DELETE SET NULL,
    player_character_id UUID REFERENCES grimoire.player_character(id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK (kind IN ('character', 'party')),
    dm_readable BOOLEAN NOT NULL DEFAULT false,
    title TEXT NOT NULL,
    markdown TEXT NOT NULL DEFAULT '',
    links JSONB NOT NULL,
    pinned BOOLEAN NOT NULL DEFAULT false,
    created_in_session UUID REFERENCES grimoire.game_session(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    CONSTRAINT note_links_chk CHECK (COALESCE(
        jsonb_typeof(links) = 'object'
        AND jsonb_typeof(links->'entity_ids') = 'array'
        AND jsonb_typeof(links->'event_ids') = 'array', false
    ))
);

CREATE INDEX note_campaign_live_idx
    ON grimoire.note (campaign_id, pinned DESC, updated_at DESC, id)
    WHERE deleted_at IS NULL;

-- Private player and party state: deliberately no public_reader grant.
-- event_ids are opaque UUID strings, with no FK and no dereferencing here.
