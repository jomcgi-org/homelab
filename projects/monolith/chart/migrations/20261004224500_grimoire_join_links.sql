-- No public_reader grants: possession and verified recipient identity are
-- separately checked by the private BFF/backend invitation endpoints.
CREATE TABLE grimoire.campaign_join_link (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    recipient_id UUID REFERENCES grimoire.app_user(id),
    invitee_email TEXT NOT NULL,
    issued_by_id UUID NOT NULL REFERENCES grimoire.app_user(id),
    token_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_by_id UUID REFERENCES grimoire.app_user(id),
    enrollment_allowed BOOLEAN NOT NULL DEFAULT false,
    enrollment_username TEXT NOT NULL,
    enrollment_id UUID,
    CONSTRAINT campaign_join_link_digest_key UNIQUE (token_digest),
    CONSTRAINT campaign_join_link_status_chk CHECK (status IN ('pending', 'accepted', 'revoked')),
    CONSTRAINT campaign_join_link_acceptance_chk CHECK (status = 'revoked' OR ((status = 'accepted') = (accepted_by_id IS NOT NULL)))
);
CREATE INDEX campaign_join_link_campaign_idx ON grimoire.campaign_join_link (campaign_id, status);
