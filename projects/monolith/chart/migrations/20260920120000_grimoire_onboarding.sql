ALTER TABLE grimoire.app_user
    ADD COLUMN issuer TEXT,
    ADD COLUMN subject TEXT,
    ADD COLUMN display_name TEXT,
    ADD CONSTRAINT app_user_identity_key UNIQUE (issuer, subject),
    ADD CONSTRAINT app_user_identity_pair_chk CHECK ((issuer IS NULL) = (subject IS NULL));

ALTER TABLE grimoire.campaign
    ADD COLUMN owner_app_user_id UUID REFERENCES grimoire.app_user(id);

-- Preserve existing DMs. The earliest DM owns the campaign after upgrade.
UPDATE grimoire.campaign c SET owner_app_user_id = (
    SELECT m.app_user_id FROM grimoire.campaign_member m
    WHERE m.campaign_id = c.id AND m.role = 'dm'
    ORDER BY m.created_at, m.id LIMIT 1
);

CREATE TABLE grimoire.campaign_invitation (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    invitee_id UUID NOT NULL REFERENCES grimoire.app_user(id) ON DELETE CASCADE,
    invited_by_id UUID NOT NULL REFERENCES grimoire.app_user(id),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT campaign_invitation_recipient_key UNIQUE (campaign_id, invitee_id),
    CONSTRAINT campaign_invitation_status_chk CHECK (status IN ('pending', 'accepted', 'declined', 'revoked'))
);
CREATE INDEX campaign_invitation_inbox_idx ON grimoire.campaign_invitation (invitee_id, status);
