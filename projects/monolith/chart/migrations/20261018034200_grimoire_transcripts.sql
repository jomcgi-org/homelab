CREATE TABLE grimoire.transcript_consent (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id uuid NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    member_id uuid NOT NULL REFERENCES grimoire.campaign_member(id) ON DELETE CASCADE,
    processor text NOT NULL CONSTRAINT transcript_consent_processor_chk CHECK (length(processor) BETWEEN 1 AND 120),
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    CONSTRAINT transcript_consent_revoked_at_chk CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
);

CREATE UNIQUE INDEX transcript_consent_active_idx
    ON grimoire.transcript_consent (campaign_id, member_id)
    WHERE revoked_at IS NULL;

ALTER TABLE grimoire.game_session
    ADD COLUMN transcript_state text NOT NULL DEFAULT 'off'
    CONSTRAINT game_session_transcript_state_chk CHECK (transcript_state IN ('off', 'on', 'paused'));

ALTER TABLE grimoire.campaign
    ADD COLUMN transcript_retention_days integer NOT NULL DEFAULT 30
    CONSTRAINT campaign_transcript_retention_days_chk CHECK (transcript_retention_days BETWEEN 1 AND 365);
