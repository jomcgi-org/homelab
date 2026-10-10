CREATE TABLE grimoire.campaign_voice (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    speaker_key TEXT NOT NULL,
    voice_hint JSONB NOT NULL DEFAULT '{}',
    rate REAL NOT NULL DEFAULT 1,
    pitch REAL NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT campaign_voice_campaign_id_speaker_key_key UNIQUE (campaign_id, speaker_key),
    CONSTRAINT campaign_voice_rate_chk CHECK (rate BETWEEN 0.5 AND 2),
    CONSTRAINT campaign_voice_pitch_chk CHECK (pitch BETWEEN 0 AND 2)
);

-- Private campaign settings: deliberately no public_reader grant.
