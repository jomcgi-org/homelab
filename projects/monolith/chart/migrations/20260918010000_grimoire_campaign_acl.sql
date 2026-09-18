CREATE TABLE grimoire.app_user (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email      TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT app_user_email_normalized_chk
        CHECK (email = lower(btrim(email)) AND email <> '')
);

CREATE TABLE grimoire.campaign_member (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id         UUID NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    app_user_id         UUID NOT NULL REFERENCES grimoire.app_user(id) ON DELETE CASCADE,
    role                TEXT NOT NULL,
    player_character_id UUID REFERENCES grimoire.player_character(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT campaign_member_role_chk
        CHECK (role IN ('dm', 'player')),
    CONSTRAINT campaign_member_dm_character_chk
        CHECK (role = 'player' OR player_character_id IS NULL),
    CONSTRAINT campaign_member_campaign_id_app_user_id_key
        UNIQUE (campaign_id, app_user_id),
    CONSTRAINT campaign_member_campaign_id_player_character_id_key
        UNIQUE (campaign_id, player_character_id)
);
